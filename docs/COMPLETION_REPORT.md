# StateTree 0.3.0 — implementation and verification report

## Delivery status

The supplied project has been extended in place with an integrated Project API,
JSON CLI, authenticated local workbench, reproducible local demo, regression tests,
operator documentation and a CI workflow. The ten numbered README features are
mapped to implementations and their limits in [FEATURE_MATRIX.md](FEATURE_MATRIX.md).
The original runtime, cloud app, deployment templates and provider/framework
adapters have been retained rather than replaced with a toy application.

**The full test suite is not green in this build environment.** Actual local
component checks passed; the missing real Strands SDK prevented complete SDK
validation. This delivery is source with a verified local workflow, not a claim
of production readiness or completed cloud/model validation.

Input archive: `statetree-main.zip`.
Input SHA-256: `04534388d52df079e77e20caf44bc4235eacdb1dc8d2b1a3877cc696cd64f6f3`.
Local baseline commit: `6b54d11` (created from the uploaded ZIP, not a remote SHA).
Working branch: `work/statetree-completion`. No remote push, merge or deployment.
The original upload has not been edited. Git history, local runtime state,
credentials, virtual environments and model weights are not in the source ZIP.

## What changed

- Added `Project` and an explicit public-session snapshot format, joining existing
  checkpoints, facts, commit recall, bounded context, portable archives and usage.
  Native runtime callers retain the actual Strands snapshot decoder by default.
- Added CLI commands and the `statetree` package entry point. Local state operations
  do not need to import the inference SDK. Actual `ask` imports real Strands on
  demand and fails clearly when it is missing; no fabricated model fallback exists.
- Added a separate loopback workbench with timeline/evidence, memory, branches,
  context previews, chat, portable bundles and usage. Bearer tokens, local Host/
  Origin validation, strict JSON, stale-state checks and explicit restore/adopt
  confirmation protect the local mutation endpoints. This is not a public service.
- Added persistent branch-candidate reopening and compare-and-swap checks; configured
  command verification and explicit main-workspace adoption use actual Git worktrees.
  Verified source files, main divergence and the original staged index are checked.
- Fixed Git snapshots when default state paths are ignored, without adding ignored
  secrets; preserved eligible literal filenames/deletions and source index state.
- Added initialization publication-gap recovery, bounded active archive references,
  retained archive chains, dependency-aware memory lifecycle, portable model-config
  validation, and safe handling of foreign historical branch receipts on import.
- Added a lazy local OpenAI-compatible transport bridge using the existing real
  Strands hooks/model adapter. Its protocol tests are included but skipped here
  because the SDK is absent. The local chat has an archive-reading tool, not an
  unrestricted shell or autonomous code-execution tool.
- Added the zero-model end-to-end demo, example configuration, Python wheel metadata,
  test runner, full-suite CI configuration and detailed startup/operation guides.

## Executed validation

Environment: Python 3.13.5, pytest 9.0.2, Git, Node 22.16.0, Linux. The installed
boto3 is 1.43.18; this does not validate the declared optional cloud dependency floor.
The actual `strands-agents==1.56.0` dependency was unavailable. Both normal package
installation and a direct-download attempt failed because of runtime networking.
No SDK substitute was created. Windows instructions are provided but were not
executed on Windows.

| Check | Observed result | Evidence |
|---|---|---|
| `python scripts/test_core.py` | **235 passed, 5 skipped, 7 deselected; 198 subtests passed**, 58.87 s, exit 0 | [Local-core log](validation/local-core-tests.log) |
| `python -m pytest --continue-on-collection-errors -q` | **248 passed, 15 failed, 5 skipped, 11 collection errors; 208 subtests passed**, 62.87 s, exit 1 | [Full-suite attempt](validation/full-suite-attempt.log) |
| Standard `python -m pytest -q` attempt | Stopped at 11 missing-SDK collection errors; the continued run above exposes the remaining runnable tests | Not represented as a passing run |
| Real loopback HTTP workbench checks | 8 passed in the targeted run; included in the core/full runs | `tests/test_workbench.py` |
| Chromium DOM, form and responsive-layout check | 1 passed in the targeted run; also included in core/full runs | `tests/test_workbench_browser.py`, screenshots below |
| `python -m compileall -q statetree deploy benchmarks examples tests` | Exit 0 | Final static-check log |
| `node --check statetree/web/static/workbench.js` | Exit 0 | Final static-check log |
| Wheel build, no dependencies fetched | Exit 0; installable `statetree-0.3.0-py3-none-any.whl` produced | [Build log](validation/wheel-build.log) |
| Isolated-target wheel installation with `--no-deps` | Exit 0; package structure smoke check, **not** a dependency-complete installation | [Install log](validation/wheel-install-smoke.log) |
| Installed-wheel `doctor` and local demo, outside the source checkout | Exit 0; all 7 checks true, 0 provider requests | [Doctor](validation/wheel-doctor.json), [Demo](validation/local-demo-report.json) |

