# Local Agent Chat Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for independent units and test-first implementation. Preserve the existing workspace; do not stage or commit unrelated work.

**Goal:** Ship an authenticated task chat controlling a local project worker, including same-prompt recovery from persistent checkpoints.

**Architecture:** The existing ECS website stores private jobs in S3 and relays model requests through the existing SageMaker gateway. An outbound PC worker runs real Strands tools in a task workspace, journals effects and checkpoints StateTree state. The browser renders actual task events and diffs.

**Tech Stack:** Python 3.13, installed Strands 1.56.0, standard-library HTTP, boto3, Git, local Docker, plain HTML/CSS/JavaScript.

**Spec:** `docs/superpowers/specs/2026-09-20-local-agent-chat-design.md`.

## Global constraints and execution ruling

- User approved the design and explicitly requested fast implementation before 20:00 IST, including kill/restart/same-prompt recovery. Proceed with parallel implementation rather than another planning approval round.
- Work in `D:\aws hackathon\statetree`. Its many uncommitted modules are the implementation baseline; a clean HEAD worktree would omit them. Do not alter their staging or commit them incidentally.
- AWS mutations follow local verification. Keep the existing benchmark routes and stored reports usable.
- Owner and worker credentials are distinct from the old demo access code. Project data is private.
- No model-controlled host shell; commands use a task-only Docker mount. Worktree separation is not a security sandbox.
- All model use, including retries and child agents, is accounted for. Unknown usage stays unknown. No promised percentage reduction for arbitrary prompts.

## Shared HTTP contract

All JSON, bounded bodies, no credentials in URL. Owner calls use `X-Owner-Key`; worker calls use `X-Worker-Key`. Task IDs are 32 lowercase hex characters. Project IDs are the first 32 hex characters of SHA256 of normalized absolute local project path; only ID/name leave the PC.

Owner endpoints:

```text
GET  /api/chat/state -> {projects:[{id,name,last_seen,online}],tasks:[task]}
POST /api/chat/tasks {project_id,prompt,fresh?:bool,task_id?:string} -> {task:task,resumed:bool}
GET  /api/chat/tasks/<id> -> {task:task}
POST /api/chat/tasks/<id>/cancel {} -> {task:task}
POST /api/chat/tasks/<id>/apply {} -> {task:task}
```

Worker endpoints:

```text
POST /api/worker/poll {worker_id,projects:[{id,name}],active_task_id?:string,lease?:string}
 -> {task:task|null,lease:string|null,cancel_requested:bool,apply_requested:bool}
POST /api/worker/tasks/<id>/events {worker_id,lease,events:[event],status?:string,checkpoint?:object,result?:string,usage?:object,diff?:string}
 -> {task:task,cancel_requested:bool,apply_requested:bool}
POST /api/worker/tasks/<id>/inference {worker_id,lease,payload:OpenAIChatJSON} -> OpenAIChatResponseJSON
```

Task fields: `id,project_id,prompt,status,events,checkpoint,result,usage,diff,created_at,updated_at,resume_count,cancel_requested,apply_requested`. Backend additionally owns worker/lease state. Event fields: `id,type,text,details,created_at`. Types include message, tool_start, tool_result, checkpoint, usage, agent, status, error. UI displays unknown event types as plain activity. Event IDs deduplicate retries; all rendering uses text nodes.

Same normalized project/prompt attaches to an active task, queues an interrupted/failed/cancelled task for explicit resume, or returns a completed result. `fresh:true` creates another task. Active expired leases become interrupted, never automatically replayed. A local lock and journal fence execution independently of cloud state.

## Review focus

1. A worker dies after an edit but before cloud acknowledgement: confirmed edit must not repeat; a command of uncertain outcome must not automatically replay.
2. Two tabs or overlapping ECS workers submit/claim the same task: conditional storage must avoid duplicate task execution.
3. Paths contain traversal, symlinks or Windows junctions: tool access must not leave the task workspace or reveal secrets.
4. The PC is offline or the model truncates/times out: chat must report the true interrupted/error state and retain evidence.
5. User changes original files before apply: retain both versions and report a conflict rather than overwrite.

## Task 1: Private cloud job API

Files: create `statetree/web/chat.py`, `tests/test_chat_api.py`; modify `statetree/web/app.py` only for routing/configuration integration.

Produces `ChatApplication(owner_key,worker_key,store,inference)` and a bounded request dispatch interface integrated with existing HTTP handlers. Store implementations support local testing and S3 conditional update of a private chat state object. `inference(payload)` is injected; production uses existing SageMaker invocation, tests use a deterministic transport.

- [ ] Write failing tests for owner/worker separation, repeat-prompt resume, fresh execution, durable reload, stale leases/events and CAS collisions.
- [ ] Implement state transitions and the contract above; preserve `/api/runs` benchmark behavior.
- [ ] Verify private reads require owner credentials and unknown/oversized fields cannot bypass request constraints.
- [ ] Run `python -B -m unittest tests.test_chat_api tests.test_hybrid_web -v` and record results.

## Task 2: Local task engine, tools and recovery

Files: create `statetree/agent/__init__.py`, `statetree/agent/runner.py`, supporting focused modules in that package, and `tests/test_agent_runner.py`.

