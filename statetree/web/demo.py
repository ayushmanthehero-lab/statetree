"""A fixed seeded example with two live inferences, never estimated usage.

The history and its commit note are authored fixtures, not earlier paid model
calls. This tests context selection on one known fact; it is not a general
benchmark of automatic summarization or project-wide answer quality.
"""

import copy
from pathlib import Path
import re
import subprocess
import tempfile
from time import perf_counter

from strands import Agent

from statetree.memory.commits import CommitNote
from statetree.runtime.runtime import StateTreeRuntime


QUESTION = 'What is the agreed export retention period? Reply only with the number and unit.'
NOTE = 'Export retention is 7 days. Completed export files are deleted after 7 days.'
SYSTEM = ('Answer the fixed project question from recorded facts. Reply only with the requested value and unit. '
          'Do not call tools. Seeded history and caller-written commit notes describe the same fixed project.')
_TURNS = [
    ('Record the export feature requirements. A completed export file stays available for 7 days, then it '
     'is deleted. This is the agreed export retention period, including for files from paid accounts.',
     'Recorded: export retention is 7 days. The download screen should show when the file will expire. '
     'The expiry rule applies to completed files. We are discussing a fixture project; no live data was changed.'),
    ('Review the dashboard layout. The overview should include a heading, two metric cards, a recent activity '
     'list, and a link to the help page. Keep labels short and make the reading order clear on small screens.',
     'The dashboard draft uses a single column on narrow screens and two cards side by side on larger screens. '
     'The activity list sits below the cards. This layout discussion does not change the export retention setting.'),
    ('Consider the loading state for the settings page. We want the save button to stay disabled until the '
     'initial values arrive. Place a small progress indicator beside the page title and preserve the layout.',
     'The settings draft keeps the form visible while initial data loads. Its fields become editable once the '
     'response arrives. Saving has a separate status message. No change was made to stored project requirements.'),
    ('Review the help content. Include an introduction, a short example, a troubleshooting paragraph, and a '
     'contact link. Explain where users can find finished exports and how to read their displayed expiry date.',
     'The help outline is recorded. The introduction explains the export workflow and the example uses a small '
     'sample report. Troubleshooting covers a missing download link. The original expiry policy stays in effect.'),
    ('Check navigation wording. The primary links should be Overview, Exports, Settings, and Help. The active '
     'page needs a visible marker. Test long account names so they do not cover the navigation or the logout link.',
     'Navigation wording is settled. A short underline marks the current page, and long account names can wrap '
     'inside their area. The logout link remains reachable. These presentation decisions do not revise retention.'),
    ('Prepare the review checklist. Cover keyboard navigation, focus visibility, empty states, the loading '
     'indicator, error messages, and the final download link. Keep this checklist separate from the fixed requirements.',
     'The review checklist is ready: keyboard movement, visible focus, clear empty states, loading feedback, '
     'readable errors, and working download links. It is a planning artifact, not a claim that automated tests ran.'),
    ('Set the search behavior for the exports list. Search by report name, ignore letter case, and show '
     'a clear empty state when nothing matches. Put a reset control beside the field so the full list is easy to restore.',
     'The list search matches report names without case sensitivity. Its empty state repeats the search text '
     'and offers a reset control. Searching filters the visible list only; it does not remove files or change their expiry.'),
    ('Choose the timestamp presentation. Display dates in the viewer\'s selected timezone and show the timezone '
     'label beside the time. Keep the underlying event timestamp in UTC so changing presentation does not alter stored events.',
     'Timestamps remain stored in UTC. The interface converts them for display and includes a timezone label. '
     'Changing that display preference leaves the original event and the agreed file retention period unchanged.'),
    ('Describe the notification settings. Users can choose whether to receive an email when an export completes. '
     'Show the current choice on the settings page and provide a short explanation of what the notification contains.',
     'Completion email is an optional preference. Its message contains the report name and a link to the exports '
     'page. The preference affects notifications only. A successful export still appears in the list when email is disabled.'),
    ('Decide how pagination should work. Show twenty exports per page, keep the current search text when '
     'moving between pages, and label the previous and next controls. Hide unavailable actions at the beginning or end.',
     'The list uses twenty items per page and preserves the search text during navigation. Previous and next '
     'controls have clear labels. These list controls affect presentation only and do not revise the stored export policy.'),
]


def fixture_history():
    return [{'role': role, 'content': [{'text': text}]} for turn in _TURNS
            for role, text in zip(('user', 'assistant'), turn)]


