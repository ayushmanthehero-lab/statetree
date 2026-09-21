# Local StateTree features

These features run without AWS credentials. For self-hosted Qwen inference, use the new [SageMaker AI deployment guide](SAGEMAKER_DEPLOYMENT.md). The adapter and deployment scripts are implemented; AWS resource creation and measured live-model savings remain to be validated in your account.

Run the complete local demonstration in temporary repositories:

```powershell
.\.venv\Scripts\python.exe -B -m examples.local_features
```

The demonstration uses configured offline model objects, registered Python tools, and three deterministic worker strategies. It makes no model requests. The tests separately exercise the actual Strands streaming loop with explicitly simulated usage and subprocess crash recovery.

## Portable public state

```python
from statetree.core.state import AgentState
from statetree.adapters.portable import MappingStateAdapter
from statetree.storage.portable import import_bundle, write_bundle, read_bundle

runtime.set_state(AgentState(
    goal="Repair the API",
    constraints=["Preserve existing endpoints"],
    subgoals=["Run regression tests"],
))
write_bundle("handoff.json", runtime.export_state())
state = import_bundle(read_bundle("handoff.json"), destination_store)
custom_loop_state = {}
MappingStateAdapter(custom_loop_state).import_state(state)
```

State includes explicit goals, constraints, subgoals, facts, pending actions, resources, artifacts, memory, checkpoint identifiers, extensions, and archive references. Application extensions are included in the bounded prompt projection; names beginning `statetree.` are reserved for internal metadata and excluded. Bundles contain the complete contents of every declared `archive_ids` reference and verify both the bundle hash and each archive hash before import. A default 16 MiB read limit is configurable. Version-1 local Strands checkpoints remain separate and compatible.

An artifact descriptor does not automatically copy a file; place required artifact content in the archive store and list its hash in `archive_ids`. Likewise, application evidence references that point outside the bundle remain external references. Credentials, live connections, tool functions, private model state, and framework scheduler internals are not exported. Bind compatible tools and resources in the destination process explicitly.

Adapters: `StrandsStateAdapter`, `MappingStateAdapter`, `LangGraphStateAdapter`, and `CrewAIFlowStateAdapter`. They transfer the public schema. Applications must actually consume that state in their continuation code. LangGraph requires a checkpointer and a declared replace-on-write `statetree` state key; CrewAI supports a dictionary Flow state or a declared Pydantic `statetree: dict` field. These are not arbitrary private-session converters.

