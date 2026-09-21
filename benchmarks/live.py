"""Opt-in SageMaker memory-task smoke comparison using actual endpoint usage.

Nothing runs without --live, an endpoint name and a region. This is a small retrieval
suite, not SWE-bench and not evidence of general coding-agent performance.
"""

import argparse
import json
from pathlib import Path
import time
from uuid import uuid4


def parse_answer(text):
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char == '{':
            try:
                value, _ = decoder.raw_decode(text[index:])
                if isinstance(value, dict) and 'total' in value and 'first_marker' in value:
                    return value
            except ValueError:
                pass
    return None


def run_case(policy, task_id, steps, budget, model, output_dir, cache_convention):
    from strands import Agent, tool
    from statetree.adapters.strands import StateTreeHooks, archive_reader
    from statetree.runtime.usage import UsageLedger
    from statetree.storage.local import LocalStateStore

    run_id = f'{policy}-{task_id}-{uuid4().hex}'
    store = LocalStateStore(output_dir / 'live-artifacts' / run_id)
    ledger = UsageLedger(output_dir / 'live-usage.sqlite3')
    state = {'documents': {}}
    values = [task_id * 7 + index for index in range(steps)]
    marker = f'PROJECT-{task_id}-OK'

    @tool
    def read_document(index: int) -> dict:
        """Read a numbered test document containing a value and diagnostic noise.

        Args:
            index: Document number, starting at zero.
        """
        if type(index) is not int or not 0 <= index < steps:
            raise ValueError('Unknown document')
        facts = {'value': values[index]}
        if index == 0:
            facts['marker'] = marker
        # Deterministic extraction on all policies; only StateTree injects it.
        state['documents'][str(index)] = facts
        return {**facts, 'diagnostics': 'No additional facts in this log.\n' * 45}

    goal = f'Read documents 0 through {steps - 1}, retain the marker in document 0, and sum their values.'
    hooks = StateTreeHooks(
        store=store, ledger=ledger, goal=goal, run_id=run_id, state=state,
        max_input_tokens=budget if policy == 'statetree' else None,
        recent_turns=1, input_cache_convention=cache_convention,
    )
    agent = Agent(
        model=model, system_prompt='Use document evidence. Follow requests precisely.',
        tools=[read_document, archive_reader(store)], hooks=[hooks], callback_handler=None,
        context_manager='auto' if policy == 'strands_auto' else False,
    )
    start = time.perf_counter()
    answer = None
    error = None
    try:
        for index in range(steps):
            agent(f'{goal}\nRead document {index} using read_document, then acknowledge briefly.',
                  limits={'turns': 6})
        result = agent(
            f'{goal}\nReturn only JSON with first_marker and total. Retrieve missing evidence if necessary.',
            limits={'turns': steps + 3},
        )
        answer = parse_answer(str(result))
    except Exception as exception:
        error = f'{type(exception).__name__}: {exception}'
    return {
        'policy': policy, 'task': task_id, 'run_id': run_id,
        'success': answer == {'first_marker': marker, 'total': sum(values)},
        'answer': answer, 'error': error,
        'seconds': round(time.perf_counter() - start, 3),
        'usage': ledger.summary(run_id=run_id),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Enable billable model calls')
    parser.add_argument('--endpoint-name', help='Existing SageMaker real-time endpoint name')
    parser.add_argument('--model-id', default='Qwen/Qwen3.5-4B', help='Model name served by the endpoint')
    parser.add_argument('--region', default=None)
    parser.add_argument('--aws-profile', default=None)
    parser.add_argument('--max-output-tokens', type=int, default=512)
    parser.add_argument('--context-window-limit', type=int, default=8192,
                        help='Deployed endpoint context limit, including input and output tokens')
    parser.add_argument('--enable-thinking', action='store_true')
    parser.add_argument('--tasks', type=int, default=3)
    parser.add_argument('--steps', type=int, default=6)
    parser.add_argument('--budget', type=int, default=6000)
    parser.add_argument('--cache-convention', choices=['auto', 'included', 'excluded'], default='included')
    parser.add_argument('--output', type=Path, default=Path('benchmark-results/live.json'))
    args = parser.parse_args(argv)
    if not args.live or not args.endpoint_name or not args.region:
        parser.error('Live calls require --live, --endpoint-name and --region; use benchmarks.replay offline.')
    if min(args.tasks, args.steps, args.budget, args.max_output_tokens, args.context_window_limit) < 1:
        parser.error('tasks, steps, budget, max-output-tokens and context-window-limit must be positive')
    if args.budget > args.context_window_limit - args.max_output_tokens:
        parser.error('budget must not exceed context-window-limit minus max-output-tokens')
    from statetree.models.sagemaker import SageMakerModel
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {'mode': 'live_retrieval_smoke_suite', 'provider': 'sagemaker',
              'endpoint_name': args.endpoint_name, 'model_id': args.model_id,
              'region': args.region, 'tasks_per_policy': args.tasks, 'steps_per_task': args.steps,
              'context_window_limit': args.context_window_limit, 'max_output_tokens': args.max_output_tokens,
              'statetree_input_estimate_budget': args.budget,
              'note': 'Default UTF-8-byte context estimate; actual tokens come from provider usage. No dollar-cost estimate.',
              'results': []}
    for task_id in range(args.tasks):
        for policy in ('full_history', 'strands_auto', 'statetree'):
            model = SageMakerModel(args.endpoint_name, model_id=args.model_id, region_name=args.region,
                                   profile_name=args.aws_profile, temperature=0.7,
                                   max_tokens=args.max_output_tokens,
                                   context_window_limit=args.context_window_limit,
                                   enable_thinking=args.enable_thinking)
            result = run_case(policy, task_id, args.steps, args.budget, model,
                              args.output.parent, args.cache_convention)
            report['results'].append(result)
            args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
            print(f"{policy} task {task_id}: success={result['success']}, usage={result['usage']}", flush=True)
    totals = {}
    for policy in ('full_history', 'strands_auto', 'statetree'):
        rows = [row for row in report['results'] if row['policy'] == policy]
        successes = sum(row['success'] for row in rows)
        tokens = sum(row['usage']['total_tokens'] for row in rows)
        incomplete = sum(row['usage']['unknown_usage_requests'] + row['usage']['ambiguous_usage_requests'] for row in rows)
        totals[policy] = {'successes': successes, 'attempts': len(rows),
                          'normalized_total_tokens': tokens, 'incomplete_usage_requests': incomplete,
                          'tokens_per_success': tokens / successes if successes and not incomplete else None}
    report['summary'] = totals
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(f'Report: {args.output.resolve()}')


if __name__ == '__main__':
    main()
