# Reuse checkpoint notes as task context

StateTree can retrieve relevant, caller-written checkpoint notes for a later task. Notes retain their checkpoint provenance and references to the original evidence. Retrieval is local and deterministic; no model writes a summary.

Run the self-contained demo from the repository root using the existing environment:

```powershell
.\.venv\Scripts\python.exe -B -m examples.commit_context
```

The demo creates and removes a disposable Git repository, seeds representative completed conversations, and uses a fixed local provider for two replies. It checks that a password-reset query selects authentication notes and excludes unrelated UI notes, compares serialized requests with full history and with a new-task boundary, and retrieves the original conversation. It needs no credentials, AWS configuration, installations, or remote model requests.

## Write notes at a completed boundary

Configure an agent as shown in the [README](../README.md#attach-an-agent), then enable bounded recall:

```python
from statetree.memory.commits import CommitNote
from statetree.runtime.runtime import StateTreeRuntime

runtime = StateTreeRuntime(
    agent, goal="Maintain the application", repo_path="/path/to/repository",
    max_input_tokens=12000, commit_context_budget=3000, recent_turns=1,
)
agent.state.set("statetree_context", {
    "constraints": ["Preserve the existing authentication validation rules"],
    "active_subgoal": "Implement password reset",
    "resources": {"auth.py": "v1"},
})

# After the agent finishes its work, supply your own factual note.
checkpoint = runtime.commit(note=CommitNote(
    summary="Login authentication uses shared validation",
    changes=["auth.py"],
    decisions=["Password reset must reuse authentication validation"],
    pending=["password reset"],
    dependencies={"auth.py": "v1"},
    outcome="completed",
))
# A short summary is also supported:
# checkpoint = runtime.commit(message="Login validation implemented")
```

Use `message` or `note`, not both. A structured note also accepts `evidence_ids=[archive_id]` and `supersedes=[older_checkpoint_id]`; its outcome is `completed`, `partial`, or `failed`. The runtime automatically archives the current messages and supplied verification as evidence when writing a note. Referenced evidence must exist locally. A note is immutable: write a new checkpoint note and declare `supersedes` when a previous note should stop being recalled.

Notes live in local sidecars bound to existing checkpoint IDs. They do not change the checkpoint manifest or its hash, and are not included in portable state bundles. Keep the local store to retain notes and their evidence.

## Inspect recall and start the next task

```python
selection = runtime.recall("password reset authentication validation")
for note in selection.notes:
    print(note["commit_id"], note["summary"], note["evidence_ids"])
print(selection.estimated_count, selection.counting_method)

# This invokes your configured model; only the demo uses a local scripted one.
runtime.run("Implement password reset using the existing validation", new_task=True)
```

`recall(query, budget=None, resources=None, limit=8)` searches notes reachable from the current checkpoint ancestry. Restored-away descendants and sibling branches are excluded. Explicitly superseded notes are excluded. Matching uses words, not embeddings or semantic/fuzzy similarity: use concrete terms such as file names, feature names, and pending work in notes and requests.

Dependency values are caller-maintained version strings. By default, recall compares each note's `dependencies` with the string-valued entries in the `resources` mapping in `agent.state.get("statetree_context")`; other resource metadata is ignored for matching. Pass `resources={...}` containing version strings to override that lookup for manual recall. A missing or mismatched dependency excludes the note. StateTree does not infer versions by reading files: update the resource mapping when the dependency changes.

A positive `commit_context_budget` enables automatic recall for each new user request. The default is `0`, which disables automatic recall. Note selection has its own budget and must also fit inside the whole-request `max_input_tokens` budget. Goal, current state, system prompt, tool schemas, and required recent messages take precedence over optional notes; selected notes may be omitted if the complete request cannot fit them.

`run(..., new_task=True)` marks an explicit boundary after a completed conversation. It archives the prior messages before replacing that history with current state, relevant notes, and a retrieval reference. Unfinished tool calls are rejected. Cancellation or failure before a complete model response restores the previous conversation and archive pointer; if a later step fails, completed model/tool progress is retained. Usage accounting is never rolled back. The default `new_task=False` retains the conversation under the existing context budget policy.

An explicit new-task boundary requires a positive `commit_context_budget` and a bounded context. Its history archive ID is available through `agent.state.get("statetree_prior_history")`. The first archive contains the previous message list. Later archives contain `messages` and `prior_history_archive`, linking earlier tasks so their evidence remains discoverable.

Put durable requirements and constraints in `statetree_context`, and update the active task before starting another task. A checkpoint summary is historical evidence, not a substitute for pinned requirements or the newest user instruction. Neither `outcome="completed"` nor a checkpoint's historical `verification_status` proves that the current files still satisfy those claims. Run appropriate current checks when correctness depends on them; `verified=True` still requires caller-supplied `verification={"passed": True, ...}`.

## Read the original evidence

The runtime registers `statetree_read_archive` with bounded contexts. The agent can call it using a note's `evidence_ids` or a history archive reference, starting with `offset=0` and a `limit` between 1 and 4096 characters. Continue at `next_offset` until it is `None`. In application code, `runtime.store.read_archive(archive_id)` reads the complete original JSON.

The default counter measures serialized UTF-8 bytes, including the system prompt and tool schemas. API names such as `max_input_tokens` and `estimated_input_tokens` describe a preflight estimate, **not actual model tokens**. A custom `counter(serialized_request) -> int` can supply another estimate. The demo reports exact byte lengths for its captured request serialization and preflight estimates separately; SDK bookkeeping can make those estimates larger. Provider framing, token usage, billing savings, latency, and task-success improvements are unmeasured.
