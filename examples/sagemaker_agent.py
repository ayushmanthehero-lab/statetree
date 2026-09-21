"""Opt-in Qwen endpoint text probe or agent smoke test with commit recall.

Agent mode creates a disposable local repository; text mode makes one tool-free call.
Does not create or delete AWS resources.
The --live flag explicitly enables inference on already deployed endpoints.
"""

import argparse
import json
from pathlib import Path
import tempfile
from time import perf_counter

from strands import Agent, tool

from examples.checkpoint_restore import git
from statetree.core.state import AgentState
from statetree.memory.commits import CommitNote
from statetree.runtime.runtime import StateTreeRuntime


def run_text_probe(model):
    """Check basic endpoint inference independently of tools, Git and persistence."""
    agent = Agent(model=model, context_manager=False, callback_handler=None)
    started = perf_counter()
    result = agent('What is 2+2? Reply only 4.', limits={'turns': 1})
    elapsed = perf_counter() - started
    text = ''.join(block['text'] for block in result.message['content'] if 'text' in block).strip()
    if text != '4':
        raise RuntimeError(f'Text smoke check failed: expected 4, received {text!r}')
    return {'mode': 'text', 'text': text,
            'raw_usage': getattr(model, 'last_provider_usage', None), 'elapsed_seconds': elapsed}


def run_demo(small_model, large_model=None):
    observations = []
    max_input_tokens = min(6000, *(
        (model.context_window_limit or 8192) - model.get_config().get('max_tokens', 512)
        for model in (small_model, large_model) if model is not None))
    if max_input_tokens <= 0:
        raise ValueError('Model context must leave room for input after reserving output tokens')

    @tool
    def read_demo_value() -> dict:
        """Read the authoritative integer used in the StateTree deployment smoke test."""
        observations.append(42)
        return {'demo_value': 42, 'source': 'local-smoke-fixture'}

    with tempfile.TemporaryDirectory(prefix='statetree-sagemaker-') as directory:
        repo = Path(directory)
        git(repo, 'init', '-q')
        git(repo, 'config', 'user.name', 'StateTree smoke test')
        git(repo, 'config', 'user.email', 'smoke@statetree.invalid')
        (repo / 'README.md').write_text('Disposable endpoint smoke test\n', encoding='utf-8')
        git(repo, 'add', 'README.md')
        git(repo, 'commit', '-qm', 'Initialize disposable fixture')
        agent = Agent(model=small_model, tools=[read_demo_value], context_manager=False,
                      callback_handler=None, system_prompt='Use tools for evidence. Reply concisely.')
        runtime = StateTreeRuntime(agent, goal='Remember the verified demonstration value',
                                   repo_path=repo, max_input_tokens=max_input_tokens, recent_turns=1,
                                   commit_context_budget=min(1600, max_input_tokens),
                                   input_cache_convention='included')
        runtime.set_state(AgentState(goal=runtime.goal, constraints=['Use the recorded evidence']))
        first = runtime.run('Call read_demo_value once and report the returned value.', limits={'turns': 3})
        if not observations:
            raise RuntimeError('Tool call was not executed. Check endpoint auto-tool-choice and tool parser.')
        checkpoint = runtime.commit(note=CommitNote(
            summary='The demo value is 42, read from the local smoke fixture',
            decisions=['Retrieve archived evidence if the demo value is needed again'],
        ))
        handoff = runtime.switch_model(large_model) if large_model is not None else None
        second = runtime.run('What demo value did the previous task record? Reply with the integer.',
                             new_task=True, limits={'turns': 3})
        if str(second).strip() != '42':
            raise RuntimeError(f'Recall smoke check failed: {second}')
        return {'first_response': str(first), 'recall_response': str(second),
                'checkpoint_id': checkpoint.id, 'handoff': handoff,
                'selected_commit_ids': runtime.hooks.last_context.selected_commit_ids,
                'usage': runtime.usage.summary(),
                'note': 'Disposable fixture only; endpoint uptime costs are separate from token counts.'}


def main(argv=None):
    def positive(value):
        try:
            number = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError('Must be a positive integer') from exc
        if number <= 0:
            raise argparse.ArgumentTypeError('Must be a positive integer')
        return number

    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--mode', choices=('text', 'agent'), default='agent')
    parser.add_argument('--endpoint-name')
    parser.add_argument('--model-id', default='Qwen/Qwen3.5-4B')
    parser.add_argument('--region')
    parser.add_argument('--aws-profile')
    parser.add_argument('--large-endpoint-name')
    parser.add_argument('--large-model-id', default='Qwen/Qwen3.8-27B')
    parser.add_argument('--max-output-tokens', type=positive, default=512)
    parser.add_argument('--context-window-limit', type=positive, default=8192)
    args = parser.parse_args(argv)
    if not args.live or not args.endpoint_name or not args.region:
        parser.error('Inference requires --live, --endpoint-name and --region. This command never deploys endpoints.')
    if args.max_output_tokens >= args.context_window_limit:
        parser.error('--max-output-tokens must be smaller than --context-window-limit.')
    if args.mode == 'text' and args.large_endpoint_name:
        parser.error('--large-endpoint-name applies only to --mode agent.')
    from statetree.models.sagemaker import SageMakerModel
    options = {'region_name': args.region, 'profile_name': args.aws_profile,
               'max_tokens': args.max_output_tokens, 'context_window_limit': args.context_window_limit,
               'enable_thinking': False}
    small = SageMakerModel(args.endpoint_name, model_id=args.model_id, **options)
    if args.mode == 'text':
        print(json.dumps(run_text_probe(small), indent=2))
        return
    large = None if not args.large_endpoint_name else SageMakerModel(
        args.large_endpoint_name, model_id=args.large_model_id, **options)
    print(json.dumps(run_demo(small, large), indent=2))


if __name__ == '__main__':
    main()