The optional integration tests instantiate real LangGraph and CrewAI flows without model calls. Their upstream public APIs are documented in [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) and [CrewAI Flow state](https://docs.crewai.com/en/concepts/flows).

## Fact memory

```python
from statetree.memory import FactMemory

memory = FactMemory()
memory.observe("schema", {"version": 1}, evidence=["schema inspection"])
memory.observe("query", "SELECT old_column FROM items", evidence=["query test"],
               dependencies={"schema": 1})
memory.observe("schema", {"version": 2}, evidence=["updated schema inspection"])
runtime.set_memory(memory, budget=2000)
```

The old schema is archived and its dependent query is invalidated. Only current selected facts enter the prompt. Full evidence history stays in the checkpoint. Identical observations deduplicate; changed values advance versions; explicit archive/delete/invalidate actions propagate to dependents. Delete creates a tombstone rather than altering old checkpoints.

`materialize` includes required facts and their dependency closure or fails if they do not fit. Its default counter measures serialized UTF-8 bytes, not billing tokens. This is deterministic management of structured observations supplied by the application. It does not infer truth or contradictions between arbitrary prose claims. Historical archives are retained; physical storage pruning is not performed.

## Model handoff

```python
# Both models must be configured, compatible, stateless Strands Model objects.
receipt = runtime.switch_model(other_model)
runtime.run("Continue from the supplied state")
```

Handoff validates a compact request including system prompt/tool schema overhead, resolves declared evidence, checkpoints the old session and files, binds the new model, and clears the replayed transcript. Existing tool registrations and the persistent usage ledger remain attached. Subsequent requests receive the active state through the context hook. Switching back uses the same operation.

Use `runtime.run` for invocations: its operation lock prevents checkpoint/restore/switch during a call. Calling the underlying agent directly bypasses that guard. Handoff requires bounded context management, plain-text system prompts, compatible text/tool providers, and completed tool transactions. Applications must keep public state current; the library does not manufacture a summary of unfinished reasoning. There is no automatic small/large-model routing policy.

## Durable steps and recovery

```python
from statetree.runtime.durable import Step

def write_report(arguments, idempotency_key):
    # Implement the declared retry contract in the tool/service.
    report_path.write_text(arguments["text"], encoding="utf-8")
    return {"path": str(report_path)}

steps = [Step("report-1", "write", {"text": "Finished"}, mode="idempotent")]
results = runtime.run_steps(steps, {"write": write_report}, run_id="task-001")
```

The checkpointed runner coordinates the durable tool journal with saved agent/workspace state. Resume the same run with the same ordered plan and registered tool implementations. Stable operation identifiers, persisted intents, and receipts allow completed steps to be reused. Local checkpoint rollback must not silently roll back the external-effect journal.

The lower-level `DurableRunner` can be used independently. Its modes are `read`, `idempotent`, and `at_most_once`. A crash or exception after an unsafe call starts leaves an uncertain outcome; automatic retry is refused. `resolve` or `reconcile` records a confirmed outcome. No library can promise arbitrary external services execute exactly once just by saving a local checkpoint. Tools must honor their declared mode and stable idempotency key. These guarantees apply to registered steps, not uninstrumented shell commands or every internal framework operation.

## Git worktrees, concurrency, and forks

A Git worktree is another checked-out working directory attached to one repository. Each worktree has its own files, HEAD and index, while sharing Git's object storage. See the [Git worktree manual](https://git-scm.com/docs/git-worktree).

```text
Shared Git repository
  +-- caller's workspace
  +-- agent A: candidate files + state
  +-- agent B: candidate files + state
  +-- agent C: candidate files + state
  +-- verifier/integration workspace: selected, checked result
```

```python
from statetree.workspace.branches import BranchManager
from statetree.runtime.parallel import SpeculativeCoordinator

def verify(path, state, phase):
    return run_project_checks(path)  # Return exactly True on success.

manager = BranchManager(repo_path, verifier=verify)
manager.initialize(runtime.get_state().to_dict())
report = SpeculativeCoordinator(manager, ledger=runtime.usage, max_workers=3).run(
    {"approach-a": worker_a, "approach-b": worker_b, "approach-c": worker_c},
    run_id="experiment-001",
    score=lambda candidate: candidate["result"]["score"],
)
if report["winner"] is not None:
    selected_workspace = report["revision"]["workspace_path"]
```

Each worker receives a `Branch` with `path` and detached JSON `state`. It returns `{"state": ..., "result": ...}`. Use `branch.path` explicitly; do not change the process-wide current directory. Verifier callbacks come from the application and check both candidates and their integrated result in separate worktrees. Ties use strategy name order. Failed candidates remain inspectable. All-fail and stale-parent outcomes preserve canonical state.

The manager publishes its own canonical portable revision and returns a retained integration worktree. It does not overwrite your dirty checkout or silently change the legacy runtime's opaque checkpoint ref. Consume the selected worktree/state explicitly. Tests ensure staging and caller files remain intact.

`integrate(candidate_id, expected_parent=...)` performs a conservative rebase/merge against current canonical state, then verifies and atomically publishes. It recursively merges disjoint JSON object fields and disjoint file changes; conflicting arrays/scalars or different edits to one file require a decision. It does not claim automatic semantic code merging. Declared `file:path` reads use actual blob versions and require canonical Git paths, including exact filename case; aliases such as `./file.txt` are rejected. Other resource versions are application supplied. Stale declared reads/writes fail. Uninstrumented external reads cannot be inferred: the application must declare dependencies.

Worktrees do not isolate processes, databases, credentials, network services, or malicious code. Workers are trusted local callables and must avoid uncontrolled external effects. Cancellation and token limits are cooperative. Bound `max_workers` and `max_branches`; call `branch.check_budget()` before each model request and `branch.record_usage(...)` immediately after each request, including errors. Usage survives losing branches and worker exceptions. In-flight calls can exceed a between-call threshold; unknown usage blocks subsequent budget-controlled calls.

## What AWS work remains

Deploy the chosen Qwen models on SageMaker AI using the deployment guide; test actual endpoint/tool compatibility; run matched live benchmarks and record all input/output/cache/verification/retry/loser usage; then consider remote storage and application hosting. SageMaker GPU hosting cost depends on provisioned capacity and uptime. The local examples and tests establish behavior, not a measured percentage of real token or dollar savings.
