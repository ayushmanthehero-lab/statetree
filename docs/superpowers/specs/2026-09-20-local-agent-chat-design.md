# StateTree task chat with a local project worker

Status: approved by the user on 20 September 2026, with same-prompt interruption recovery added below. Implementation is authorized; the user requested fast completion before 20:00 IST. This document does not claim that task chat is implemented.

## Intended outcome and confirmed choice

The user wants a chat application where they submit a task and watch an agent use tools, edit project files, run checks and return a result. They chose a project folder on their PC, controlled through the website. StateTree's central benefit remains retaining checkpoint evidence and recalling concise task notes instead of repeatedly sending all prior conversations.

Success means a real task such as fixing a failing test leads to actual inspected files, a reviewable patch, recorded test output and a checkpoint. A later task can retrieve that checkpoint's note and original evidence. Merely displaying a chat-shaped interface or simulated agent activity does not satisfy the request.

## Current implementation boundary

- `statetree/web/app.py` accepts only an empty JSON object to start a fixed comparison. `demo.py` uses a seeded question in a temporary repository. There is no project chat API.
- `StateTreeRuntime` provides real Strands invocation, checkpoint/restore, context hooks, archive retrieval and usage accounting. Project coding tools are not registered.
- `SpeculativeCoordinator` runs application-provided worker callables in Git worktrees. There is no model-driven subtask delegation application yet.
- The gateway and native model launcher cap replies at 64 tokens. The cloud demo is not a validation of general coding performance.

## Architecture decision

Use an outbound local worker: the browser submits authenticated jobs to the existing ECS service, and a worker on the PC polls for them. Only the local worker handles project files and tool execution. It registers a locally selected folder under an opaque project ID; a browser request cannot supply an arbitrary host path.

This fits the user's chosen local-project workflow and avoids an additional inbound endpoint for computer control. Two alternatives were considered: exposing a local tool server through another public tunnel increases the exposed control surface; an AWS project workspace requires moving the project and execution environment away from the selected PC workflow.

The Strands agent loop lives in the worker. Its model transport sends authenticated inference requests to the ECS service, which invokes the existing SageMaker gateway and Qwen tunnel. AWS credentials remain with the cloud service and outside project command containers. The worker holds a separate, revocable worker credential. The current Cloudflare tunnel continues to carry model inference only.

## User interface

- A left sidebar lists registered projects and conversations, plus the local worker's online/offline state.
- The main area contains task and assistant messages, a bottom composer, a new-task action and a stop button.
- Expandable activity entries show actual file reads, searches, edits, commands, subtask delegation and tool results. No invented activity or private reasoning transcript is displayed.
- A side panel shows changed files, diffs, test results and StateTree checkpoints. Applying a finished patch is a separate action against the original project folder.
- A compact usage panel shows measured input/output/cache tokens, selected notes and all delegated/retried requests. It does not invent a baseline for an ordinary task.
- The existing fixed comparison and saved report URLs remain available as a secondary benchmark view.

## Project worker and tools

The operator registers a specific Git project locally. Each task uses a worktree based on a captured starting state, preserving the operator's dirty files and staging. Existing StateTree workspace code should be reused only after its behavior is checked against this flow. Worktrees separate file changes; they do not provide a process security boundary.

Initial model-visible tools are bounded project listing/search, file reading, exact file edits, Git diff, command/test execution, archive retrieval, checkpoint completion and subtask delegation. Paths must resolve within the assigned task worktree; escaping links, junctions and traversal are rejected. Private deployment keys, credential directories and excluded local files must not enter the task snapshot, prompt or uploaded artifacts.

Commands run in a local Docker container with only the task files mounted, no host credentials or Docker socket, no network by default, and explicit time, output and resource limits. Runtime images are selected locally and verified before a task begins. The UI reports missing dependencies; the agent cannot silently execute unrestricted commands on the host. File edits use expected-content checks so concurrent changes cause a conflict rather than an overwrite.

Serialize host file reads, edits, snapshots and apply operations against all task writers, including child agents and command containers. Wait for cancellation and container termination before releasing that lock. Resolve and validate paths while holding the lock, rejecting symlinks and Windows reparse points/junctions; use race-resistant no-follow access for operations exposed to changes by other local processes. A path validation performed before a concurrent writer runs is insufficient.

Applying a patch verifies that the original files still match the recorded base. Conflicts are displayed and the task worktree is retained. Destructive Git resets or automatic replacement of the user's checkout are not part of the flow.

## Agent behavior and memory

Same-prompt recovery is an explicit requirement: persist a normalized prompt identity per registered project. Resubmitting that prompt resumes the latest unfinished task and its checkpoint instead of creating a duplicate. A running task is attached to, not restarted. Completed tasks return their existing result unless the user explicitly starts a fresh run. Save checkpoints after completed agent/tool steps, retaining workspace state, short notes, outstanding work and receipt references. On process restart, use the concise checkpoint context and recover evidence on demand instead of replaying the full transcript. Checkpoint metadata and indispensable current state still consume context; commit messages alone cannot prove whether a side effect completed. The interruption/restart test must kill a real worker process and verify continuation without repeating a confirmed edit.

