# StateTree completion ledger

Plan: docs/superpowers/plans/2026-09-21-project-completion.md

Baseline: 64 tests + 95 subtests passed (usage, facts, durable runner, selected deployment/container suites).
Environment: Python 3.13.5; Git and pytest present. Strands 1.56.0 is not installed. pip and direct download attempts failed due runtime network access. Real provider/cloud runs are not authorized or attempted.
Decision: preserve Strands, add explicit decoder injection for public-state-only local operations; do not emulate the SDK. No remote push/deploy.

Task 1: public-state Project facade and decoder injection implemented; real Git/SQLite tests exercise recovery and rollback without rolling back usage.
Task 2: persisted branch candidates, reopen and CAS implemented; worktree exclusion mismatch fixed by keeping the standard exclusion policy in the Project facade.
Task 3: CLI and local component demo implemented; real Strands bridge added behind lazy imports. SDK/provider execution remains unverified in this environment.
Regression: ignored default storage paths caused git add to fail. Explicit literal eligible-file enumeration fixes it without adding ignored credentials. Test RED then GREEN.
Regression: integrated worktrees mark baseline untracked files as tracked. Adoption now composes verified files with the source index, preserving staged/untracked status and rejecting ignored-file collisions. Test RED then GREEN.
Verification: original branch/speculation suite 22 passed + 20 subtests after the changes.
Task 4: loopback workbench, bearer authentication, Host/Origin checks, strict JSON, stale-head protection and confirmation-gated file mutations implemented.

Task 5: CI, feature matrix, operator guide, full-suite attempt, core runner, final wheel and isolated-wheel demo executed.
Final review: self-review (no independent reviewer available).
Ruling: explicit public-session decoder, not a fabricated SDK — preserves honest dependency boundaries — native-SDK paths still need real-SDK validation.
Ruling: separate local workbench — preserves existing cloud API behavior — does not redeploy or replace the public site.
Ruling: conservative coordinator baseline and explicit adoption — prevents silently overwriting divergent main state — no automatic coordinator rebase on later main edits.
Ruling: actual HTTP bridge for managed-browser DOM tests — respects loopback-navigation policy — ordinary same-origin browser navigation remains untested here.
Final core: python scripts/test_core.py → 235 passed, 5 skipped, 7 deselected, 198 subtests; exit 0.
Final full: python -m pytest --continue-on-collection-errors -q → 248 passed, 15 failed, 5 skipped, 11 collection errors, 208 subtests; exit 1. All failing traces reach missing strands SDK.
Final package: wheel built, isolated --no-deps installation successful, doctor and seven-check zero-model demo passed.
No merge/push/deploy: full-SDK gate remains open; deliver source with explicit verification report.