def _git(repo, *arguments):
    subprocess.run(['git', '-C', str(repo), *arguments], check=True, capture_output=True,
                   timeout=20)


def _usage(runtime):
    summary = runtime.usage.summary()
    known = summary['requests'] == 1 and summary['known_usage_requests'] == 1
    status = 'known' if known else 'ambiguous' if summary['ambiguous_usage_requests'] else 'unknown'
    return {'status': status, 'requests': summary['requests'],
            **{field: summary[field] if known else None for field in
               ('input_tokens', 'output_tokens', 'total_tokens', 'cache_read_input_tokens')},
            'input_cache_convention': 'included',
            'raw_provider_usage': [record['usage'] for record in runtime.usage.records()]}


def _strategy(model, *, compact):
    with tempfile.TemporaryDirectory(prefix='statetree-web-') as directory:
        repo = Path(directory)
        _git(repo, 'init', '-q')
        _git(repo, 'config', 'user.name', 'StateTree fixed demo')
        _git(repo, 'config', 'user.email', 'demo@statetree.invalid')
        (repo / 'README.md').write_text('Disposable seeded export-retention fixture.\n', encoding='utf-8')
        _git(repo, 'add', 'README.md')
        _git(repo, 'commit', '-qm', 'Initialize demo fixture')
        agent = Agent(model=model, context_manager=False, callback_handler=None, system_prompt=SYSTEM)
        agent.messages[:] = copy.deepcopy(fixture_history())
        runtime = StateTreeRuntime(agent, goal='Recall export retention', repo_path=repo,
                                   max_input_tokens=3600 if compact else None, recent_turns=1,
                                   commit_context_budget=1500 if compact else 0,
                                   input_cache_convention='included')
        checkpoint = runtime.commit(note=CommitNote(summary=NOTE)) if compact else None
        started = perf_counter()
        try:
            result = runtime.run(QUESTION, new_task=compact, limits={'turns': 1})
            output = ''.join(block.get('text', '') for block in result.message['content']).strip()
            passed = bool(re.fullmatch(r'7\s+days[.!]?', output, flags=re.IGNORECASE))
            status = 'completed'
            error = None
        except Exception:
            # SDK exceptions can contain upstream URLs/bodies. Never return them
            # through a public API. Preserve whatever usage the hook recorded.
            output, passed, status = '', False, 'error'
            error = 'Inference did not complete. Check the local model, tunnel, gateway and AWS logs.'
        context = runtime.hooks.last_context
        return {'status': status, 'output': output, 'passed': passed, 'error': error,
                'elapsed_seconds': round(perf_counter() - started, 3), 'usage': _usage(runtime),
                'selected_commit_ids': context.selected_commit_ids if context else [],
                'commit_id': checkpoint.id if checkpoint else None}


def compare_results(baseline, optimized):
    known = all(item['usage']['status'] == 'known' for item in (baseline, optimized))
    passed = baseline['passed'] and optimized['passed']
    original = baseline['usage']['input_tokens'] if known else None
    saved = original - optimized['usage']['input_tokens'] if known else None
    return {'status': 'task_failed' if not passed else 'measured' if known else 'usage_unavailable',
            'both_tasks_passed': passed, 'input_tokens_saved': saved,
            'input_reduction_percent': round(100 * saved / original, 2) if original else None,
            'note': 'One fixed seeded example. Counts include cached input tokens; this is not a billing or speed comparison.'}


def run_demo(model_factory):
    """Use separate clients with identical configuration for the two requests."""
    baseline = _strategy(model_factory(), compact=False)
    optimized = _strategy(model_factory(), compact=True)
    return {'schema_version': 1,
            'fixture': {'id': 'export-retention-v2', 'title': 'Remember a project decision',
                        'question': QUESTION, 'expected_answer': '7 days', 'commit_note': NOTE,
                        'history_origin': 'Fixed, caller-authored seeded conversation; not generated or charged in this run.',
                        'history_messages': len(fixture_history()),
                        'commit_note_origin': 'Fixed caller-authored note from the same facts; no summarization model call.',
                        'scope': 'Two real model calls on one known fact; not a general savings benchmark.'},
            'model': {'id': 'Qwen/Qwen3.5-4B', 'temperature': 0, 'max_output_tokens': 64,
                      'enable_thinking': False, 'inference_location': 'Operator PC'},
            'baseline': baseline, 'statetree': optimized,
            'comparison': compare_results(baseline, optimized)}