Produces this interface:

```python
LocalTaskRunner(project_path, state_root, model_factory,
                event_sink=None, cancel_requested=None,
                command_image='python:3.13-slim')
# event_sink(event_dict); cancel_requested() -> bool; model_factory() -> Strands Model
runner.run(task_dict)  # dict: status,result,checkpoint,usage,diff
runner.apply(task_id)  # dict with applied/conflict result; preserve caller staging
```

The engine owns local task identity, journals, process locks, task workspaces, file/command tools, real bounded child inspection/review, and checkpoints. It must not own HTTP/AWS credentials. Model factory is injected by the transport layer.

- [ ] Write failing tests for a real agent/tool loop using deterministic test model, checkpoint continuation, deduplicated edit receipt, unknown command outcomes, path/link rejection and apply conflicts.
- [ ] Implement minimal tools and a serial checkpointable Strands loop. Use installed SDK per-turn limits/checkpoints rather than private scheduler assumptions.
- [ ] Persist state before each external effect and receipts immediately after it. Resume from condensed notes plus current task/receipt state, retaining archived evidence.
- [ ] Verify same task in a new process resumes and cannot race another process holding the local lock.
- [ ] Run `python -B -m unittest tests.test_agent_runner -v`; record any live-model limitations separately.

## Task 3: Task chat interface

Files: `statetree/web/static/index.html`, `style.css`, `app.js`; retain original benchmark assets under distinct names/routes coordinated with Task 1.

Consumes the HTTP contract above. Implement project/conversation sidebar, owner login, prompt composer, live tool timeline, stop/resume/fresh controls, diff/checkpoint/usage inspector and offline status. No framework dependency or synthetic progress. Preserve report links by routing `#run=<id>` to the benchmark view.

- [ ] Implement accessible semantic controls and responsive chat layout.
- [ ] Check all dynamic text is rendered as text, secrets are not persisted in browser storage, and polling never resubmits a task.
- [ ] Validate JavaScript syntax and verify frontend contract against Task 1; use a real browser if available.

## Task 4: Worker transport, credentials and model limits

Files: create `deploy/local_agent.py`, `tests/test_local_agent.py`; modify `deploy/local_qwen.py`, `deploy/hybrid.py`, `deploy/hybrid_template.py`, `deploy/hybrid_gateway/server.py` and focused tests.

- [ ] Add failing tests for separate owner/worker key migration without rotating existing keys, bounded relay payload and worker request authentication.
- [ ] Build an outbound worker with stable worker identity, poll/heartbeat, event retries, cancellation and authenticated model relay. It calls the Task 2 interface and publishes only bounded public task fields.
- [ ] Make model output limit configurable; test 256-token starting limit and retain the gateway deadline. Do not raise limits without matching launcher/gateway/client behavior.
- [ ] Grant web storage only the added private chat prefix. Keep secrets out of build contexts and logs.
- [ ] Add local CLI preparation/status/serve documentation and graceful shutdown behavior.

## Task 5: End-to-end evidence and release

- [ ] Run focused suites, existing hybrid regression suite and independent review; address blockers.
- [ ] Run a local real-Qwen task on a disposable project and verify an actual edit/test/checkpoint.
- [ ] Kill a worker subprocess after a confirmed checkpoint, resend the same prompt, and verify continuation with preserved work and measured usage.
- [ ] Confirm container boundaries with actual Docker and inspect live UI if browser tooling permits.
- [ ] Publish/update the existing AWS stack after local checks, then verify private chat, worker heartbeat, a real task and preserved public benchmark report.
- [ ] Record reproducible commands, limitations and actual checks in the deployment guide/verification record; give the user the working URL and startup commands.

## Progress ledger

- 17:30 IST: approved design updated with same-prompt recovery; deadline 20:00 IST. Existing runtime and web code audited. Current checkout retained to preserve uncommitted baseline.
- Task 1: implemented private API/storage; API/worker/web integration reached 44 passing tests. Added follow-up `parent_task_id`, per-project claims, late-cancel reconciliation and 2000 UTF-8 byte prompt bound during review.
- Task 3: implemented chat and preserved benchmark assets; actual headless Edge desktop/mobile/login/activity checks passed. Screenshots and browser-only fixture harness are under ignored `build/hybrid`. No fixture activity is product code.
- Task 4: outbound worker, persistent event outbox, separate credentials, bounded model allowance and private S3 IAM prefix implemented. Existing keys preserved. Unit tests and real loopback relay tests pass; live checkpoint/usage events now update top-level task state immediately.
- Task 2: engine implements real Strands tools/checkpoints, process locks, filtered private project copies, isolated Docker checks, parent task inheritance and explicit apply. Independent review found cancellation, context-size, file-handle and crash-fencing gaps; fixes/tests are in progress.
- Task 5: live native model accepts 128-token replies without restarting the user's process. Initial real task timed out; warmed retry read the real file but the next request timed out at 55 seconds. Diagnostic replay returned the correct edit call in 18.8 seconds with 2411/2415 input tokens cached. Worker transport now places changing StateTree data after stable system/tool definitions to preserve prefix caching; full live recovery verification remains pending. No updated AWS hosting deployed yet.
