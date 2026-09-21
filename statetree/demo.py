"""Executable local component demonstration. No model calls or invented tokens."""
from pathlib import Path
import sys

from statetree.core.commit import canonical_json
from statetree.project import Project, atomic_json
from statetree.runtime.durable import Step


def run_demo(directory):
    from statetree.cli import initialize_git
    directory = Path(directory).absolute()
    if directory.exists():
        raise ValueError('Demo requires a new disposable directory; existing data will not be overwritten')
    directory.mkdir(parents=True)
    repo = directory / 'project'
    initialize_git(repo)
    (repo / 'export_policy.py').write_text('RETENTION_DAYS = 7\n', encoding='utf-8')
    project = Project.init(repo, goal='Maintain the agreed export retention policy', config={
        'context_budget': 6000, 'note_budget': 1800, 'fact_budget': 1500, 'recent_turns': 2,
        'verification_commands': [[sys.executable, '-c',
            "from pathlib import Path; assert Path('export_policy.py').read_text() == 'RETENTION_DAYS = 7\\n'"]],
    })
    project.remember('export.retention_days', 7, evidence=['authored-demo:retention-decision'])
    project.checkpoint('Export retention is 7 days. Completed export files are deleted after 7 days.')
    for index in range(24):
        # Clearly authored conversation fixture, not a transcript from a model.
        project.agent.messages.extend([
            {'role': 'user', 'content': [{'text': f'Fixture design discussion {index}. ' + 'Discuss layout, spacing and colors. ' * 15}]},
            {'role': 'assistant', 'content': [{'text': f'Authored fixture reply {index}. Keep the design consistent.'}]},
        ])
    saved = project.checkpoint('Authored unrelated design conversation fixture')
    query = 'What is the agreed export retention period?'
    baseline = project.context(query, new_task=False)
    recalled = project.context(query, new_task=True)
    # Compare the actual serialized request shape used by ContextBuilder, not
    # just the note length. Both requests here are *prepared*, not sent.
    def byte_count(context):
        return len(canonical_json({'system_prompt': context['system_prompt'],
                                   'messages': context['messages'], 'tool_specs': []}))
    full_history_bytes = len(canonical_json({'system_prompt': project.agent.system_prompt,
                                           'messages': [*project.agent.messages, {'role': 'user', 'content': [{'text': query}]}],
                                           'tool_specs': []}))
    compacted = project.compact()
    project.restore(saved['id'])
    checks = {'checkpoint_restore': project.status()['head'] == saved['id'],
              'fact_recall': bool(project.recall(query)['notes']),
              'archive_preservation': bool(project.read_archive(compacted['archive_id'])['content'])}
    calls = []
    def effect(arguments, key):
        calls.append(key)
        (repo / 'durable_result.txt').write_text(arguments['text'], encoding='utf-8')
        return {'written': arguments['text']}
    steps = [Step('write-result', 'write', {'text': 'durable result'}, mode='idempotent')]
    first = project.run_steps(steps, {'write': effect}, run_id='demo-recovery')
    project = Project(repo)
    second = project.run_steps(steps, {'write': effect}, run_id='demo-recovery')
    checks['recovery'] = first == second and len(calls) == 1
    binding = project.switch_model('another-local-model', 'http://127.0.0.1:8081/v1/chat/completions')
    checks['public_state_handoff'] = (binding['model_invoked'] is False and
                                     project.state.facts['export.retention_days']['value'] == 7)
    def strategy(name, rank):
        def execute(branch):
            (branch.path / 'selected_strategy.txt').write_text(name, encoding='utf-8')
            return {'state': branch.state, 'result': {'rank': rank}}
        return execute
    speculation = project.speculate({'conservative': strategy('conservative', 2),
                                     'alternative': strategy('alternative', 1)},
                                    run_id='demo-speculation', score=lambda c: c['result']['rank'])
    if speculation['revision'] is None:
        raise RuntimeError('Local speculation verification failed: ' + str(speculation['attempts']))
    project.adopt(speculation['revision']['id'])
    checks['verified_merge'] = (repo / 'selected_strategy.txt').read_text() == 'conservative'
    bundle = project.export_state()
    other_repo = directory / 'imported-project'
    initialize_git(other_repo)
    other = Project.init(other_repo, goal='Portable destination')
    other.import_state(bundle)
    checks['portable_round_trip'] = other.state.to_dict() == project.state.to_dict()
    report = {
        'kind': 'statetree-local-component-demo', 'fixture': 'Authored policy and unrelated conversations',
        'project': str(repo), 'checks': checks, 'provider_requests': project.usage()['requests'],
        'context_comparison': {
            'counting_method': 'utf8_bytes_estimate', 'provider_tokens': None,
            'full_authored_history_bytes': full_history_bytes,
            'bounded_history_request_bytes': byte_count(baseline),
            'new_task_recall_request_bytes': byte_count(recalled),
            'selected_commit_ids': recalled['selected_commit_ids'],
            'model_task_success': None,
            'caveat': 'Prepared-request bytes, not measured model token savings, answer quality, cost, energy or water.'},
        'speculation': {'winner': speculation['winner'], 'report_id': speculation['report_id'],
                        'attempts': speculation['attempts']},
        'public_state_binding': binding, 'usage': project.usage(),
    }
    if not all(checks.values()):
        raise RuntimeError('A real local component check failed: ' + str(checks))
    atomic_json(directory / 'demo-report.json', report)
    return report
