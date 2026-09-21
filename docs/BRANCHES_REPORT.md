# Local isolated branches and speculation

The implementation owns `workspace/branches.py`, `runtime/parallel.py`, and their two test modules. During verification it also fixed a shared snapshot issue in `workspace/git.py`, with a regression in `test_workspace.py`. It makes no model or cloud calls. It uses real Git worktrees, immutable JSON objects, SQLite publication checks, and the existing usage ledger.

## API

```python
from statetree.workspace.branches import BranchManager
from statetree.runtime.parallel import SpeculativeCoordinator
from statetree.runtime.usage import UsageLedger

def verify(path, state, phase):
    # Application-owned checks; inspect files at path and validate state.
    # phase is "candidate" or "integrated". Return exactly True to pass.
    return (path / "answer.txt").exists() and state["extensions"].get("answer") == 42

manager = BranchManager(repo_path, verifier=verify)  # root defaults to .statetree/parallel
baseline = manager.initialize(agent_state.to_dict(), resource_versions={"catalog": "v1"})

branch = manager.fork("candidate-a", reads={"catalog": "v1"})
# Use branch.path explicitly for every filesystem operation/subprocess cwd.
(branch.path / "answer.txt").write_text("42", encoding="utf-8")
branch.state["extensions"]["answer"] = 42
candidate = manager.complete(branch, state=branch.state, result={"quality": 1})
revision = manager.integrate(candidate["id"], expected_parent=baseline["id"])
print(revision["workspace_path"], revision["workspace_sha"], revision["state"])
```

Keep application-specific keys in the portable `AgentState`'s allowed `extensions` or other documented fields. The manager merges generic JSON and delegates application schema validation to the verifier.

`manager.head()` returns the current revision or `None`. `initialize` captures a starting baseline once; it is not a claim that an unfinished task already passes verification. Subsequent canonical revisions can only be published by `integrate` after independent checks. Reopening `BranchManager` on the same `root` retains its refs, branch descriptors, candidates, and files. The supplied verifier is an application policy and must be supplied again after reopening.

`fork(name, *, base_id=None, reads=None, writes=None, namespace=None)` creates a detached Git worktree from a pinned snapshot. `Branch` exposes `name`, `path`, `base_id`, `state`, `reads`, and `writes`. State is copied, not shared. A namespace permits repeated human-readable names across separate runs; the coordinator assigns a fresh namespace automatically. Branch metadata fixes the declarations at fork time; mutating the returned dictionaries does not change persisted dependencies.

`complete(branch, *, state, result=None)` captures candidate files and state in an immutable manifest. The caller must stop writing before completion. Subsequent modifications to its working directory do not change that candidate. `verify_candidate(candidate_id)` checks an isolated fresh checkout of the candidate without publishing it. Verifiers must not mutate the checked state or nonignored files; either mutation fails verification.

`integrate(candidate_id, *, expected_parent)` is the explicit **rebase-and-publish operation**. The candidate's base may be older than the expected canonical parent. It recursively merges JSON dictionaries and treats arrays/scalars and entire file contents as indivisible values. Disjoint changes merge; conflicting updates, delete/modify conflicts, case-insensitive filename collisions, and file/directory collisions fail. There is no textual source-code merge or inference that two semantically different facts agree. Independent checks inspect both the candidate and the merged result. A final SQLite compare-and-set rejects publication if canonical state changed while verification ran. `MergeConflictError`, `StaleParentError`, and `VerificationError` leave the canonical ref unchanged.

**The manager's canonical ref is separate from the legacy opaque-Strands checkpoint canonical ref.** Publication returns a pinned `workspace_sha`, retained `workspace_path`, and merged JSON `state`. It never moves Git HEAD, replaces the caller's dirty files/staging, or silently promotes the legacy runtime. Consumers explicitly adopt the returned state/workspace through their own binding/checkpoint workflow.

## Resource dependencies

`reads` and `writes` map resource names to their expected string version, or `None` for absence. External resource versions begin in `resource_versions`; a successful declared write creates a new version digest. A changed read version or conflicting write version rejects integration even when the changed files are disjoint.

File writes are compared automatically. To detect stale file reads, obtain the version with `manager.resource_version(revision, "file:path/to/file")` and declare it in `reads`. File versions include Git blob identity and executable mode; absence is `None`. Files omitted from declared reads and undeclared external dependencies cannot be checked. This is explicit dependency validation, not runtime interception of arbitrary file/network access.

## Speculative workers and usage

