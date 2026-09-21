"""Compare context policies on the same fixed trace; no model calls are made."""

import argparse
import json
from pathlib import Path
import tempfile

from statetree.context import ContextBuilder
from statetree.storage.local import LocalStateStore


def compare(steps=30, budget=6000):
    if steps < 1:
        raise ValueError('steps must be positive')
    messages = [{'role': 'user', 'content': [{'text': 'Inspect logs and retain the project constraints.'}]}]
    rows = []
    with tempfile.TemporaryDirectory(prefix='statetree-replay-') as directory:
        store = LocalStateStore(directory)
        full = ContextBuilder(10**9, recent_turns=steps + 1)
        bounded = ContextBuilder(budget, recent_turns=2, archive=store.put_archive)
        for step in range(steps):
            state = {'completed_checks': step, 'constraints': ['Preserve API compatibility']}
            common = {'goal': 'Diagnose the test failures', 'state': state, 'system_prompt': 'Use evidence.'}
            baseline = full.build(messages, **common)
            compact = bounded.build(messages, **common)
            rows.append({'step': step + 1, 'full_history_input_estimate': baseline.estimated_input_tokens,
                         'statetree_input_estimate': compact.estimated_input_tokens,
                         'masked_observations': compact.masked_observations,
                         'dropped_messages': compact.dropped_messages})
            messages.extend([
                {'role': 'assistant', 'content': [{'toolUse': {
                    'toolUseId': f'call-{step}', 'name': 'read_log', 'input': {'step': step}}}]},
                {'role': 'user', 'content': [{'toolResult': {
                    'toolUseId': f'call-{step}', 'status': 'success',
                    'content': [{'text': f'Check {step} passed.\n' + 'repeated diagnostic noise\n' * 50}]}}]},
            ])
        original_total = sum(row['full_history_input_estimate'] for row in rows)
        compact_total = sum(row['statetree_input_estimate'] for row in rows)
        return {
            'mode': 'offline_fixed_trace_replay', 'model_calls': 0,
            'counting_method': 'serialized_utf8_bytes_estimate_not_provider_tokens',
            'steps': steps, 'input_budget_estimate': budget,
            'full_history_cumulative_input_estimate': original_total,
            'statetree_cumulative_input_estimate': compact_total,
            'estimated_input_reduction_percent': round(100 * (1 - compact_total / original_total), 2),
            'archived_objects': len(list(store.archive_dir.glob('*.json'))),
            'limitations': 'Fixed trace only: no model outputs, agent success, billed tokens or costs measured.',
            'calls': rows,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--steps', type=int, default=30)
    parser.add_argument('--budget', type=int, default=6000)
    parser.add_argument('--output', type=Path, default=Path('benchmark-results/replay.json'))
    args = parser.parse_args()
    result = compare(args.steps, args.budget)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key != 'calls'}, indent=2))
    print(f'Report: {args.output.resolve()}')


if __name__ == '__main__':
    main()
