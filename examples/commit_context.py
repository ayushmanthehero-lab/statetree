"""Reuse checkpoint notes at a new-task boundary with no remote inference."""

import copy
import json
import os
from pathlib import Path
import tempfile

os.environ['OTEL_SDK_DISABLED'] = 'true'

from strands import Agent
from strands.models import Model

from examples.checkpoint_restore import git
from statetree.memory.commits import CommitNote
from statetree.runtime.runtime import StateTreeRuntime


class LocalReplyModel(Model):
    """Capture real SDK requests and return fixed text entirely in process."""

    def __init__(self):
        self.requests = []
        self.config = {'model_id': 'local-commit-context', 'max_tokens': 32}

    def get_config(self):
        return self.config

    def update_config(self, **kwargs):
        self.config.update(kwargs)

    async def structured_output(self, *args, **kwargs):
        raise NotImplementedError('This demo uses plain-text replies')
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.requests.append(copy.deepcopy({
            'system_prompt': system_prompt,
            'messages': [{key: value for key, value in message.items() if key != 'metadata'}
                         for message in messages],
            'tool_specs': tool_specs,
        }))
        yield {'messageStart': {'role': 'assistant'}}
        yield {'contentBlockDelta': {'delta': {'text': 'Local fixture reply; no code changes made.'}}}
        yield {'contentBlockStop': {}}
        yield {'messageStop': {'stopReason': 'end_turn'}}


def serialized_bytes(request):
    """Measure captured request JSON; provider framing/tokenization is unmeasured."""
    return len(json.dumps(request, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8'))


def completed_exchange(topic):
    return [
        {'role': 'user', 'content': [{'text': f'Inspect the {topic} fixture.'}]},
        {'role': 'assistant', 'content': [{'text': f'{topic} fixture detail; ' * 110}]},
    ]


def main():
    with tempfile.TemporaryDirectory(prefix='statetree-commit-context-') as directory:
        repo = Path(directory)
        git(repo, 'init', '-q')
        git(repo, 'config', 'user.name', 'StateTree demo')
        git(repo, 'config', 'user.email', 'demo@statetree.invalid')
        (repo / 'auth.py').write_text('# Authentication fixture, version v1\n', encoding='utf-8')
        (repo / 'ui.css').write_text('/* Dashboard fixture, version v1 */\n', encoding='utf-8')
        git(repo, 'add', 'auth.py', 'ui.css')
        git(repo, 'commit', '-qm', 'initial disposable demo fixture')

        model = LocalReplyModel()
        agent = Agent(model=model, callback_handler=None, context_manager=False,
                      system_prompt='Use current requirements; consult historical evidence when needed.')
        agent.state.set('statetree_context', {
            'constraints': ['Preserve the existing authentication validation rules'],
            'active_subgoal': 'Implement password reset',
            'resources': {'auth.py': 'v1', 'ui.css': 'v1'},
        })
        runtime = StateTreeRuntime(
            agent, goal='Maintain the application', repo_path=repo,
            max_input_tokens=12000, commit_context_budget=3000, recent_turns=1,
        )

        # Representative completed conversations, not actual implementation results.
        agent.messages.extend(completed_exchange('login authentication'))
        auth = runtime.commit(note=CommitNote(
            summary='Login authentication uses shared validation',
            changes=['auth.py'],
            decisions=['Password reset must reuse authentication validation'],
            pending=['password reset'], dependencies={'auth.py': 'v1'},
        ))
        agent.messages.extend(completed_exchange('dashboard spacing'))
        original_history = copy.deepcopy(agent.messages)
        ui = runtime.commit(note=CommitNote(
            summary='Restyled dashboard spacing and colors', changes=['ui.css'],
            decisions=['Keep the compact layout'], dependencies={'ui.css': 'v1'},
        ))
        history_id = runtime.commit_memory.read(ui.id).evidence_ids[-1]
        assert runtime.store.read_archive(history_id)['messages'] == original_history

        query = 'password reset authentication validation'
        selection = runtime.recall(query)
        assert [note['commit_id'] for note in selection.notes] == [auth.id]
        print('Recall: authentication checkpoint selected; unrelated UI checkpoint excluded')
        print(f'Note estimate: {selection.estimated_count} ({selection.counting_method})')

        runtime.run(query)
        full_request = model.requests[-1]
        full_bytes = serialized_bytes(full_request)
        full_estimate = runtime.hooks.last_context.estimated_input_tokens
        assert len(full_request['messages']) == len(original_history) + 1
        assert full_bytes <= full_estimate <= 12000

        runtime.restore(ui.id)
        runtime.run(query, new_task=True)
        fresh_request = model.requests[-1]
        fresh_bytes = serialized_bytes(fresh_request)
        fresh_estimate = runtime.hooks.last_context.estimated_input_tokens
        assert fresh_bytes <= fresh_estimate <= 12000
        assert fresh_bytes < full_bytes <= 12000
        assert auth.id in fresh_request['system_prompt']
        assert ui.id not in fresh_request['system_prompt']
        prior_history_id = agent.state.get('statetree_prior_history')
        assert runtime.store.read_archive(prior_history_id) == original_history

        print(f'Serialized request: full history {full_bytes} UTF-8 bytes; new task {fresh_bytes} UTF-8 bytes')
        print(f'Preflight estimates: full history {full_estimate}; new task {fresh_estimate} (utf8_bytes_estimate)')
        print('Original completed conversation: recovered exactly from its local archive')
        print(f'Local scripted replies: {len(model.requests)}; remote model requests: 0')
        print('PASS: byte estimates only; actual token savings and task success are unmeasured')


if __name__ == '__main__':
    main()
