# StateTree GTX 1650 update — implementation and verification

Date: September 21, 2026. Package version: **0.3.1**.
Input: the previously delivered `statetree-completed.zip`, not a fresh GitHub clone.
Local baseline: `2840761201ca858942adc6e93b1eed7288bf970f` (an upload-derived local commit, not a remote commit).

## Delivered behavior

Added `statetree/local_gpu.py`, `statetree/gpu_download.py`, PowerShell setup/start
wrappers, a GTX 1650 example configuration, tests, and a setup guide. The README
links to the new workflow. Existing AWS/deployment code is unchanged.

Setup installs pinned Windows x64 CUDA 12.4 llama.cpp binaries and the pinned
Unsloth Qwen3.5-4B Q4_K_M GGUF. SHA-256 checks precede download publication;
ZIP paths are validated before extraction. Existing operator-supplied assets
are supported and labeled custom rather than falsely claiming pinned provenance.
Assets stay in a separate per-user cache by default.

Start detects a CUDA backend, chooses the GTX 1650 when enumerated, loads the
local model with automatic GPU/RAM fitting, checks positive GPU layer-offload
logs, validates authenticated readiness and the advertised model ID, then starts
the existing StateTree workbench. A new ephemeral model key is kept out of argv
and project configuration. The launcher stops its owned child on normal exit or
handled errors. It does not kill an existing process to free a port.

Default context is 4096, output cap 256, one sequence, microbatch 64, fit margin
768 MiB, and text-only/non-thinking mode. Runtime fit and speed are **not measured**.
Existing project facts, goal, run ID, token threshold and verification commands
are preserved. A changed model binding uses an explicit public-state checkpoint.
Context budgets are tightened without raising existing stricter settings.

`LocalClient` gained a validated optional HTTP timeout (default still 55 seconds).
Only the local project bridge reads `STATETREE_LOCAL_TIMEOUT`; the combined GPU
launcher sets it to 300 seconds while it runs. No paid-provider fallback was added.

## Executed checks

| Check | Actual result |
|---|---|
| Initial affected-project baseline | 27 passed; see `validation/gpu/baseline.log` |
| Initial red test run | 17 expected failures for missing launcher/downloader/timeout |
| Final GPU tests | **26 passed, 23 subtests passed**, 3.18 seconds |
| Plain `python -m pytest -q` | **11 collection errors**, exit 2, missing `strands` |
| Final full suite, continuing past collection errors | **274 passed, 15 failed, 5 skipped, 11 collection errors, 231 subtests passed**, 64.25 seconds, exit 1 |
| Python compilation / AST checks | Passed |
| Existing workbench JavaScript syntax check | Passed (`node --check`) |
| Wheel build (`--no-deps --no-build-isolation`) | Passed; version 0.3.1 |
| Isolated wheel install (`--no-deps`) and launcher dry run outside source | Passed; not a dependency-complete installation |

Archived logs have ANSI escapes and trailing whitespace removed; results are unchanged.
Test totals overlap and must not be added. The final full-suite log includes the
new tests. A preliminary local-core run was interrupted by the tool timeout;
its partial log is retained and **not claimed as a passing run**. The completed
full-suite run above provides the broader observed results.

The new lifecycle tests execute a real subprocess and real loopback HTTP, but
that subprocess is an explicitly authored test server. Its CUDA/offload strings
are fixtures, **not evidence that a GPU or model was used**. One orchestration
test bypasses only the SDK-presence gate; it installs no fake Strands module and
makes no real model request. SHA/download unit tests use byte fixtures. Actual
multi-gigabyte asset downloads were not executed in this network-restricted container.

## Full-suite failures and collection errors

All 15 failures and 11 collection errors trace to the missing real `strands`
module. The process-death case reports an exit-code assertion because its child
fails the import before reaching the injected death point. These are not silently
marked passing; installing the genuine dependency may reveal further defects.

- `FAILED tests/test_chat_api.py::ChatHTTPTests::test_application_rejects_reused_benchmark_credential_for_private_chat`
- `FAILED tests/test_chat_api.py::ChatHTTPTests::test_local_application_keeps_chat_durable_and_legacy_benchmark_available`
- `FAILED tests/test_kernel.py::KernelTests::test_branch_heads_are_independent`
- `FAILED tests/test_kernel.py::KernelTests::test_commit_restore_in_fresh_process`
- `FAILED tests/test_kernel.py::KernelTests::test_process_death_before_publication_commit_does_not_advance_refs`
- `FAILED tests/test_kernel.py::KernelTests::test_restore_rewinds_branch_parent`
- `FAILED tests/test_kernel.py::KernelTests::test_saved_manifest_is_verified_on_read`
- `FAILED tests/test_kernel.py::KernelTests::test_snapshot_tampering_rejected_before_workspace_changes`
- `FAILED tests/test_kernel.py::KernelTests::test_stale_runtime_cannot_overwrite_newer_head`
- `FAILED tests/test_kernel.py::KernelTests::test_unverified_progress_advances_branch_not_canonical`
- `FAILED tests/test_worker_chat.py::WorkerChatTests::test_buffered_events_over_one_batch_flush_after_worker_restart`
- `FAILED tests/test_worker_chat.py::WorkerChatTests::test_cancellation_after_local_completion_cannot_leave_task_claimed_forever`
- `FAILED tests/test_worker_chat.py::WorkerChatTests::test_checkpoint_is_visible_before_the_runner_finishes`
- `FAILED tests/test_worker_chat.py::WorkerChatTests::test_new_worker_resumes_same_task_and_receives_persisted_checkpoint`
- `FAILED tests/test_worker_chat.py::WorkerChatTests::test_worker_executes_real_http_model_relay_events_and_owner_apply`
- `ERROR tests/test_agent_runner.py`
- `ERROR tests/test_bedrock.py`
- `ERROR tests/test_benchmarks.py`
- `ERROR tests/test_commit_context.py`
- `ERROR tests/test_handoff.py`
- `ERROR tests/test_hybrid_demo.py`
- `ERROR tests/test_portable.py`
- `ERROR tests/test_sagemaker.py`
- `ERROR tests/test_sagemaker_example.py`
- `ERROR tests/test_strands_adapter.py`
- `ERROR tests/test_workflow.py`

## Review and limitations

A separate self-review checked API signatures, real endpoint authentication
against the pinned llama.cpp source, Windows argument handling, state preservation,
custom-asset provenance and child-process cleanup. No independent reviewer or
security audit was available. New regression tests exposed and verified fixes for
the project model-binding call, keyword-only workbench port, and custom provenance.

**Not executed here:** Windows or PowerShell, the NVIDIA driver, CUDA binaries,
actual Qwen inference/tool use, live token savings, model speed/accuracy, AWS or
GitHub changes. The container has no GTX 1650 and no access to the user's PC.
The full SDK suite is not green. Run the setup and launcher on the user's Windows
machine to perform the actual CUDA readiness checks, then send a real chat prompt.

Only normal shutdown/handled exceptions guarantee child cleanup; killing the
parent externally can leave a process. The local key is ephemeral, so an unrelated
standalone StateTree process will not automatically authenticate to this server.
The workbench remains archive-tool-only, not an unrestricted coding agent.

See `GTX1650_SETUP.md` for instructions, troubleshooting and primary-source links.
Existing 0.3.0 reports in this repository describe the earlier delivery, not this
update. The uploaded original archives were not modified. Nothing was pushed or
deployed into the user's account.