```python
def strategy(branch):
    branch.check_budget()  # Required before each potentially metered call.
    # Call a configured model locally if the application has authorized it.
    # Immediately persist the actual returned provider counts, including errors:
    # branch.record_usage(provider_usage_or_none, request_id=stable_request_id)
    return {"state": branch.state, "result": {"quality": 1}}

coordinator = SpeculativeCoordinator(
    manager,
    ledger=UsageLedger(manager.root / "usage.sqlite3"),
    max_workers=2,
    max_branches=4,
    total_token_limit=100_000,
    per_branch_token_limit=30_000,
)
report = coordinator.run(
    {"approach-a": strategy, "approach-b": strategy},
    run_id="experiment-1",
    score=lambda candidate: candidate["result"]["quality"],
)
```

`run(strategies, *, run_id, reads=None, writes=None, score=None)` invokes at most `max_workers` callable strategies simultaneously in separate folders. It creates at most `max_branches` branches. Each callable returns `state`, optional JSON `result`, and optionally a `usage` list of request records. The fixed verifier determines eligibility; a worker's own `passed=True` has no authority. Higher finite application scores win; equal scores use strategy-name order, independent of completion order. If a selected candidate fails integrated verification, the next eligible candidate is tried against the same expected parent. A stale parent aborts promotion. All-fail runs preserve canonical state.

`branch.record_usage(usage=None, *, request_id=None, phase="strategy", model="", status="ok", input_cache_convention="auto")` persists observed request counts immediately. This retains spending even if the strategy subsequently raises. Optional returned usage records accept the same fields and are processed before candidate completion. Replaying a request must reuse the same ID and payload; independent attempts need distinct IDs. Missing provider usage is recorded as unknown, not known zero. A pure local strategy does not imply a model request, so the coordinator does not invent usage events.

`branch.check_budget()` checks persistent run and branch totals. The coordinator also checks once before starting each worker. Unknown or ambiguous usage blocks further calls when a threshold applies. These are cooperative between-call thresholds, not hard billing caps: already running calls may cross them, and arbitrary callables must explicitly check before additional calls. Verifier/model calls performed outside the worker need their own ledger recording. No provider-internal retry accounting or reconciliation is inferred.

Reports contain every attempt, retained folder and candidate ID, verification/scoring outcome, error, winner, canonical revision, per-branch usage, total usage, and an immutable `report_id`. Losing and failed folders remain available for inspection. Observed loser and failure usage is never rewound.

## Local safety boundary and limitations

Workers must be cooperative local callables: use the provided folder, never process-wide `os.chdir`, stop editing before returning, and perform no uncontrolled external side effects. Threads cannot forcibly terminate a hung callable. Worktrees share Git object storage and do not isolate credentials, processes, databases, network access, or malicious code. This is file isolation, not a security sandbox.

Storage within the source repository must be under `.statetree`; other custom storage must not contain the source repository. Symlink/junction storage paths are rejected. The underlying snapshot adapter rejects unsupported links, submodules, and Git assume-unchanged/skip-worktree flags. There is no automatic recursive cleanup: worktrees, candidates, verifier artifacts, and reports are deliberately retained. Applications should remove only explicitly resolved owned worktree directories through Git after retaining desired artifacts. No shell-built destructive cleanup is used.

The implementation uses immutable-object publication followed by SQLite canonical publication. An interrupted attempt may leave an orphan manifest or folder. It does not publish an unchecked merge. This manager does not claim an atomic cross-system transaction for external resources, arbitrary semantic merge, or a migration of framework-private model state.

## Verification evidence

The initial 14 behavior tests were run before either implementation module existed and failed with explicit missing-implementation assertions. The first implementation pass then ran those tests successfully against temporary Git repositories (14 tests, 98.5 seconds).

An additional regression reproduced duplicate strategy-name rejection on a second run. Namespace support fixes that failure while preserving readable strategy names and per-run usage attribution. Further tests cover simultaneous canonical publication, stale file reads, immutable-candidate verification, verifier file mutation, branch-count bounds, and token thresholds including unknown usage. Final targeted results are recorded after the last run below.

The stale-file-read test exposed a shared Git snapshot defect: copying the index retained cached filesystem metadata, allowing equal-size working edits to be omitted when timestamps matched. A deterministic regression preserved file mtime under Git's coarse-stat configuration and observed the snapshot incorrectly contain staged bytes. Snapshotting now preserves the original index tree and rebuilds only its private temporary index before staging working content. This forces content reads without modifying the real index or staging.

The combined branch/parallel/workspace command passed 33 tests in 195.3 seconds after this fix:

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_branches tests.test_parallel tests.test_workspace -q
```

A final verifier-state regression then demonstrated that a callback could mutate its private state copy and validate different state from the immutable candidate. Verification now rejects state mutation, just as it rejects file mutation. The new state-mutation regression, existing file-mutation regression, and clean stale-base merge regression all passed together (3 tests, 23.6 seconds). The controller runs the final complete project suite once all workers finish.