Construct a real Strands agent with the project tools and attach `StateTreeRuntime`. The agent selects tool calls from the user's task and tool results. Start with one main agent and an explicit bounded delegation tool for inspection or review subtasks. Child agents receive only their task and selected context, have no nested delegation, and return recorded results. The PC currently has one model slot, so inference requests are serialized; separate roles do not imply simultaneous model execution.

At a completed task boundary, persist changed-file references, tool receipts, tests and a concise task note. A model-provided summary is labeled as such and its generation usage is counted. Verification status comes from tool evidence; an assistant saying "passed" is insufficient. If no usable summary is produced, record a factual partial/failed note from observed receipts instead of inventing decisions.

Follow-up messages retain the active task context. Explicit new tasks use `run(..., new_task=True)` with commit recall enabled. Current instructions and pinned requirements remain authoritative. Full tool evidence stays retrievable outside the active prompt. Do not replay a full baseline alongside every real task; optional comparisons must use isolated, reproducible tasks so they do not execute user changes twice.

## Private state and job protocol

Introduce separate owner and worker credentials; the existing shared benchmark code does not authorize access to the PC. Chat, tasks, file snippets, diffs, worker APIs and project artifacts require authentication. Public benchmark report IDs must never become access paths for private project material. Credentials are not placed in URLs or logs.

Use a separate private prefix in the existing S3 bucket for job documents and immutable event batches. Create records conditionally and update job state using ETag compare-and-swap. Explicit revisions and worker lease tokens reject stale acknowledgements, duplicate claims and updates from overlapping ECS deployments. Local SQLite journals retain tool intents and receipts before acknowledging events. This is a small single-owner queue, not a claim of general distributed scheduling.

States include queued, running, waiting for input, completed, failed, cancel requested, cancelled and interrupted. A worker heartbeat expiring marks a running task interrupted; it must not automatically reissue a potentially completed command. Reconnection uploads acknowledged/missing events idempotently. Resume proceeds only from a confirmed boundary. Browser reloads reconnect to saved events without starting another task.

Cloud leases alone do not fence execution on the PC. Hold a local process-level single-writer lock bound to the project/task journal; persist each stable step identity and intent before dispatch. Do not reassign or replay uncertain work until the previous worker and command container are confirmed stopped, or their outcome has been reconciled. A lost response is not permission to repeat a command. These are refusal/reconciliation guarantees, not a claim of exactly-once arbitrary effects.

Cancellation stops new model/tool steps and terminates the task's command container. Already running remote inference can still finish and consume tokens; it must not trigger subsequent edits after cancellation. Unknown usage and uncertain command outcomes remain explicit.

S3 supports the conditional writes needed for this design; implementation must handle failed preconditions and conflicts rather than treating them as success. See [AWS conditional-write documentation](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html).

## Model and deployment constraints

Make reply/context limits explicit across the local launcher, gateway and agent transport. Test a starting reply allowance of 256 tokens with small edits and bounded tool outputs; adjust only using measured CPU latency. The current gateway deadline is 50 seconds. SageMaker Serverless allows one minute for an invocation, so extending only a browser timeout will not solve long generations. See [AWS Serverless invocation limits](https://docs.aws.amazon.com/sagemaker/latest/dg/serverless-endpoints-invoke.html).

Tasks can contain multiple individually bounded model calls. Length-truncated or malformed tool requests must not be executed as complete instructions. Timeout and capacity errors remain visible. Qwen3.5-4B on this CPU must pass actual file-edit/tool-use tests before making claims about coding task quality.

Implement and validate locally first. Publishing new images, adjusting IAM prefixes/secrets and updating the live stack are a later concrete deployment step. No new paid compute instance is proposed. Document worker setup, credential handling, startup, shutdown and recovery. The local model, worker and tunnel must be online for new work.

## Acceptance evidence

1. A freeform task fixes a small repository bug through actual read/edit/test calls; the UI shows matching events, diff and test output.
2. A later task recalls the prior checkpoint, can retrieve its evidence and records real request usage. A deliberately unrelated note is not selected merely to claim savings.
3. A requested inspection/review subtask runs a distinct real agent and its usage is included. Failures and token limits propagate honestly.
4. Browser reload and ECS restart preserve acknowledged conversation/events. Duplicate job claims cannot run a command twice; an uncertain interrupted command requires reconciliation.
5. Cancellation prevents subsequent edits, a stale apply request cannot overwrite changed user files, and the original checkout/staging remain intact until apply.
6. Unauthenticated access, benchmark credentials, path escapes, escaping links and attempts to access host secrets fail. Command isolation is verified with actual container probes.
7. Existing benchmark/report access remains functional. The redesigned interface is checked in a real browser when tooling is available; API checks alone are not labeled visual verification.

## Review boundary

This is an architecture change covering web task state, a local execution worker and their authenticated protocol. The next artifact is a concrete implementation plan after review of this design. No product code, secret, local model process or AWS resource was changed while preparing this draft.
