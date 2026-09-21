**StateTree local MVP implementation plan — 20 September 2026**

Goal: implement the research guide's first five recommendations in the existing workspace: reliable local checkpoints, cumulative usage accounting, bounded context, and a reproducible comparison.

Architecture: immutable JSON manifests/blobs plus SQLite branch references; Git workspace snapshots; a pure deterministic context builder; a persistent usage ledger; Strands hooks connecting context and per-call provider usage. Local tests and a synthetic benchmark require no API credentials. A live comparison is explicitly opt-in.

The user authorized implementation of the recommendations. Work directly in the requested checkout, preserve the existing research guide, and leave changes reviewable without committing or publishing. Use Python 3.11+ and standard-library unittest; use the installed Strands 1.56 SDK for integration. No paid calls or dependency downloads are required.

- [x] Checkpoint kernel: regress duplicate-keyword construction; hash final manifest and snapshot content; immutable blob writes; SQLite expected-parent transactions and per-branch refs; validate before restore. Tests cover tampering, stale writers and restart.
- [x] Workspace snapshots: capture tracked and nonignored untracked files with explicit exclusions; preserve staging separately; pin Git objects; reject unsafe restores. Tests use temporary repositories only.
- [x] Usage ledger: idempotent request IDs; raw provider counts plus normalized input/output/cache counters; phase/run/branch attribution; report missing usage explicitly. Tests cover cache conventions, repeat delivery and conflicting request records.
- [x] Context builder: archive original tool results; mask older observations; retain goal, constraints, state and recent valid message groups; fit an explicit budget or raise. Tests cover Unicode, oversized pinned content, message pairing and input immutability.
- [x] Strands integration: register per-model hooks for preparation and usage; avoid accumulating state prefixes; persist usage even for discarded/retried calls when provided; document unsupported paths.
- [x] Benchmark and examples: no-network fresh-process recovery example and deterministic context-policy comparison; clearly label estimates; provide optional live runner with real usage and task checks.
- [x] Verification: full unittest suite (61 tests passed), example, benchmark and independent review; limitations and commands are in README.

Independent module contracts:

* `ContextBuilder(max_input_tokens, recent_turns=4, counter=None, archive=None).build(messages, *, goal, state=None, system_prompt='', tool_specs=None)` returns a `ContextResult` with `messages`, `system_prompt`, `estimated_input_tokens`, `masked_observations`, `dropped_messages`. `counter` accepts serialized text and returns an integer; default conservative UTF-8-byte estimate is labeled, not advertised as provider tokens. `archive` accepts a JSON-serializable object and returns a stable ID. `ContextBudgetError` rejects inputs that cannot fit safely.
* `UsageLedger(path).record(request_id, *, run_id, branch, phase='agent', model='', usage=None, status='ok')` stores request-level raw usage exactly once; conflicting reuse raises. `summary(run_id=None, branch=None)` returns aggregate counters. No inferred usage is presented as actual.
* `GitWorkspace(path, exclude_paths=()).snapshot()` returns a pinned commit SHA containing working-tree and index trees. `validate(sha)` checks the checkpoint before writes. `restore(sha)` restores working-tree content and original index, including an explicit safe policy for newly introduced files. Normal branch/HEAD must stay unchanged.

Review focus: incomplete checkpoint publication; stale branch writers; unknown provider usage; context masking across tool pairs; restore collisions with unrelated untracked files. Use minimal implementations and regression tests for real failure modes.

Implementation decisions: the local MVP rejects unsupported Git index flags rather than risking silent data loss. It preserves unrelated later untracked files rather than deleting them. Recovery checkpoints and canonical verified state have separate references. A cumulative usage limit stops between calls and is not a strict billing cap. The live benchmark is a small matched retrieval suite; a larger coding evaluation remains future work. No API calls, dependency downloads, commits or publication were performed during implementation.

Independent review found stale snapshots under index flags and archive-reference chains under repeated compaction. Both have targeted regressions and were corrected. Additional regressions cover leading-space filenames and directory collisions. The Strands integration is exercised through its real event loop with a scripted local provider, including retry and tool-use paths.
