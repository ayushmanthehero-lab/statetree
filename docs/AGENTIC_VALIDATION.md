# StateTree Agent tasks — implementation and verification

Date: 2026-09-21. This is a source update to the supplied GTX 1650 project, with
both earlier `gpu_download.py` and `local_gpu.py` fixes retained. It has not been
installed on the user's PC, pushed to GitHub or deployed to AWS.

Local baseline: `697a3c4be437e243c9cf82e3c91e53e0cd4d1e6f` (created from the uploaded ZIP plus those fixes,
not a claim about the remote repository). Working branch: `work/agentic-resume`.
Package metadata remains 0.3.1; this update introduces no runtime dependency.

## Implemented

- SDK-independent local llama.cpp tool transport with tools included in formatted
  prompt token preflight, thinking disabled and one model sequence at a time.
- Eight tools: list/read/search/create/edit project files, approved native argv
  execution, saved planning and paginated receipt retrieval.
- SQLite task identity, normalized-prompt deduplication, pending model replies,
  pre-effect intents, immutable operation receipts, usage records and event log.
- Automatic Git-backed Project checkpoint after each receipt, with retained
  evidence and program-generated continuation notes. No separate summarizer call.
- Compact model input: original task, tool/system instructions, bounded note and
  latest result. Full transcript/history is not replayed; older evidence is paged.
- File-effect reconciliation by before/after hashes and receipt-based command
  replay suppression. Independent command worker survives coordinator death.
- Persistent uncertainty guard and explicit operator reconciliation for unknown
  or partial command effects. Resume alone cannot bypass that review.
- Authenticated Agent tasks UI, independent task worker, saved tasks, event polling,
  pause/resume, native execution consent, current usage/head updates and lock clearing.
- Source-only installer with known-source checks, checksum verification, external
  backup, private-path protection, refusal of unknown local edits and repeat-safe apply.
- Existing archive chat/cloud worker/GPU setup retained. Fixed empty-tracked-tree
  Git restore pathspec error exposed by the new checkpoint-rewind regression test.

## Actual validation

Executed on Linux with Python 3.13.5, Git, Node and Chromium.
No real Qwen model or Windows GPU was used. Scripted model replies are test inputs,
not measurements of model skill, speed, accuracy, or token savings.

| Check | Observed result |
|---|---|
| Eight new Agent tasks test modules, fresh release run | **47 passed**, 21.31 s, exit 0 |
| Full suite, `python -m pytest --continue-on-collection-errors -q` | **321 passed, 15 failed, 5 skipped, 11 collection errors; 231 subtests passed**, 105.60 s, exit 1 |
| Baseline full suite before this implementation | **274 passed, 15 failed, 5 skipped, 11 collection errors; 231 subtests passed**, exit 1 |
| Actual process-death/reconciliation and browser tests together | **4 passed**, 12.27 s |
| Updated dashboard header plus HTTP/legacy workbench regression checks | **13 passed**, 14.26 s |
| Python compileall and both JavaScript syntax checks | exit 0 |
| Python wheel build, no dependencies fetched | exit 0 |

These runs overlap; passed counts must not be added. The new modules are
`test_agentic_store_tools.py`, `test_agentic_engine.py`, `test_agentic_transport.py`,
`test_agentic_workbench.py`, `test_agentic_process_recovery.py`,
`test_agentic_windows_job.py`, `test_agentic_browser.py` and
`test_agentic_update_installer.py`.

Evidence is in [validation/agentic](validation/agentic/), including the complete
[release log](validation/agentic/full-release.log),
[targeted log](validation/agentic/targeted-release.log), and red-to-green records.

### What those checks exercised

The process-death test actually launched a separate coordinator, let a native
Python command change a counter, killed the coordinator, and resumed with a fresh
runner. The counter remained **1**; the original worker supplied its saved command
receipt and the new runner checkpointed it without repeating the effect.
Other tests cover a network failure after a completed write, a simulated crash
between receipt and checkpoint, two saved pending tools, truncated model output,
immutable receipts, rewound checkpoints, uncertain outcomes, permission consent,
private/traversal/symlink paths and idempotent source updates.

The Chromium test fills the new task form, runs scripted tool requests against
real files and a real Python subprocess, observes checkpoints, resends the task,
and checks lock clearing plus 1440px/390px document widths. As in earlier builds,
managed Chromium blocks direct loopback navigation here: the test renders local
assets in memory and bridges fetch through Python to the real authenticated HTTP
server. It is not a direct same-origin navigation test. Separate direct HTTP tests
exercise authentication, strict fields, status/pause responsiveness and restart.

Windows job-object API binding and failure-handling tests use a fake kernel API;
they are **not Windows process-lifecycle executions**. Native Windows behavior,
PowerShell installation, CUDA model startup and actual llama.cpp/Qwen tool choices
still require testing on the user's machine. The direct transport contract test
uses a real local HTTP test server, not a loaded model. No fake Strands SDK was
provided to make legacy tests pass.

