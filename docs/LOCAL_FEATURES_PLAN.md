# StateTree local feature completion

The user authorized coding the six feature areas and deferred AWS setup and live cloud runs. Extend the existing checkout without commits or publication. Preserve the current checkpoint API and its tests.

## Design and constraints

Python 3.11+, the installed Strands SDK, Git, and SQLite. No cloud calls. Framework-specific objects, credentials, open processes, and private model state do not belong in the portable format. A portable handoff carries explicit application state and references to evidence, with a bounded prompt projection. Version-1 local checkpoints remain readable.

Portable state is a validated JSON AgentState: schema_version, goal, constraints, subgoals, facts, pending_actions, resources, artifacts, memory, checkpoints, extensions. Unknown required capabilities fail closed. Hash-verified bundles carry state plus referenced archive blobs. Import is explicit and never rewrites the source workspace. Same-framework opaque snapshots remain separate from this format.

Recovery uses a persisted tool-step journal and stable operation identifiers. Supported tool handlers must declare read-only, idempotent, or at-most-once behavior. Uncertain non-idempotent effects require reconciliation; no arbitrary exactly-once guarantee is made. Local steps and scripted models provide offline verification.

Memory policies operate on structured observations with evidence and dependency versions. Superseded and invalidated facts leave the active prompt; historical checkpoint data remains immutable. Uncertain semantic claims are not automatically declared true.

Parallel branches receive separate Git worktrees and application state. Git worktrees are file isolation, not security or external-service isolation. Resource reads/writes are declared and validated conservatively. File merges and state merges require conflict checks and independent verification before promotion. Speculative workers must not perform uncontrolled external side effects.

## Tasks and file ownership

- [x] Portable state, bundles, adapters, and model handoff (controller): core/state.py, storage/portable.py, adapters/portable.py, runtime/handoff.py, runtime/runtime.py; tests/test_portable.py, tests/test_handoff.py, tests/test_optional_adapters.py. Verified malformed schemas, hashes/references, fresh-process transfer, failed binding rollback, bounded handoff, and accumulated usage. Real LangGraph 1.2.11 and CrewAI 1.15.22 continuation tests passed in a separate environment.
- [x] Durable local runner (recovery worker): runtime/durable.py and tests/test_durable.py; runtime/workflow.py and tests/test_workflow.py add automatic aligned checkpoint recovery. Covered cached replay, argument changes, interrupted effects/checkpoint publication, reconciliation, concurrent claims, and no automatic repeat of uncertain unsafe effects.
- [x] Fact lifecycle (memory worker): memory/facts.py, memory/__init__.py, tests/test_memory.py. Covered repeated, superseded and dependent observations, tombstones, evidence retention, serialization, JSON type distinctions and bounded active facts.
- [x] Isolated branches, resource conflicts, and speculative execution (branch worker): workspace/branches.py, runtime/parallel.py, tests/test_branches.py, tests/test_parallel.py. Covered real worktrees, independent edits, stale dependencies/CAS, conservative integration, verifier mutation/failure/all-fail, and retained loser usage. Also fixed the shared Git index-stat-cache defect with an equal-size/timestamp regression.
- [x] Integration, runnable local example, documentation, independent review, and full verification (controller). All 143 distinct tests passed across the main and optional-framework environments; both local demos passed. AWS/live runs remain deferred.

## Verification gates

Each worker writes and observes a failing behavior test before implementation, then runs targeted tests. Integration uses actual local components and subprocesses. A final review covers persistence boundaries, path handling, retained evidence, and stale promotion. The final full suite is run once all modules settle. Do not equate scripted-provider counts with actual model savings.

## Execution decisions

Work in the user-requested checkout because the MVP is uncommitted there. Product worktrees are exercised only in temporary test/example repositories. Parallel implementation is limited to disjoint file ownership and explicit JSON interfaces. Reuse the existing usage ledger; storage accounting must not rewind with checkpoints. No dependency install or AWS configuration is needed for the core local implementation.

## Progress

Original 61-test baseline passed. Local feature demo passed all six areas with zero model requests/AWS calls. Independent reviews are in PORTABLE_REVIEW.md and LOCAL_FEATURES_REVIEW.md; confirmed findings received focused regressions and fixes. Final suite discovered 143 tests: 141 passed in the original environment and the other two passed against real optional frameworks. See VERIFICATION.md for commands, evidence, and limits. Changes remain uncommitted in the requested checkout.
