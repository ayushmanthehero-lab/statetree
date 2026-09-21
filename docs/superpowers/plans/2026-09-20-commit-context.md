# Commit Context Implementation Plan

> **For agentic workers:** Use executing-plans for runtime integration and bounded parallel implementation for the independent note store. Track verification below.

**Goal:** Reuse relevant structured checkpoint messages as budgeted context for the next user task, with original evidence retrievable locally.

**Architecture:** Immutable sidecar notes bind to existing checkpoint IDs without changing manifests. A deterministic keyword retriever follows current ancestry, handles explicit supersession and declared resource versions, and supplies optional notes to the context builder. An explicit new-task boundary archives completed history before replacing it with current state and selected notes.

**Tech Stack:** Python 3.11+, standard library, existing Strands 1.56 integration, Git checkpoint store; no new dependencies.

**Spec:** User-approved design in this conversation: structured notes, local task-based search, budgeted context, branch provenance and evidence retrieval.

## Global Constraints

- Work in the existing workspace; preserve unrelated uncommitted changes.
- No AWS configuration, credentials, resources, paid calls or model-generated summaries.
- Existing checkpoint hashes, restore behavior and default conversation policy remain compatible.
- Default counts are serialized UTF-8 byte estimates, never reported as actual model tokens.
- Notes are historical evidence; current state and user instructions take precedence.

## Review Focus

- Sibling branches and restored-away descendants must not leak into recall.
- Missing/corrupt evidence must fail before lossy history replacement or model invocation.
- Mandatory state, tool schemas and the newest request must win over optional notes under a tight budget.
- Retrieval must reset for each invocation, including after restore and a model handoff.
- New-task history replacement must archive first, reject unfinished tools, and preserve history on preparation failure.

## Task 1: Immutable notes and local retrieval

Files: `statetree/memory/commits.py`, `tests/test_commit_memory.py`.

API: `CommitNote(summary, changes=[], decisions=[], pending=[], evidence_ids=[], dependencies={}, supersedes=[], outcome='completed')`; `CommitMemory(store).validate(note)`, `.write(commit_id, note)`, `.read(commit_id)`, `.search(query, head_id, budget=..., counter=None, resources=None, limit=8)` returns notes, estimate and counting method.

- [x] Write tests for strict note validation, immutable storage/hash checks, missing evidence, branch ancestry, restore, supersession, dependency versions, relevance and budget.
- [x] Run `python -B -m unittest tests.test_commit_memory -v` and observe missing-feature failures.
- [x] Implement structured sidecars and deterministic local whole-note selection; verify focused tests.

## Task 2: Runtime and context integration

Files: `statetree/context/builder.py`, `statetree/adapters/strands.py`, `statetree/runtime/runtime.py`, `tests/test_commit_context.py`.

API: `StateTreeRuntime(..., commit_context_budget=0)` enables recall when positive and requires a bounded context. `commit(message='...', note=None)` accepts a shorthand message or structured note, archives original messages/verification, writes the note before publishing the branch head. `recall(query, budget=None, resources=None, limit=8)` exposes selection. `run(prompt, new_task=True)` archives complete prior history, supplies a persistent retrieval reference, and starts a new conversation. `ContextBuilder.build(..., commit_notes=None)` fits optional notes only after mandatory context fits; reports selected IDs.

- [x] Write builder tests for relevance handoff, full-request budget, mandatory precedence and no input mutation.
- [x] Write real scripted-provider tests for message persistence, note injection, changing queries, archive reading, fresh instances/restore, explicit new-task compaction and preparation failures.
- [x] Observe failures with `python -B -m unittest tests.test_commit_context -v`.
- [x] Implement runtime wiring and note-aware context preparation; run focused regression tests.

## Task 3: Demonstrate and verify

Files: `examples/commit_context.py`, `docs/COMMIT_CONTEXT.md`, `README.md`, `docs/VERIFICATION.md`.

- [x] Add a fully local demo: checkpoint login notes and unrelated UI notes, ask for password reset, inspect selected notes and compare serialized request sizes with full history.
- [x] Document explicit note-writing, new-task boundaries, current requirements, keyword limitations, archive retrieval, resource versions, and lack of actual billing/success measurements.
- [x] Run the new demo and full unittest suite; independently review changed feature files and resolve concrete findings.
- [x] Record measured verification results without changing Git staging or publishing.
