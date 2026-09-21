# StateTree Agent tasks — automatic checkpoints and resume

This update adds a durable local coding loop to the working GTX 1650 edition.
The **Agent tasks** tab is separate from the older **Local chat** tab. Local chat
remains archive-only; use Agent tasks for real file edits and command execution.
The existing model download/retry and positive-GPU-offload fixes are included.

## Update an existing Windows installation

First press **Ctrl+C** in the terminal running StateTree and Qwen. Wait for both
services to stop. Keep the existing `.statetree` and `.venv` directories intact.

Download `statetree-agentic-update.zip` and `apply_agentic_update.py` into your
Windows Downloads folder. Do not extract the update ZIP. In PowerShell, from the
existing project directory, run:

```powershell
cd 'D:\aws hackathon\statetre gpu\statetree-gtx1650'
& .\.venv\Scripts\python.exe "$env:USERPROFILE\Downloads\apply_agentic_update.py" --project . --zip "$env:USERPROFILE\Downloads\statetree-agentic-update.zip"
```

The updater validates every payload checksum and checks the current source against
the expected earlier version. Unknown local source edits stop the update before
any source changes. It creates a source-only backup outside the project, normally
under `%LOCALAPPDATA%\StateTree\source-update-backups`. The model cache, installed
Python environment, checkpoints and task database are not replaced. A repeated
run recognizes files already updated. This is not a crash-atomic multi-file
upgrade: after an interrupted update, keep the server stopped and run it again.