### Full-suite failures, not hidden

The 15 failures and 11 collection errors still trace to the absent `strands`
package in this container. The legacy process-death test has an exit-code assertion
failure because its child fails the Strands import before reaching its injected
exit point. Missing dependencies can conceal further integration defects; this is
not a green full-suite or production-readiness claim.

Failed tests:

- `tests/test_chat_api.py::ChatHTTPTests::test_application_rejects_reused_benchmark_credential_for_private_chat`
- `tests/test_chat_api.py::ChatHTTPTests::test_local_application_keeps_chat_durable_and_legacy_benchmark_available`
- `tests/test_kernel.py::KernelTests::test_branch_heads_are_independent`
- `tests/test_kernel.py::KernelTests::test_commit_restore_in_fresh_process`
- `tests/test_kernel.py::KernelTests::test_process_death_before_publication_commit_does_not_advance_refs`
- `tests/test_kernel.py::KernelTests::test_restore_rewinds_branch_parent`
- `tests/test_kernel.py::KernelTests::test_saved_manifest_is_verified_on_read`
- `tests/test_kernel.py::KernelTests::test_snapshot_tampering_rejected_before_workspace_changes`
- `tests/test_kernel.py::KernelTests::test_stale_runtime_cannot_overwrite_newer_head`
- `tests/test_kernel.py::KernelTests::test_unverified_progress_advances_branch_not_canonical`
- `tests/test_worker_chat.py::WorkerChatTests::test_buffered_events_over_one_batch_flush_after_worker_restart`
- `tests/test_worker_chat.py::WorkerChatTests::test_cancellation_after_local_completion_cannot_leave_task_claimed_forever`
- `tests/test_worker_chat.py::WorkerChatTests::test_checkpoint_is_visible_before_the_runner_finishes`
- `tests/test_worker_chat.py::WorkerChatTests::test_new_worker_resumes_same_task_and_receives_persisted_checkpoint`
- `tests/test_worker_chat.py::WorkerChatTests::test_worker_executes_real_http_model_relay_events_and_owner_apply`

Collection errors:

- `tests/test_agent_runner.py`
- `tests/test_bedrock.py`
- `tests/test_benchmarks.py`
- `tests/test_commit_context.py`
- `tests/test_handoff.py`
- `tests/test_hybrid_demo.py`
- `tests/test_portable.py`
- `tests/test_sagemaker.py`
- `tests/test_sagemaker_example.py`
- `tests/test_strands_adapter.py`
- `tests/test_workflow.py`

## Review findings and limits

A separate self-review pass found and fixed: preflight-only requests being counted
as unknown generation usage; unresolved commands disappearing behind a 200-task
history window; dashboard header usage staying stale; and uncertainty being
bypassed on resume. There was no independent reviewer or security audit.

Native code is explicitly approved per task and runs with the user's OS privileges;
it is not a filesystem/network sandbox. Windows jobs and POSIX process groups are
lifetime controls. Tool path guards do not limit what executed code can do. Avoid
simultaneous external edits/CLI mutations to the same project. The configured
model endpoint is literal loopback only, with no redirect or proxy fallback.

Normal checkpoint/receipt recovery is automatic. Unacknowledged external effects
cannot be inferred reliably from a note: unknown or partial commands require an
operator observation, recorded separately from original evidence. Replay suppression
is by persisted operation ID, not a proof that a model cannot choose a similar new
command. Task completion is a model report; inspect exit codes and tests.

Each activation pauses after 100 model preparations; Resume preserves progress.
Commands default to 60 seconds (maximum 600), with 32 KiB output capture. Prompt
preflight includes tools and reserves response space; oversized requests pause
instead of silently dropping their objective. Cut-off tool calls are not executed.
Budget checks are between-call thresholds, not hard billing caps. Unknown usage
stays unknown. No measured energy, water, latency or token-saving claim is made.

The journal survives project checkpoint rollback and is not in portable public-state
bundles. Keep the stopped project's complete `.statetree` directory for recovery.
The installer backs up source only, not project memory; it is not a full-system
backup or crash-atomic multi-file transaction. Unknown local source modifications
are refused before writes.

## Primary implementation references

The pinned llama.cpp `tools/server/server-context.cpp` at b11064 routes
`/apply-template` through the same chat-parameter parser as chat completions, then
`/tokenize` provides token IDs. Microsoft documents Windows job membership,
child inheritance and kill-on-last-handle-close behavior. These references inform
the implementation; they are not live validation of the user's machine.

- https://raw.githubusercontent.com/ggml-org/llama.cpp/b11064/tools/server/server-context.cpp
- https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects

See [AGENTIC_TASKS.md](AGENTIC_TASKS.md) for installation, ordinary recovery,
uncertain-command review, execution permissions and operating limits.