Archived text logs have trailing whitespace normalized; results and tracebacks are unchanged.
These runs overlap; their passed/subtest counts must not be added together.
The local-core script explicitly selects SDK-independent modules and deselects
seven legacy chat/worker cases that instantiate Strands indirectly. It is not
an alternative definition of the full suite. Five skips are the three real-SDK
bridge cases and the two optional framework integrations. Missing dependencies
may hide further integration defects; those paths still require a successful
real-dependency run. Tests were not changed to pretend a missing SDK exists.

### Full-suite failing test names

All failures below trace to `ModuleNotFoundError: No module named 'strands'`.
The process-death test reports an expected-exit-code mismatch because its child
fails that import before reaching its injected death point.

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

### Modules with collection errors

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

### Browser scope

The managed Chromium policy rejected direct navigation to loopback with
`ERR_BLOCKED_BY_ADMINISTRATOR`. The executed browser test uses an in-memory local
DOM and an explicit Python HTTP bridge to the **real authenticated loopback
server**. It checks actual forms, persisted checkpoint/context responses,
JavaScript errors, token-lock clearing and desktop/mobile horizontal overflow.
Separate direct HTTP tests exercise authentication, origin/host rules and
mutation guards. Browser policy was not disabled. This is not a claim that
normal same-origin navigation was exercised in this environment.

[Desktop capture](validation/workbench-desktop.png) ·
[Mobile capture](validation/workbench-mobile.png).

### Installed-wheel demonstration

The freshly built wheel was installed into an isolated target with `--no-deps`
and run from outside the source directory. The demo genuinely exercised
checkpoint/restore, fact recall, evidence preservation, completed-step recovery,
public-state model binding, verified speculative merging and portable round trip.

The authored comparison recorded 17,443 serialized bytes for the full fixture,
5,414 for bounded-history context and 2,578 for a new-task recalled request.
**These are UTF-8 byte estimates, not provider token measurements.**
`provider_requests` is 0, `provider_tokens` is null and model task success is null.
The handoff checks public data/binding preservation without invoking either model.
Strategy scores are declared fixture values, not model benchmark results.
The README's pre-existing measured Qwen result was preserved, not rerun.

Wheel SHA-256: `5686f33c74fe700fbb31f9f77eb2cf279173c6dbb9d5e6f947710865f61b45a2`.

## Review and design decisions

The final review was a separate self-review by the implementing assistant; there
was no independent reviewer or security audit. Red-to-green regression checks
were executed for the new facade, branch reopening, CLI, ignored-path snapshots,
index-preserving adoption, workbench controls, hardening and portable receipts.

Public-state-only operations use an explicit snapshot adapter rather than a fake
SDK. The cost of this boundary is that the new `Project` checkpoints and native
SDK session checkpoints are deliberately different formats; public ASP bundles
are the supported interchange boundary.

The workbench is separate from the existing cloud frontend. Consequently, running
it does not update the public AWS demo. Local chat is deliberately archive-tool
only; existing native APIs/private worker remain the path for configured coding
tools. Verification executes operator-trusted argv commands in clean worktrees,
not untrusted commands in a security sandbox.

Branch-coordinator canonical revisions remain distinct from the runtime checkpoint
head. Adoption is explicit, checks stale main state and files, and preserves the
source index. The coordinator is initially seeded once; it does not silently
rebase every subsequent fork on arbitrary main edits. See the branch section of
[PROJECT_WORKBENCH.md](PROJECT_WORKBENCH.md) before operating a long-lived project.

Rollback does not erase recorded provider usage, durable external effects, archive
history or the last verified reference. Memory `delete` affects current active
state, not physical erasure of immutable historical evidence. Portable bundles
include declared public state and evidence, not a backup of Git history, billing
records, credentials, private model state or every workspace file.

No unreported failed checks have been treated as passing. Cosmetic restructuring
of the legacy cloud UI was intentionally not undertaken.

## Remaining validation and operational boundaries

Actual Qwen responses, full Strands/native handoff behavior, optional LangGraph/
CrewAI integration, provider token reductions, Windows execution, Docker workers
and AWS deployment were **not validated live** here. Nothing was pushed to GitHub
or installed/deployed into the user's machine/account. The source provides a CI
workflow for a real-dependency full run; that remote workflow has not run here.

Token limits are between-call thresholds using observed usage, not guaranteed
billing caps. Unknown usage remains unknown and can block subsequent budgeted
requests. Byte estimates and fixtures do not establish accuracy, energy, water,
latency or financial savings. No paid-provider fallback is enabled.

Before treating this as a production release, install the declared real dependencies
and run `python -m pytest -q`. Then exercise a chosen actual model endpoint and any
cloud deployment separately with the appropriate credentials and independent
review. Startup does not perform those external actions automatically.

## Start here

[PROJECT_WORKBENCH.md](PROJECT_WORKBENCH.md) includes PowerShell and POSIX setup,
CLI examples, branch workflow, model binding, memory/portable semantics and the
reproducible demo. [FEATURE_MATRIX.md](FEATURE_MATRIX.md) maps every numbered
README feature to its implementation and evidence boundary.