Restart from that same directory:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Start-GTX1650.ps1 -MaxTokens 512
```

No model download or dependency reinstall is required for the existing editable
installation. Open the printed workbench address, enter the terminal's token,
and select **Agent tasks**. Hard-refresh the browser (Ctrl+F5) if the tab is absent.
The full source ZIP is also provided, but it is not necessary for this update.

## Give the agent work

The project path shown in Agent tasks is where file tools operate. Native code
executes with that directory as its working directory. These are your real files,
not a disposable copy. Start with a trusted test project or a small safe task.

For a first test, enter:

> Create agent_smoke/hello.py that prints "StateTree agent works". Actually write
> the file with your tools, run it with Python, and report the observed output.

Select **I trust this task: allow native code and build commands on this
computer**, then **Run / resume automatically**. Consent is saved for that task,
so resuming it does not require approving each command. File tools work without
native execution consent. To change execution permission, explicitly start a new
task with the correct checkbox; merely resending text cannot escalate permission.

The model can use `list_files`, `read_file`, `search_files`, `write_file`,
`edit_file`, `run_command`, `update_plan` and `read_evidence`. `python` resolves to
StateTree's current Python environment. Other executables must already be
installed and available in the command environment. Commands are argv arrays,
not implicit shell strings. This update does not add browser/computer control,
external account connectors, paid APIs, automatic deployment or package setup.

**Native execution is not sandboxed.** Python, PowerShell and build tools can
access files and networks with your Windows account's permissions. File-tool
path checks do not confine executed programs. Windows job objects manage process
lifetime, not privileges or filesystem/network access. Only enable execution for
work and source material you trust. The retained original Docker worker is a
separate execution path; this new local mode does not require Docker.

## What is automatic

For each tool operation, StateTree saves its intent before execution, then a
receipt of the observed result, then a Git-backed **StateTree checkpoint** of
public project state and eligible files. It emits these events in the dashboard.
Errors are also saved as evidence; a model saying "done" is not a passed test.
The task worker runs independently of the browser's request. Closing or refreshing
the tab does not discard a task or stop it while the server stays running.

Resending the **same whitespace-normalized message** reopens its saved task ID.
An active task is not launched twice. An interrupted task reconciles saved work
and continues; an already-completed task returns its saved answer. Select the
**New task** checkbox only to intentionally do that same task again.

After a network/model failure, there are up to three attempts before pausing.
After closing the terminal or restarting Windows, restart the launcher and select
**Resume saved task**, or resend the original message without New task selected.
No new task instructions or manual commit command are needed to resume.
Only one task runs per project. Server startup alone does not silently start
inference for old tasks. A browser network interruption cannot be repaired until
the browser can reconnect; persisted progress stays on disk meanwhile.

`Pause task` requests a safe stop. A model request already in flight may finish,
but no later tool starts while the pause flag is set. A running native command is
stopped by its receipt worker; it may already have made partial changes. Ctrl+C
still stops the workbench and owned model server. A hard process kill can leave
an independent command worker briefly finishing/recording its existing operation;
resume waits for its receipt rather than launching the command again.

These checkpoints are not automatic GitHub pushes, releases, deployments, or
ordinary commits to your checked-out Git branch. They use StateTree's existing
checkpoint storage. They do not undo external effects, consumed tokens or
unrelated untracked files. Do not run another editor/agent/CLI mutation against
the same project while a task is changing it; external concurrent edits are not
made safe by this feature. Rewinding the project head blocks stale-task resume.

## Compact context instead of transcript replay

Each model request contains the original task, fixed tool instructions and
schemas, a bounded continuation note, and the newest bounded tool evidence.
The automatic note contains the task ID, saved-step count, recent receipt IDs,
changed-file hashes, model-authored plan/next action when supplied, and pointers
to older evidence. Older output is retrieved with `read_evidence` on demand.
It does **not** replay the complete chat or all past tool output.

A separate summarization model call is not needed: the default continuation note
is assembled from recorded operations. The model can improve its task plan with
`update_plan`, which is stored as model-authored planning rather than verified
fact. A single vague commit title is not enough to resume arbitrary work reliably;
this implementation keeps structured evidence behind the concise note.

The pinned llama.cpp server formats the prompt including the tools and tokenizes
it before generation. The runner reduces evidence detail when necessary and
reserves output capacity. A still-too-large original task pauses with an error;
it is not silently cut down. No actual provider token-saving percentage or model
quality improvement is established by the fixture tests.

## Recovery boundaries

Saved completed receipts are reused for the same persisted operation ID. File
write recovery compares current content with saved before/after SHA256 hashes;
it confirms an already-written file without writing it again. This is a replay
guard, not a guarantee that the model will never choose a similar new action.

When a native command's effects are uncertain, or a stopped command has possible
partial effects, the task enters **needs_review** instead of running it again.
Inspect the affected files/processes/external system, record what actually
happened in the task's review form, confirm, then resume. The original receipt
is retained; your observation is separately labelled operator-provided.
Do not claim success in that form unless you verified it.

No local checkpoint system can infer an unacknowledged external side effect from
a short summary alone. For example, a command may have sent a request just before
a crash. External exactly-once behavior requires the target's idempotency or
reconciliation support. This update does not promise arbitrary exactly-once
execution or automatically undo a partly executed command.

## Limits and storage

The default activation allows 100 model preparations, then pauses; Resume starts
a further activation without discarding progress. Native commands default to
60 seconds, accept at most 600 seconds, and retain up to 32 KiB of combined output.
File reads and receipt reads are paged; file tools use the existing protected-path
rules and project file-count limit. Very large files/projects may need splitting.

The existing launcher defaults to 256 output tokens. The recommended restart
above uses 512 for small code/tool calls while retaining the 4096-token context.
If a reply is cut off, no truncated tool call is executed. Restart with
`-MaxTokens 1024` and resume, or ask for smaller edits. More output allowance also
leaves less input room; the GTX 1650's working placement/speed is not re-benchmarked.

Task identity, pending actions, immutable receipts, compact notes and event
history are in `.statetree/project/agentic/tasks.sqlite3`. Git-backed checkpoint
state and evidence use the original StateTree storage. Keep `.statetree` across
restarts. The task journal is deliberately outside checkpoint rollback and is
not included in public-state portable bundles. Back up the whole local project
and its `.statetree` directory when stopped for machine-to-machine recovery.
Token usage remains in the existing ledger; tokenizer previews are not generation
usage. A generation with unknown usage remains unknown and may block a configured
budget. Between-call thresholds are not hard billing caps.

## Verification boundary

See `AGENTIC_VALIDATION.md` for actual commands and results. Tests use scripted
model responses with real files, harmless Python processes, SQLite, Git and local
HTTP/browser controls. They do not show that Qwen reliably completes every coding
task. Actual Qwen inference, Windows command/job-object behavior and your GPU
performance have not been executed in this container. Nothing is pushed to GitHub
or redeployed to AWS by this update.
