"""Exercise StateTree's local features in disposable repositories; no AWS calls."""

from pathlib import Path
import tempfile

from strands import Agent

from examples.checkpoint_restore import NoInferenceModel, git
from statetree.adapters.portable import MappingStateAdapter
from statetree.core.state import AgentState
from statetree.memory import FactMemory
from statetree.runtime.durable import Step
from statetree.runtime.parallel import SpeculativeCoordinator
from statetree.runtime.runtime import StateTreeRuntime
from statetree.storage.local import LocalStateStore
from statetree.storage.portable import import_bundle
from statetree.workspace.branches import BranchManager, MergeConflictError


class NamedOfflineModel(NoInferenceModel):
    def __init__(self, name):
        self.name = name

    def get_config(self):
        return {'model_id': self.name}


def main():
    with tempfile.TemporaryDirectory(prefix='statetree-features-') as directory:
        repo = Path(directory) / 'repo'
        repo.mkdir()
        git(repo, 'init', '-q')
        git(repo, 'config', 'user.name', 'StateTree demo')
        git(repo, 'config', 'user.email', 'demo@statetree.invalid')
        (repo / 'answer.txt').write_text('0', encoding='utf-8')
        git(repo, 'add', 'answer.txt')
        git(repo, 'commit', '-qm', 'initial')

        agent = Agent(model=NamedOfflineModel('local-A'), callback_handler=None, context_manager=False)
        runtime = StateTreeRuntime(agent, goal='Produce answer 42', repo_path=repo,
                                    max_input_tokens=6000, run_id='local-demo')
        evidence = runtime.store.put_archive({'expected_answer': 42})
        runtime.set_state(AgentState(goal='Produce answer 42', archive_ids=[evidence]))
        memory = FactMemory()
        memory.observe('answer', 0, evidence=['old inspection'])
        memory.observe('answer', 42, evidence=[evidence])
        runtime.set_memory(memory)
        assert runtime.get_state().facts['answer']['value'] == 42
        print('Memory: superseded fact archived; current fact is 42')

        custom = {}
        target = LocalStateStore(Path(directory) / 'destination-store')
        MappingStateAdapter(custom).import_state(import_bundle(runtime.export_state(), target))
        assert target.read_archive(evidence)['expected_answer'] == 42
        print('Portability: Strands state and evidence imported into a custom loop')

        runtime.switch_model(NamedOfflineModel('local-B'))
        runtime.switch_model(NamedOfflineModel('local-A'))
        print('Models: local-A -> local-B -> local-A; no inference requested')

        calls = []
        def write_answer(arguments, operation_id):
            calls.append(operation_id)
            (repo / 'answer.txt').write_text(str(arguments['value']), encoding='utf-8')
            return {'value': arguments['value']}
        steps = [Step('write-answer', 'write', {'value': 42}, mode='idempotent')]
        first = runtime.run_steps(steps, {'write': write_answer}, run_id='write-demo')
        second = runtime.run_steps(steps, {'write': write_answer}, run_id='write-demo')
        assert first == second and len(calls) == 1
        print('Recovery: repeated workflow reuses its durable result and aligned checkpoint')

        def verifier(path, state, phase):
            return (path / 'answer.txt').read_text(encoding='utf-8') == '42' and state['answer'] == 42
        manager = BranchManager(repo, verifier=verifier)
        manager.initialize({'answer': 42})

        def strategy(value):
            def run(branch):
                (branch.path / 'answer.txt').write_text(str(value), encoding='utf-8')
                return {'state': {'answer': value}, 'result': {'score': 1}}
            return run
        report = SpeculativeCoordinator(manager, ledger=runtime.usage, max_workers=3).run(
            {'correct': strategy(42), 'wrong-a': strategy(40), 'wrong-b': strategy(41)},
            run_id='three-strategies', score=lambda candidate: candidate['result']['score'])
        assert report['winner'] == 'correct'
        assert (repo / 'answer.txt').read_text() == '42'
        print('Forks: three independent worktrees; verifier selected correct; result integrated')

        base = manager.head()
        version = manager.resource_version(base, 'file:answer.txt')
        stale = manager.fork('stale-reader', reads={'file:answer.txt': version})
        stale_candidate = manager.complete(stale, state={'answer': 42, 'observed': True})
        # Independently advance a declared logical resource without touching the caller.
        writer = manager.fork('new-state')
        changed = manager.complete(writer, state={'answer': 42, 'observed': False})
        manager.integrate(changed['id'], expected_parent=base['id'])
        try:
            manager.integrate(stale_candidate['id'], expected_parent=manager.head()['id'])
        except MergeConflictError:
            print('Concurrency: conflicting state updates rejected before promotion')
        else:
            raise AssertionError('Expected a concurrent state conflict')
        print('PASS: all local demonstrations; zero model requests and zero AWS calls')


if __name__ == '__main__':
    main()
