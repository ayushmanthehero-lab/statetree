"""Bounded local speculation with independent verification and durable usage.

Workers receive separate Git folders, not security or external-effect
sandboxes. They must use branch.path explicitly (never process-wide chdir),
stop writing before returning, and avoid uncontrolled external side effects.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import math
from uuid import uuid4

from statetree.workspace.branches import StaleParentError


class SpeculativeCoordinator:
    """Run local callable alternatives, retaining all files and observed usage.

    Strategies return ``{'state': dict, 'result': JSON, 'usage': [records]}``.
    Prefer ``branch.record_usage`` immediately after each model request so
    exceptions do not lose accounting. A usage record supports request_id,
    usage, phase, model, status, and input_cache_convention. No usage is
    invented for a plain local computation. ``usage=None`` on an observed
    request is recorded as unknown by UsageLedger.

    ``score(candidate)`` is an application callback; higher finite scores win,
    with strategy name as deterministic tiebreaker. Each eligible candidate
    is independently verified. The winner must also pass integrated checks.
    Token limits are between-call thresholds, not hard billing caps: workers
    must call ``branch.check_budget`` before each request. Concurrent requests
    already in flight can exceed a run threshold. Callables cannot be safely
    killed by a thread pool; use bounded/cooperative local workers.
    """

    def __init__(self, manager, *, ledger, max_workers=4, max_branches=8,
                 total_token_limit=None, per_branch_token_limit=None):
        for name, value in (('max_workers', max_workers), ('max_branches', max_branches)):
            if type(value) is not int or value <= 0:
                raise ValueError(name + ' must be a positive integer')
        for name, value in (('total_token_limit', total_token_limit),
                            ('per_branch_token_limit', per_branch_token_limit)):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(name + ' must be a positive integer')
        self.manager = manager
        self.ledger = ledger
        self.max_workers = max_workers
        self.max_branches = max_branches
        self.total_token_limit = total_token_limit
        self.per_branch_token_limit = per_branch_token_limit

    def _check_budget(self, run_id, branch):
        for limit, criteria in (
            (self.total_token_limit, {'run_id': run_id}),
            (self.per_branch_token_limit, {'run_id': run_id, 'branch': branch}),
        ):
            if limit is None:
                continue
            usage = self.ledger.summary(**criteria)
            if usage['unknown_usage_requests'] or usage['ambiguous_usage_requests']:
                raise RuntimeError('Cannot continue with incomplete prior token usage')
            if usage['total_tokens'] >= limit:
                raise RuntimeError('Speculative token threshold reached')

    def run(self, strategies, *, run_id, reads=None, writes=None, score=None):
        if not isinstance(run_id, str) or not run_id:
            raise ValueError('run_id must be a nonempty string')
        if not isinstance(strategies, dict) or not strategies or len(strategies) > self.max_branches:
            raise ValueError('Supply between one and max_branches strategies')
        for name, strategy in strategies.items():
            self.manager._name(name)
            if not callable(strategy):
                raise ValueError('Each strategy must be callable')
        if score is not None and not callable(score):
            raise ValueError('score must be callable')
        parent = self.manager.head()
        if parent is None:
            raise ValueError('Initialize the manager before speculation')
        namespace = uuid4().hex
        branches = {name: self.manager.fork(name, base_id=parent['id'], reads=reads, writes=writes,
                                           namespace=namespace)
                    for name in sorted(strategies)}
        for name, branch in branches.items():
            def record(usage, *, request_id=None, phase='strategy', model='', status='ok',
                       input_cache_convention='auto', branch_name=name):
                return self.ledger.record(
                    request_id or uuid4().hex, run_id=run_id, branch=branch_name, phase=phase,
                    model=model, status=status, usage=usage, input_cache_convention=input_cache_convention,
                )
            branch._usage_recorder = record
            branch._budget_checker = lambda name=name: self._check_budget(run_id, name)

        def execute(name):
            branch = branches[name]
            attempt = {'name': name, 'path': str(branch.path), 'candidate_id': None,
                       'score': None, 'error': None, 'verified': False}
            try:
                branch.check_budget()
                output = strategies[name](branch)
                if not isinstance(output, dict):
                    raise ValueError('Strategy must return a result dictionary')
                usage = output.get('usage', [])
                if not isinstance(usage, list):
                    raise ValueError('Returned usage must be a list of request records')
                for entry in usage:
                    if not isinstance(entry, dict):
                        raise ValueError('Usage records must be dictionaries')
                    branch.record_usage(**entry)
                candidate = self.manager.complete(branch, state=output['state'], result=output.get('result'))
                attempt['candidate_id'] = candidate['id']
            except Exception as error:
                attempt['error'] = type(error).__name__ + ': ' + str(error)
            return attempt

        attempts = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            jobs = {executor.submit(execute, name): name for name in branches}
            for job in as_completed(jobs):
                attempts.append(job.result())
        attempts.sort(key=lambda attempt: attempt['name'])
        for attempt in attempts:
            if attempt['error'] is not None:
                continue
            try:
                candidate = self.manager.verify_candidate(attempt['candidate_id'])
                value = score(candidate) if score else 0
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError('Scores must be finite numbers')
                attempt['score'] = value
                attempt['verified'] = True
            except Exception as error:
                attempt['error'] = type(error).__name__ + ': ' + str(error)
        eligible = sorted((attempt for attempt in attempts if attempt['verified']),
                          key=lambda attempt: (-attempt['score'], attempt['name']))
        winner, revision = None, None
        for attempt in eligible:
            try:
                revision = self.manager.integrate(attempt['candidate_id'], expected_parent=parent['id'])
                winner = attempt['name']
                break
            except Exception as error:
                attempt['error'] = type(error).__name__ + ': ' + str(error)
                if isinstance(error, StaleParentError):
                    break
        for attempt in attempts:
            attempt['usage'] = self.ledger.summary(run_id=run_id, branch=attempt['name'])
        report = {'run_id': run_id, 'expected_parent': parent['id'], 'winner': winner,
                  'revision': revision, 'attempts': attempts, 'usage': self.ledger.summary(run_id=run_id)}
        report['report_id'] = self.manager.store.put_archive(report)
        return report
