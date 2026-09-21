"""Local checkpoint runtime. External side effects are outside its rollback scope."""

from pathlib import Path
import copy
from functools import wraps
from threading import Lock
import weakref

from statetree.core.commit import StateTreeCommit, validate_branch
from statetree.storage.local import HeadConflictError, LocalStateStore
from statetree.workspace.git import GitWorkspace
from statetree.runtime.usage import UsageLedger


def _quiescent(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError('Runtime is busy; wait for the current operation to finish')
        try:
            return method(self, *args, **kwargs)
        finally:
            self._operation_lock.release()
    return guarded


class StateTreeRuntime:
    def __init__(self, agent, *, goal, branch='main', repo_path='.', store_path=None,
                 max_input_tokens=None, recent_turns=4, counter=None, run_id=None,
                 total_token_limit=None, input_cache_convention='auto',
                 commit_context_budget=0, snapshot_loader=None):
        validate_branch(branch)
        if type(commit_context_budget) is not int or commit_context_budget < 0:
            raise ValueError('commit_context_budget must be a nonnegative integer')
        if commit_context_budget and max_input_tokens is None:
            raise ValueError('Commit context requires max_input_tokens')
        if snapshot_loader is not None and not callable(snapshot_loader):
            raise ValueError('snapshot_loader must be callable')
        self._snapshot_loader = snapshot_loader
        self._operation_lock = Lock()
        self.agent = agent
        self.goal = goal
        self.branch = branch
        repo_path = Path(repo_path).resolve()
        self.store = LocalStateStore(store_path or repo_path / '.statetree')
        self.store.bind_workspace(repo_path)
        from statetree.memory.commits import CommitMemory
        self.commit_memory = CommitMemory(self.store)
        self.commit_context_budget = commit_context_budget
        self._counter = counter
        exclusions = []
        if self.store.root.is_relative_to(repo_path):
            exclusions.append(self.store.root.relative_to(repo_path).as_posix())
        self.workspace = GitWorkspace(repo_path, exclude_paths=exclusions)
        self._parent = self.store.get_head(branch)
        self.usage = UsageLedger(self.store.root / 'usage.sqlite3')
        self.hooks = None
        if hasattr(agent, 'hooks'):
            from statetree.adapters.strands import StateTreeHooks, archive_reader
            runtime_ref = weakref.ref(self)

            def current_commit():
                owner = runtime_ref()
                if owner is None:
                    raise RuntimeError('The attached StateTree runtime no longer exists')
                return owner._parent
            if getattr(agent, '_statetree_runtime_attached', False):
                raise ValueError('This agent already has a StateTree runtime')
            if max_input_tokens is not None:
                names = {spec['name'] for spec in agent.tool_registry.get_all_tool_specs()}
                if 'statetree_read_archive' in names:
                    raise ValueError('The statetree_read_archive tool name is already registered')
                agent.tool_registry.process_tools([archive_reader(self.store)])
            self.hooks = StateTreeHooks(
                store=self.store, ledger=self.usage, goal=goal, branch=branch,
                run_id=run_id, max_input_tokens=max_input_tokens, recent_turns=recent_turns,
                counter=counter, total_token_limit=total_token_limit,
                input_cache_convention=input_cache_convention,
                commit_memory=self.commit_memory if commit_context_budget else None,
                commit_head=current_commit,
                commit_context_budget=commit_context_budget,
            )
            agent.hooks.add_hook(self.hooks)
            agent._statetree_runtime_attached = True
            agent._statetree_runtime_ref = weakref.ref(self)

    def decode_snapshot(self, value):
        """Decode using the explicitly bound adapter; native Strands is the default."""
        if self._snapshot_loader is not None:
            return self._snapshot_loader(value)
        from strands import Snapshot
        return Snapshot.from_dict(value)

    @_quiescent
    def run(self, prompt, *, new_task=False, **kwargs):
        """Invoke the attached agent; normal model-loop requests are metered by hooks."""
        if type(new_task) is not bool:
            raise ValueError('new_task must be a boolean')
        if not new_task:
            return self.agent(prompt, **kwargs)
        if not self.commit_context_budget or self.hooks is None or self.hooks.builder is None:
            raise ValueError('new_task requires bounded commit context')
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError('new_task requires a nonempty text prompt')
        from statetree.context.builder import _message_groups
        _message_groups(self.agent.messages)  # Never cut an unfinished tool call.
        previous = copy.deepcopy(self.agent.messages)
        old_archive = self.agent.state.get('statetree_prior_history')
        # Archive before replacing anything, even when no notes match the query.
        if old_archive is not None:
            self.store.read_archive(old_archive)
        archive_id = self.store.put_archive(previous if old_archive is None else {
            'messages': previous, 'prior_history_archive': old_archive})
        responses_before = self.hooks._completed_responses
        try:
            self.agent.state.set('statetree_prior_history', archive_id)
            self.agent.messages[:] = []
            return self.agent(prompt, **kwargs)
        finally:
            # Cancellation can return normally. Roll back the conversation if
            # no complete provider response was observed; retain model/tool
            # progress if a later step fails. Usage accounting is never rewound.
            if self.hooks._completed_responses == responses_before:
                self.agent.messages[:] = previous
                if old_archive is None:
                    self.agent.state.delete('statetree_prior_history')
                else:
                    self.agent.state.set('statetree_prior_history', old_archive)

    @_quiescent
    def commit(self, *, current_subgoal=None, verified=False, verification=None,
               message=None, note=None):
        """Checkpoint, optionally saving a caller-written message or structured note."""
        return self._commit(current_subgoal=current_subgoal, verified=verified,
                            verification=verification, message=message, note=note)

    def _commit(self, *, current_subgoal=None, verified=False, verification=None,
                message=None, note=None):
        if verified and (not verification or verification.get('passed') is not True):
            raise ValueError('verified=True requires verification evidence with passed=True')
        if message is not None and note is not None:
            raise ValueError('Supply either message or note, not both')
        if message is not None:
            note = {'summary': message}
        if note is not None:
            from statetree.memory.commits import CommitNote
            note = self.commit_memory.validate(note)
            evidence_id = self.store.put_archive({
                'messages': copy.deepcopy(self.agent.messages), 'verification': verification})
            data = note.to_dict()
            if evidence_id not in data['evidence_ids']:
                data['evidence_ids'].append(evidence_id)
            note = CommitNote.from_dict(data)
        with self.store.transaction() as db:
            if self.store.get_head(self.branch, db=db) != self._parent:
                raise HeadConflictError('Stale runtime: branch has advanced')
            snapshot = self.agent.take_snapshot(preset='session', include=['system_prompt']).to_dict()
            blob_id = self.store.put_snapshot(snapshot)
            workspace_sha = self.workspace.snapshot()
            if verified and verification.get('workspace_sha') is not None:
                expected_workspace = verification['workspace_sha']
                self.workspace.validate(expected_workspace)
                if self.workspace._entries(workspace_sha) != self.workspace._entries(expected_workspace):
                    raise ValueError('Workspace changed after verification; refusing canonical promotion')
            commit = StateTreeCommit.create(
                parent=self._parent, branch=self.branch, goal=self.goal,
                current_subgoal=current_subgoal,
                strands_snapshot_path=f'snapshots/{blob_id}.json', snapshot_digest=blob_id,
                workspace_sha=workspace_sha,
                verification_status='verified' if verified else 'unverified',
                verification=verification,
            )
            self.store.save_commit(commit)
            if note is not None:
                self.commit_memory.write(commit.id, note)
            self.store.move_head(
                db, self.branch, commit.id, expected_parent=self._parent,
                promote=verified and self.branch == 'main',
            )
        self._parent = commit.id
        return commit

    @_quiescent
    def recall(self, query, *, budget=None, resources=None, limit=8):
        """Find historical notes reachable from this runtime's current checkpoint."""
        if budget is None:
            budget = self.commit_context_budget or 2000
        if resources is None and hasattr(self.agent, 'state'):
            context = self.agent.state.get('statetree_context') or {}
            resources = {key: value for key, value in context.get('resources', {}).items()
                         if isinstance(value, str) and value.strip()}
        counter = self.hooks.builder.counter if self.hooks and self.hooks.builder else self._counter
        return self.commit_memory.search(query, self._parent, budget=budget,
                                         counter=counter, resources=resources, limit=limit)

    @_quiescent
    def fork(self, branch, commit_id=None):
        target = commit_id or self._parent
        if target is None:
            raise ValueError('Create a checkpoint before forking')
        self.store.fork(branch, target)
        return target

    @_quiescent
    def restore(self, commit_id):
        return self._restore(commit_id)

    def _restore(self, commit_id):
        with self.store.transaction() as db:
            if self.store.get_head(self.branch, db=db) != self._parent:
                raise HeadConflictError('Stale runtime: branch has advanced')
            data = self.store.load_commit(commit_id)
            snapshot = self.decode_snapshot(self.store.read_snapshot(data['snapshot_digest']))
            self.workspace.validate(data['workspace_sha'])
            old_agent = self.agent.take_snapshot(preset='session', include=['system_prompt'])
            old_workspace = self.workspace.snapshot()
            try:
                self.agent.load_snapshot(snapshot)
                self.workspace.restore(data['workspace_sha'])
                self.store.move_head(db, self.branch, commit_id, expected_parent=self._parent)
            except Exception:
                self.agent.load_snapshot(old_agent)
                self.workspace.restore(old_workspace)
                raise
        self._parent = commit_id
        self.goal = data['goal']
        if self.hooks is not None:
            self.hooks.goal = self.goal
        return data

    @_quiescent
    def set_state(self, state):
        """Set portable application state; use this at completed step boundaries."""
        from statetree.adapters.portable import _apply_strands_state
        _apply_strands_state(self.agent, state)
        self.goal = state.goal
        if self.hooks is not None:
            self.hooks.goal = state.goal

    @_quiescent
    def get_state(self):
        from statetree.adapters.portable import StrandsStateAdapter
        return StrandsStateAdapter(self.agent).export_state()

    @_quiescent
    def set_memory(self, memory, *, budget=None, counter=None, required=()):
        """Persist fact history while injecting only active, optionally budgeted facts."""
        from statetree.adapters.portable import StrandsStateAdapter, _apply_strands_state
        from statetree.core.state import AgentState
        from statetree.memory.facts import FactMemory
        adapter = StrandsStateAdapter(self.agent)
        state = adapter.export_state().to_dict()
        memory = FactMemory.from_dict(memory.to_dict())
        selected = memory.active_facts() if budget is None else memory.materialize(
            budget, counter=counter, required=required).facts
        state.update(facts=selected, memory=memory.to_dict())
        _apply_strands_state(self.agent, AgentState.from_dict(state))
        return selected

    @_quiescent
    def export_state(self):
        """Return a self-contained bundle; does not export files or credentials."""
        from statetree.adapters.portable import StrandsStateAdapter
        from statetree.storage.portable import export_bundle
        return export_bundle(StrandsStateAdapter(self.agent).export_state(), self.store)

    @_quiescent
    def import_state(self, bundle):
        from statetree.adapters.portable import _apply_strands_state
        from statetree.storage.portable import import_bundle
        state = import_bundle(bundle, self.store)
        _apply_strands_state(self.agent, state)
        self.goal = state.goal
        if self.hooks is not None:
            self.hooks.goal = state.goal
        return state

    @_quiescent
    def switch_model(self, model, *, max_input_tokens=None, counter=None):
        from statetree.runtime.handoff import switch_model
        return switch_model(self, model, max_input_tokens=max_input_tokens, counter=counter)

    @_quiescent
    def run_steps(self, steps, tools, *, run_id):
        """Run registered durable tool steps with aligned checkpoint recovery."""
        from statetree.runtime.workflow import run_checkpointed
        return run_checkpointed(self, steps, tools, run_id=run_id)
