# StateTree Project Completion Implementation Plan

**Goal:** Expose and verify the ten README features through an integrated project facade, CLI and authenticated local workbench.
**Architecture:** Reuse existing core stores and runtime. Add explicit public-state snapshot decoding and a lazy real Strands inference bridge. Preserve existing interfaces and cloud app.
**Tech Stack:** Python 3.11+, Git, SQLite, standard-library HTTP, Strands 1.56.0 for inference, vanilla JS.
**Spec:** ../specs/2026-09-21-project-completion.md

## Constraints and decisions
- Do not alter or overwrite the user's remote repository or AWS resources.
- Keep the original uploaded archive unchanged; work in its isolated extracted copy.
- SDK installation is blocked by runtime networking. Test actual stdlib components; preserve required SDK and add CI for complete tests. Do not vendor a fake SDK.
- Default model configuration is local loopback inference. No paid service fallback.
- UTF-8 serialized sizes are estimates, not model tokens, energy, money or accuracy claims.

## Review focus
Stale concurrent callers; checkpoints containing corrupt or wrong-format snapshots; sensitive filesystem paths; incomplete usage and retry accounting; verifier failures and changed files.

## Tasks
- [x] 1. Write failing `tests/test_project.py` integration cases for Project, snapshot decoding, facts, checkpoints, context, portable state and durable recovery. Run the tests before implementing. Add `statetree/project.py`, `statetree/project_state.py` and decoder injection in `runtime/runtime.py`; rerun.
- [x] 2. Add regression tests for reopening named branches and stale candidate completion. Extend `workspace/branches.py` with persisted candidate heads, listing and reopening. Integrate verified commands and explicit adoption into Project; test actual Git worktrees.
- [x] 3. Add `statetree/cli.py` and `__main__.py`, package entry points, local model bridge and reproducible offline demo. Test subprocess commands and optional genuine Strands integration. No fabricated model calls.
- [x] 4. Add authenticated loopback workbench in `web/workbench.py` plus static HTML/CSS/JS; test HTTP authentication, rebinding/origin/body validation, safe operations and errors. Keep the existing cloud frontend intact.
- [x] 5. Run all available tests, compile checks, JS checks, wheel/package checks and example smoke flows. Review changes separately. Add CI and truthful README quickstart, feature matrix, validation log and archive/patch artifacts.

## Verification outcome
Implementation and available local checks were executed. This is **not** a green full-SDK release: see `docs/COMPLETION_REPORT.md` for the 15 dependency-related failures and 11 collection errors. No merge, remote push or deployment was performed.
