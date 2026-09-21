# Portable state and model handoff review

Reviewed `core/state.py`, `storage/portable.py`, `adapters/portable.py`,
`runtime/handoff.py`, the portable additions to `runtime/runtime.py`, and their
focused tests against `docs/LOCAL_FEATURES_PLAN.md`. No production or test code
was changed. Optional LangGraph/CrewAI integration was outside this pass while
its real-SDK tests were still pending.

## Findings

### P2: Public adapter transfer can leave an attached runtime on the old goal

`transfer_state()` calls `destination.import_state()` directly
(`adapters/portable.py:105-108`). `StrandsStateAdapter.import_state()` updates
the agent's `statetree` and `statetree_context` values
(`adapters/portable.py:45-59`), but cannot update `StateTreeRuntime.goal` or
`StateTreeHooks.goal`. The hook then builds the next request with its old goal
and the newly imported prompt state (`adapters/strands.py:97-103`).

Focused reproduction: create a runtime with `OLD-GOAL`, then call
`transfer_state(MappingStateAdapter(source_with_NEW_GOAL),
StrandsStateAdapter(runtime.agent))` and run one offline request. The observed
state was:

```text
runtime_goal= OLD-GOAL
exported_goal= NEW-GOAL
old_in_prompt= True
new_in_prompt= True
{"goal":"OLD-GOAL","state":{"goal":"NEW-GOAL", ...}}
```

The model receives contradictory control state, and a later checkpoint also
uses the stale runtime goal. `StateTreeRuntime.set_state()` and
`StateTreeRuntime.import_state()` synchronize the goals, but the public generic
transfer path provides no guard against attaching a `StrandsStateAdapter` to a
runtime-owned agent. Either make that path synchronize the attached runtime or
fail closed with instructions to use the runtime import API. Add an actual
model-request test for this public composition.

### P2: `extensions` survive transfer but disappear from model context

`AgentState` defines `extensions` as validated portable application state
(`core/state.py:49`), and the branch documentation directs applications to put
application-specific keys there. `prompt_state()` omits the field
(`core/state.py:97-102`). A model handoff clears all messages and relies on that
projection (`runtime/handoff.py:51-55`), so state stored only in `extensions`
is unavailable to the receiving model.

Focused reproduction transferred
`extensions={"continuation":{"ticket":"EXTENSION-MARKER"}}`, switched models,
and ran an offline request:

```text
portable_preserved= {'continuation': {'ticket': 'EXTENSION-MARKER'}}
marker_in_model_request= False
```

Include `extensions` in the bounded projection, or define and document a
separate prompt-visible application extension field. The existing budget check
can bound it. Add a handoff request test using application-specific state.

### P2: A failed model switch can advance the branch head permanently

`switch_model()` commits and moves the active branch head before binding the
new model and storing its receipt (`runtime/handoff.py:42-56`). Its exception
handler restores the model, messages, context, goals, and builder, but does not
restore `runtime._parent` or the store head (`runtime/handoff.py:57-66`). This
misses the plan's rollback-on-failed-binding requirement. The current failure
test stops during preflight, before the commit (`tests/test_handoff.py:44-52`),
so it does not exercise this transaction boundary.

A focused offline reproduction made only the receipt `put_archive` call raise:

```text
error= receipt write failed
model_restored= True
head= d240cd85...5045952
parent= d240cd85...5045952
```

Before the call both head and parent were `None`. Thus the failed operation
leaves an unexplained `before model handoff` checkpoint and changes the parent
of subsequent work; with no prior checkpoint, the public API cannot restore the
original empty branch. Make checkpoint/head publication and binding one
recoverable operation, or explicitly define a failed handoff as retaining the
pre-handoff checkpoint and report that checkpoint in the raised error. Add a
test that injects failure after `_commit`, not only during preflight.

## Reviewed behavior without a finding

The portable bundle validates its outer hash, every archive's content hash,
and the exact set of declared archive references before publication.
`AgentState` and adapters copy JSON data rather than aliasing caller-owned
objects. Partial archive publication can leave immutable orphans but does not
bind destination application state. Runtime locking and message-group
validation reject handoff during runtime-owned calls and unfinished tool
transactions. Successful switches retain the existing hook and usage ledger,
so run-level accounting continues across models; provider-private state and
usage transfer between unrelated stores remain outside the declared portable
schema.

## Re-review after fixes

The three original findings are addressed for their covered paths:

- Public `StrandsStateAdapter.import_state()` forwards to the live owning
  runtime, so its operation lock and goal synchronization are used.
- Prompt projection includes application extensions while omitting reserved
  top-level keys beginning with `statetree.`; the projection remains subject to
  the existing complete-request budget.
- Receipt-write failure now restores the binding and uses a branch-head CAS to
  rewind only the checkpoint published by that handoff. The regression covers
  both the empty-head case and the active model/messages.

The updated request-level regressions directly exercise all three original
failures. Two adjacent P2 issues remain.

### P2: Model ID lookup is outside the post-checkpoint rollback boundary

The revised handoff captures the old parent and commits at
`runtime/handoff.py:45-46`, then builds the receipt by calling `_model_id()` on
both models at `runtime/handoff.py:47-50`. The rollback `try` does not begin
until line 51. A valid `Model` whose `get_config()` raises therefore advances
the branch head and `_parent` without entering the CAS rollback.

Focused reproduction with a `ScriptedModel` subclass whose `get_config()`
raises produced:

```text
error_type= OSError
error= config unavailable
head= bd9e2a6...a1ed4
parent= bd9e2a6...a1ed4
model_unchanged= True
```

Both head and parent were `None` before the call. Resolve and validate the
`from_model` and `to_model` labels during preflight, before `_commit`, or move
receipt construction inside the rollback boundary. Add a regression whose
model configuration read fails after the ordinary type/statefulness checks.

### P2: A collected weak-reference owner silently restores split-brain state

`StrandsStateAdapter.import_state()` forwards only while
`_statetree_runtime_ref()` is live (`adapters/portable.py:45-50`). If an
application retains the agent but drops the runtime, the runtime is collected
while its hooks remain registered on the agent. The adapter then falls back to
`_apply_strands_state()`, updating portable state without updating the old
hook's goal.

Focused reproduction deleted the only strong runtime reference, forced garbage
collection, imported `NEW-GOAL`, and invoked the retained agent:

```text
owner_collected= True
old_in_prompt= True
new_in_prompt= True
```

When `_statetree_runtime_attached` is true but its owner weak reference is
dead, fail closed rather than applying state directly. This also gives callers
a clear signal that the runtime lifecycle must be retained while its hooks are
active.

## Final narrow resolution

Both follow-up P2 findings are resolved.

- `runtime/handoff.py:45` now resolves both model identifiers before
  `_commit`. A failing `get_config()` therefore remains a preflight failure and
  cannot publish a checkpoint or advance `_parent`. The regression asserts
  that both the store head and runtime parent remain `None`.
- `adapters/portable.py:50-51` now detects an attached agent whose runtime weak
  reference is dead and raises before `_apply_strands_state`. The regression
  forces owner collection and verifies that adapter import is refused.

The placement of both guards closes the reproduced mutation paths. No new
defect was found in this narrow re-review. The controller's fresh targeted
portable/handoff result is 15 passing tests; this reviewer did not repeat the
broader suite.
