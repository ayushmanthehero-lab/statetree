# Independent local-feature review

Reviewed `workspace/branches.py`, `runtime/parallel.py`, `runtime/durable.py`, `memory/facts.py`, and the new `runtime/workflow.py` against `LOCAL_FEATURES_PLAN.md` and `LOCAL_FEATURES.md`. Portable/model handoff review was excluded because the controller is handling it separately. Findings below describe the reviewed implementation before remediation; subsequent fixes need focused regression checks.

No cloud/model requests, installation, or commits were performed. Existing component suites were not repeated. Four focused Python repros used the installed virtual environment; workflow and branch repros used temporary actual Git repositories.

## P1: Workflow checkpoint publication and progress publication can diverge after a crash

Locations: `statetree/runtime/workflow.py`, baseline `_commit` followed by `_WorkflowStore.activate` (approximately lines 347-348), and completed-step `_commit` followed by `_WorkflowStore.append` (approximately lines 405-423).

A crash or persistence error after `_commit` publishes the new branch head but before the separate workflow SQLite transaction leaves `latest_checkpoint` behind. Resume compares the branch head to the old value and raises `Workflow branch was manually rewound or diverged`, even though the newer checkpoint contains the legitimate completed-step marker and result reference. The same publication gap exists during baseline activation. This prevents continuation of an otherwise recoverable idempotent workflow.

Focused repro: create a runtime and one idempotent step that writes `effect.txt`; patch `_WorkflowStore.append` to raise `RuntimeError('interrupt after checkpoint')`; execute once, remove the patch, instantiate a fresh runtime, and resume the identical plan/run ID. The effect and completed checkpoint persist, but resume fails with the divergence error. This models interruption at the exact durable publication boundary without relying on process timing.

Recommendation: atomically publish progress with the checkpoint head, or recover the narrowly defined interrupted publication using validated ancestry, plan, marker, journal intent, and archived result evidence. Preserve rejection of genuine manual rewind/divergence. Test both baseline and step publication gaps, including fresh-process recovery.

## P1: Noncanonical file resource names bypass stale-read detection

Location: `statetree/workspace/branches.py`, `_declarations` and `resource_version` (approximately lines 203-227).

`file:./file.txt` passes `_safe_name`, but `resource_version` queries Git's canonical entry dictionary with the literal `./file.txt`. It therefore returns `None` even when `file.txt` exists. The same declaration continues to appear absent after another branch changes the actual file, so stale dependent work is accepted. Repeated separators and embedded `./` have the same normalization issue; case aliases additionally need a clear policy on Windows.

Focused repro:

1. Initialize a manager over the test repository containing `file.txt`.
2. Compare `resource_version(base, 'file:file.txt')` (a hash) with `resource_version(base, 'file:./file.txt')` (`None`).
3. Fork a dependent with the latter declaration and complete a state change derived from the old file.
4. Integrate another branch that modifies `file.txt`.
5. Integrate the dependent against the new canonical parent.

Observed: integration succeeds and publishes the stale calculated state. This crosses the documented declared-dependency boundary; the dependency was explicitly declared through the public version API.

Recommendation: normalize file names consistently before persistence and lookup, or reject noncanonical names before returning versions/forking. Add alias-based stale read/write regressions.

## P2: Fact deduplication conflates JSON booleans and numbers

Location: `statetree/memory/facts.py`, `FactMemory.observe`, comparison of `current['value']` with `copied_value` (approximately line 241).

Python equality considers `True == 1` and `False == 0`, including within nested dictionaries/lists. An observation that changes a JSON boolean into a number with unchanged evidence/dependencies is incorrectly deduplicated. The old value remains current, no version advances, and dependent facts remain active.

Focused repro: observe `config = {'enabled': True}` with evidence `['file:config']`; observe a dependent with `dependencies={'config': 1}`; observe `config = {'enabled': 1}` with the same evidence. Observed replacement is still version 1 with `{'enabled': True}`, and both config and dependent remain active.

Recommendation: compare canonical JSON representations or use explicit JSON-type-sensitive equality. Cover nested boolean/number changes and dependent invalidation.

## P2: Durable plan preflight omits fresh-step argument validation

Location: `statetree/runtime/durable.py`, `_validate_plan` and `_validate_persisted_definitions` (approximately lines 283-306).

`Step` copies arguments at construction but exposes a mutable dictionary. `_validate_plan` checks only Step identity, duplicate IDs, and registered tools. `_validate_persisted_definitions` serializes arguments only for steps already present in the journal. Therefore an invalid fresh later step can reject only after earlier effects execute, contrary to `run`'s documented whole-plan validation before effects.

Focused repro: construct two fresh valid Steps; assign `second.arguments['invalid'] = float('nan')`; run both with a handler recording calls. Observed: the first handler executes once, then the second step raises `ValueError: $.invalid contains a non-finite number`.

Recommendation: snapshot/revalidate every supplied Step and its arguments during preflight, before any effect. A regression should assert zero handler calls on an invalid later step.

## Other reviewed behavior

No additional concrete defect was found in the reviewed speculative coordinator. Candidate snapshots and persisted declarations are detached from mutable worker state; integration performs independent candidate/integrated verification and a final canonical-parent CAS. The coordinator preserves recorded losing/error usage. Trusted local callables, cooperative budgets, declared external dependencies, and separate portable versus legacy canonical refs were treated as intentional documented boundaries, not defects.

## Remediation re-review

All four findings above are resolved in the revised code. This follow-up is scoped to those fixes and their changed sections; it is not a full-suite sign-off.

- **Workflow publication gaps:** recovery now validates the already-published checkpoint's ancestry, branch/subgoal, workflow plan/marker, persisted intent definition and operation key, and archived result before repairing the missing progress transaction. Independently reran the fresh-process completed-step and baseline crash regressions plus the manual-rewind rejection regression: **3 passed**, with `ResourceWarning` treated as an error. Also reran the original idempotent append-failure repro: a fresh runtime resumed successfully, returned the original result, and the effect occurred exactly once in that repro.
- **File resource aliases:** `_file_resource_path` rejects noncanonical spelling and trailing dot/space components; version lookup rejects case aliases of known Git paths. The focused alias regression passed independently. The original `file:./file.txt` declaration now fails before it can be mistaken for an absent dependency.
- **Fact JSON types:** canonical JSON equality replaces Python value equality. Both scalar and nested boolean/number regressions passed independently, including dependent invalidation.
- **Durable plan preflight:** every supplied Step is reconstructed and its arguments copied/validated before execution. The targeted regression passed independently, confirming rejection before any effect and isolation from later mutation by an earlier handler.

Total independently executed during this follow-up: **7 focused regression tests passed**, plus the original idempotent interruption repro. No additional defect was found within these changed sections. No product files were edited by the reviewer.
