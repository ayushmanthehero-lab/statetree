# Structured fact memory report

## Scope

`FactMemory` is a deterministic policy for caller-supplied structured facts.
It does not extract claims from prose, decide whether arbitrary statements are
true, or infer that values stored under different keys contradict each other.
The caller supplies an explicit key, JSON value, evidence references, and
exact dependency versions.

The active projection from `active_facts()` is suitable for
`AgentState.facts`. The complete lifecycle from `to_dict()` is suitable for
`AgentState.memory`; it retains archived and invalid records, deletion
tombstones, evidence, dependencies, and invalidation provenance.

## Public API

```python
from statetree.memory import FactMemory

memory = FactMemory()
runtime = memory.observe(
    "runtime",
    {"language": "python"},
    evidence=["archive:readme"],
)
command = memory.observe(
    "test_command",
    "python -m unittest",
    evidence=["archive:contributing"],
    dependencies={"runtime": runtime["version"]},
)

from statetree.core.state import AgentState
state = AgentState(goal="Continue the task", facts=memory.active_facts(), memory=memory.to_dict())
memory = FactMemory.from_dict(state.memory)
```

The mutation methods are:

- `observe(key, value, *, evidence, dependencies=None) -> dict`: creates an
  active version. An exact duplicate of the active record is deduplicated.
  A changed observation archives the active version and invalidates active
  dependents transitively.
- `invalidate(key) -> dict`: marks the active record invalid and transitively
  invalidates its dependents.
- `archive(key) -> dict`: archives the active record and transitively
  invalidates its dependents.
- `delete(key) -> dict`: archives the active record, invalidates dependents,
  and appends a new version with status `deleted`. Earlier records remain in
  history. A later observation advances from the tombstone's version.
- `active_facts() -> dict`: returns a detached JSON object containing only
  active records, ordered lexically by key.
- `to_dict() -> dict` and `FactMemory.from_dict(payload)`: serialize and
  strictly validate the full version-1 lifecycle. Unknown fields, malformed
  records, noncontiguous versions, stale active dependencies, dependency
  cycles, non-finite/non-JSON values, and invalid evidence fail closed.

All returned values and accepted mutable inputs are copied. Mutating a value,
evidence list, dependency mapping, returned record, projection, or serialized
payload cannot mutate the memory indirectly.

## Bounded materialization

```python
projection = memory.materialize(
    4096,
    counter=None,
    required=["test_command"],
)
state = AgentState(goal="Continue the task", facts=projection.facts, memory=memory.to_dict())
print(projection.estimated_count, projection.counting_method)
```

`materialize(budget, counter=None, *, required=())` returns a
`FactMaterialization` with `facts`, `estimated_count`, `counting_method`, and
`omitted_keys`. Required facts bring their full transitive dependency closure.
If that closure does not fit, `FactBudgetError` is raised. Optional facts are
tried in lexical key order and are included only when their complete dependency
closure fits, so selection is deterministic and never emits a dangling active
dependency.

The dependency-free default serializes the facts as canonical compact JSON and
counts UTF-8 bytes. Its label is
`serialized_json_utf8_bytes_estimate`; this is a size estimate, not a model
token count. A custom `counter(serialized_json) -> nonnegative int` changes the
label to `custom_counter`.

## Lifecycle rules and limits

Versions increase independently for each key and dependencies bind to an exact
active version. A dependency on a missing, inactive, or older version is
rejected. A replacement that would depend on one of the old version's active
dependents is rejected transactionally as a cycle because superseding the old
version would immediately invalidate that dependency.

Evidence is a nonempty list of unique, nonempty string references for observed
facts. The memory stores those references; it does not fetch or verify the
referenced artifacts. Deletion tombstones intentionally have no value,
evidence, or dependencies. Lifecycle operations require an active key, so a
mistyped or repeated operation raises `KeyError` rather than silently changing
nothing.

Selection priority is lexical key order after required closures. There is no
recency, relevance, confidence, embedding, semantic search, prose extraction,
or automatic conflict resolution. Applications that need those policies must
make them explicit before calling this API.

## TDD evidence

The initial package-contract test was run before implementation and failed:

```text
test_memory_package_exists ... FAIL
AssertionError: unexpectedly None : The structured memory package must exist
```

After adding only the package boundary, that test passed. The full behavioral
test file was then written before `FactMemory`; its first run failed at import
because `FactBudgetError`, `FactMemory`, and `FactValidationError` did not yet
exist. After implementing the contract, the targeted run reported:

```text
Ran 18 tests in 0.014s
OK
```

The tests use standard-library `unittest` and make no network, model, cloud, or
AWS calls.

## Boolean/number deduplication regression

Duplicate detection compares canonical serialized JSON values rather than
Python equality. This matters because Python considers `True == 1` and
`False == 0`, including at nested positions, while JSON preserves booleans and
numbers as different value types. Observing a boolean and then a number now
creates a new version and performs the normal transitive invalidation of facts
that depended on the boolean version.

The direct and nested regressions were first run against Python equality and
both failed with `version 1 != 2`. After switching to canonical JSON equality,
both passed. The direct test also verifies that a fact depending on version 1
becomes invalid when numeric version 2 replaces it.
