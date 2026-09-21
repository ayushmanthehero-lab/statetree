# Durable local runner report

## Public API

The implementation is in `statetree.runtime.durable` and has no dependency
outside the Python standard library.

```python
Step(
    step_id: str,
    tool: str,
    arguments: dict,
    mode: str = "read",  # "read" | "idempotent" | "at_most_once"
)

DurableRunner(
    path,
    tools: Mapping[str, Callable],
    run_id: str,
)

DurableRunner.run(steps: Sequence[Step]) -> list
DurableRunner.resolve(step: Step, result) -> JSONValue
DurableRunner.reconcile(step: Step, reconciler: Callable) -> JSONValue
```

Each tool handler and reconciler has this calling convention:

```python
handler(arguments: dict, idempotency_key: str) -> JSONValue
```

`StepConflictError(ValueError)` reports reuse of a persisted `step_id` with
different tool, mode, or arguments. `UncertainStepError(RuntimeError)` reports
an unresolved `at_most_once` intent; its `step_id` and `idempotency_key`
attributes support external reconciliation.

## Recovery behavior

The runner writes a committed SQLite `intent` before invoking a handler and a
committed JSON receipt after it returns. Completed steps replay that receipt
without invoking the handler. A key derived from `(run_id, step_id)` remains
stable across retries and process restarts. Existing definitions for every
step in an input plan are checked before its first effect.

`read` and `idempotent` intents are retryable after an exception or abrupt
process exit. An `at_most_once` intent fails closed after either event. The
caller must then use `resolve(step, verified_result)` after inspecting the
external system, or `reconcile(step, reconciler)` with a read-only query that
returns the prior operation's receipt. Neither interface repeats the original
tool.

A per-journal, per-run file lock is held across the complete `run`, including
handler calls. A same-run process therefore waits and replays the completed
receipt rather than concurrently claiming the same step. A process-local
thread lock covers threads as well. The Windows implementation uses
`msvcrt.locking`; POSIX uses `fcntl.flock`. It does not probe PIDs or send
signals, and operating-system lock release handles process death.

Arguments and receipts use strict JSON data: objects have string keys, arrays
are lists, numbers are finite, and Python-only values are rejected. The runner
validates duplicate IDs and the complete tool mapping before any input-plan
effect.

## TDD and verification evidence

The public-surface test was run before the module existed and failed with one
assertion because `statetree.runtime.durable` could not be imported. The full
behavior suite was then run against the importable stub and failed in all
unimplemented behavior paths (`11` tests; `9` reported failures and `5`
reported errors including subtests). A later safety regression test failed
because an earlier new step executed before a later definition conflict was
detected; the persisted-definition preflight made it pass.

The targeted suite exercises cached replay, input-definition conflicts,
intent visibility at the effect boundary, retry keys, handler exceptions,
manual resolution, reconciliation, strict JSON, duplicate and unknown plan
entries, real child-process death after an effect, restart behavior, and two
live subprocesses contending for one unsafe step. The final command and result
were:

```text
.\.venv\Scripts\python.exe -B -W error -m unittest tests.test_durable -v
Ran 13 tests in 1.422s
OK
```

`python -B -m py_compile` also accepted both the implementation and test
module. The controller owns the final repository-wide test run after all
parallel feature files settle.

## Limits

- The guarantee is a local journaling protocol, not exactly-once delivery.
  Correct idempotent recovery depends on the handler honoring the supplied key.
- `resolve` trusts the caller's external inspection, and `reconcile` trusts its
  callback to query without repeating the effect.
- File-lock behavior is intended for processes on one host using a local
  filesystem. Network filesystems with weak locking semantics are unsupported.
- One run is deliberately serialized for safety. Different run IDs have
  different lock files but still share SQLite's normal short write locks.
- A handler that never returns keeps the run lock; this API has no lease,
  timeout, cancellation, journal pruning, or encryption.
- No AWS service, model provider, or other live external system was invoked.

## Checkpoint-aligned workflow integration

`statetree.runtime.workflow` adds the integration entry point used by
`StateTreeRuntime.run_steps`:

```python
run_checkpointed(runtime, steps, tools, *, run_id) -> list[JSONValue]
```

The caller holds the runtime operation lock. `run_checkpointed` additionally
holds a cross-process, per-workspace file lock for the entire restore, tool,
and checkpoint span. It validates every step and every tool mapping entry
before workspace mutation. The ordered plan and its hash, run ID, workspace,
branch, baseline checkpoint, latest checkpoint, and ordered progress are kept
in `.statetree/workflows.sqlite3`, outside checkpoint rollback. Tool intents
and receipts remain in `.statetree/durable.sqlite3`.

Before a handler runs, the integration restores the workflow's latest aligned
checkpoint. Each completed handler result is stored as a content-addressed
archive. A reference, step index, and stable operation key are added to the
portable state's reserved `extensions['statetree.workflow']` mapping; the
workspace and agent are then checkpointed before the durable receipt is
written. Reserved `statetree.` extensions stay out of the model prompt.

On a fresh process, the branch head, workflow database, portable marker,
result archive, and durable receipt must agree before the next handler can
run. Cached receipts replay without repeating effects. An interrupted
baseline or step publication is recovered only when the current head is a
hash-validated direct child with the exact expected run, plan, branch,
completed prefix, journal definition/key, result reference, and checkpoint
subgoal. Manual rollback, an unrelated child, altered plans, and changed scope
fail before a handler.

An `at_most_once` crash before the wrapper checkpoint remains uncertain and
pauses without restoring away possible local evidence. If the wrapper
checkpoint and result reference were already published, the missing durable
receipt can be reconstructed without invoking the handler.

The workflow tests were written and observed failing first: initially all five
failed because the integration module was absent; publication-gap regressions
then failed with the divergence guard before narrow recovery was added. The
final warnings-as-errors verification was split to keep each subprocess-heavy
command bounded:

```text
3 workflow tests: Ran 3 tests in 22.562s - OK
3 workflow tests: Ran 3 tests in 15.757s - OK
```

The six tests cover full cached replay, altered-plan and invalid-tool
preflight, concurrent fresh processes, a real process exit after a completed
step checkpoint but before workflow progress publication, a real exit after
baseline checkpoint publication but before activation, manual branch rewind,
and an uncertain unsafe effect before checkpointing.

This integration resumes only registered, explicitly classified tool steps.
It does not serialize or resume an opaque LLM/model call, private framework
scheduler state, or arbitrary Python stack. Workflow metadata and archives
are not automatically pruned, and a process that does not honor the workspace
lock can still race the runtime and trigger the normal stale-head guard.
