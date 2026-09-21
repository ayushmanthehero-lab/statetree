"""Checkpoint an agent and restore it in a fresh process, without model calls."""

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile

from strands import Agent
from strands.models import Model
from statetree.runtime.runtime import StateTreeRuntime


class NoInferenceModel(Model):
    def get_config(self):
        return {'model_id': 'no-inference-demo'}

    def update_config(self, **kwargs):
        pass

    async def stream(self, *args, **kwargs):
        raise RuntimeError('This demo does not invoke a model')
        yield

    async def structured_output(self, *args, **kwargs):
        raise RuntimeError('This demo does not invoke a model')
        yield


def make_agent():
    return Agent(model=NoInferenceModel(), system_prompt='Checkpoint demo', callback_handler=None)


def git(repo, *args):
    subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restore-repo', type=Path)
    parser.add_argument('--commit-id')
    args = parser.parse_args()
    if args.restore_repo:
        agent = make_agent()
        runtime = StateTreeRuntime(agent, goal='Recover the demo', repo_path=args.restore_repo)
        runtime.restore(args.commit_id)
        assert agent.state.get('current_task') == 'working correctly'
        assert (args.restore_repo / 'demo_state.txt').read_text() == 'ORIGINAL'
        print('Fresh process restored agent state and workspace: PASS')
        return

    with tempfile.TemporaryDirectory(prefix='statetree-demo-') as directory:
        repo = Path(directory)
        git(repo, 'init', '-q')
        git(repo, 'config', 'user.name', 'StateTree demo')
        git(repo, 'config', 'user.email', 'demo@statetree.invalid')
        (repo / 'demo_state.txt').write_text('ORIGINAL', encoding='utf-8')
        git(repo, 'add', 'demo_state.txt')
        git(repo, 'commit', '-qm', 'initial')
        agent = make_agent()
        agent.state.set('current_task', 'working correctly')
        runtime = StateTreeRuntime(agent, goal='Recover the demo', repo_path=repo)
        checkpoint = runtime.commit(verified=True, verification={
            'passed': True, 'checks': ['demo file equals ORIGINAL', 'agent task equals working correctly'],
        })
        print(f'Checkpoint saved: {checkpoint.id}', flush=True)
        agent.state.set('current_task', 'CORRUPTED')
        (repo / 'demo_state.txt').write_text('CORRUPTED', encoding='utf-8')
        subprocess.run([
            sys.executable, '-B', '-m', 'examples.checkpoint_restore',
            '--restore-repo', str(repo), '--commit-id', checkpoint.id,
        ], check=True, cwd=Path(__file__).resolve().parents[1])
        print('Model calls: 0. Demo used an isolated temporary repository.')


if __name__ == '__main__':
    main()
