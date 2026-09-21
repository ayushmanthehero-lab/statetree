# Local project workbench — StateTree 0.3.0

This release connects the existing StateTree runtime to a `Project` Python API,
a JSON-first CLI, and a local web interface. The existing cloud benchmark,
private task-chat worker, deployment templates and native Strands APIs remain.
The local workbench is a separate entry point; it does not redeploy that site.

## 1. Start from the delivered source

Use Python 3.11 or later and Git on your PATH. Run commands from the extracted
`statetree-completed` directory. The archive contains source, not a virtual
environment, model weights, credentials, or an already-running server.

PowerShell, without changing the script-execution policy:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[test]'
.\.venv\Scripts\python.exe -m statetree doctor
.\.venv\Scripts\python.exe -m statetree init --git --goal "Build and maintain StateTree"
.\.venv\Scripts\python.exe -m statetree serve
```

Linux/macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m statetree doctor
.venv/bin/python -m statetree init --git --goal "Build and maintain StateTree"
.venv/bin/python -m statetree serve
```

The terminal prints a loopback URL, normally `http://127.0.0.1:8765`, and a
random local access token. Open that URL on the **same computer** and paste the
token into the login form. Keep the terminal running. Ctrl+C stops the server.
There is no public hosting, tunnel, AWS deployment or model invocation in this
startup sequence. The browser token is held only in the current tab's memory.

`init --git` bootstraps an empty Git baseline when needed. It does not make a
normal Git commit of your source. StateTree snapshots do capture eligible
working files, including untracked source, in local pinned Git objects. Existing
staged changes are preserved. Add secrets to `.gitignore` **before** initialization;
ignore rules do not protect secrets already tracked by Git. Never commit `.env`
files, real keys, `.statetree` state, or a virtual environment to your public repo.

For a different project, install StateTree once and supply the global repository
option **before** the command:

```powershell
python -m statetree --repo 'D:\my-project' init --git --goal "Maintain my application"
python -m statetree --repo 'D:\my-project' serve --port 8766
```

Do not run `init` on every restart. Reuse the same repository and `.statetree`
directory. After a restart, run `status` or `serve` to reopen the saved state.
If initialization was interrupted after its first checkpoint but before the
configuration file was published, repeating `init` with the **same goal** recovers
that narrow publication gap without creating a duplicate initial checkpoint.

## 2. Run the no-model end-to-end demonstration

This command works from the source root with Python and Git even when the
Strands SDK is not installed. Use a **new directory outside your source repo**:

```powershell
python -m statetree demo --directory '..\statetree-local-demo'
```

The demo creates disposable Git repositories, checkpoints an authored policy,
records facts, prepares bounded context, compacts and restores history, resumes
a completed durable step without repeating it, records a public-state model
binding, runs two local strategies, independently verifies their candidate
worktrees, adopts the selected integration, and imports a portable state bundle.
It saves `demo-report.json` in the directory you selected and refuses to overwrite
an existing directory.

**It makes zero model calls.** The conversations and strategy scores are
explicit authored fixtures. Context comparisons report serialized UTF-8 bytes,
not provider tokens, model accuracy, billing savings, speedups, electricity or
water savings. Successful recovery and merge checks are actual local executions.

## 3. Use the dashboard

**Timeline:** checkpoint notes, verification labels, restore confirmation,
paginated history and evidence pages. Restore does not reset the usage ledger or
silently change the last verified main reference.

**Memory:** current facts, evidence references, dependency versions, keyword
recall, archive and invalidate actions. The stored fact history is retained while
only bounded active facts are prepared for inference.

**Branches:** create real isolated worktrees, capture completed candidates,
verify and integrate a candidate, and explicitly adopt the result into main.
The worktree path is shown so you can edit the correct folder. Namespaced
speculative branches remain visible as retained evidence.

**Context lab:** inspect a bounded request preview without inference, select a
new-task boundary, and compact the active conversation with original evidence
retained. The SDK-free preview omits the live agent's tool schema and
provider-specific framing. The actual inference hook performs its own final
budget check including registered tool specifications.

**Local chat:** invokes the real Strands SDK and the operator-configured local
model. The only model tool exposed here is paginated archive reading. This is
not an unrestricted coding agent or shell. For isolated code-editing tasks and
networkless Docker command execution, use the retained original task-worker
workflow documented in the main README.

**Settings & portability:** inspect operator configuration, export a validated
public-state bundle, and import a trusted bundle into a new checkpoint. Browser
requests cannot choose arbitrary shell commands, model URLs, or filesystem paths.

## 4. Configure real inference and verification

`examples/project_config.json` is a starting configuration for this repository.
Supply it on first initialization with `--config examples/project_config.json`,
or carefully update the same fields in `.statetree/project/project.json` while
no operation is running. The generated `run_id` is persistent; keep it to retain
the same run-scoped token threshold. Configuration is re-read at each mutating
project boundary. The full model configuration has four required fields.

Verification commands are operator-defined **argv arrays**, not shell strings:

```json
{
  "verification_commands": [["python", "-m", "pytest", "-q"]],
  "verification_timeout": 300
}
```

Install the configured test runner and use a command appropriate for the target
project. The `python` executable in this example must resolve to the environment
with your dependencies. An absolute Python executable path is also accepted.
Verification timeouts are 1–3600 seconds per command. A missing command, nonzero
exit, timeout, or modification of the checked captured files prevents promotion.
Command output is archived with a bounded tail and its original byte count.
Checks run in an isolated worktree but **are trusted local programs, not a
security sandbox**. Configure checks outside model- and browser-controlled input.

```powershell
python -m statetree checkpoint "Export policy tests pass" --verify
```

No command is considered verified by default. Plain checkpoints remain
unverified, even when their note says that work was successful.

For inference, start your own compatible model server separately. StateTree does
not download weights or start Qwen. Bind the model ID expected by that server:

```powershell
python -m statetree model 'Qwen/Qwen3.5-4B' --url 'http://127.0.0.1:8080/v1/chat/completions' --max-tokens 512 --context-window-limit 8192
python -m statetree ask 'What is the agreed export retention period?' --new-task
```

Only literal loopback HTTP chat-completion URLs are accepted by this interface.
Use `STATETREE_LOCAL_API_KEY` in your process environment when the local server
requires a key. The key is not stored in project configuration. A different model
binding is a checkpointed configuration change; it does not load weights or
prove a successful handoff inference until an actual subsequent request succeeds.
The real Strands bridge and live model execution were **not validated in the
provided build environment**, where the SDK could not be installed; see the
completion report and the optional real-SDK HTTP contract tests.

The bridge limits a turn to 24 model-call preparations, uses the actual StateTree
context/usage hooks, and retains observed usage on failed turns. The total-token
limit is a **between-call threshold for the persisted run**, not a hard billing
cap. A request already in flight may exceed it. Unknown or ambiguous prior usage
blocks further budget-enforced requests instead of being treated as zero. The
usage overview is the whole-project ledger, including other run IDs and branches.

## 5. CLI examples

All successful commands print JSON. Errors print JSON to stderr and return a
nonzero exit status. `python -m statetree --help` lists the commands.

```powershell
python -m statetree remember export.retention_days 7 --evidence 'caller:approved-policy'
python -m statetree checkpoint 'Completed export files are deleted after 7 days'
python -m statetree recall 'export retention period'
python -m statetree context 'What is our retention period?' --new-task
python -m statetree facts
python -m statetree history --limit 20
python -m statetree usage
python -m statetree export '..\statetree-state.json'
python -m statetree import '..\statetree-state.json'
```

`remember` takes a JSON value. Numbers such as `7` need no special quoting; a
string value must be supplied as a valid quoted JSON string. Dependencies map
fact keys to their observed integer versions, for example
`--dependencies '{"port":1}'` using the quoting rules of your shell.

For branches:

```powershell
python -m statetree branch fork retention-experiment
# Edit the returned worktree path, not main.
python -m statetree branch complete retention-experiment
# Use the full candidate ID returned above:
python -m statetree branch merge FULL_CANDIDATE_ID
# Main remains unchanged until this confirmed operation:
python -m statetree branch adopt FULL_REVISION_ID --yes
```

The branch coordinator has its **own** canonical ref, seeded from the main
project when it is first used. Subsequent forks start at that coordinator's
canonical state, not automatically at every later main checkpoint. Main state
or captured-file divergence is rejected during adoption; it is not silently
rebased or overwritten. Resolve that divergence deliberately before adoption.
Declared resource reads/writes use the existing conservative merge rules.

A main checkpoint can be restored with
`python -m statetree restore FULL_CHECKPOINT_ID --yes`. Captured files and state
are restored; unrelated later untracked files are preserved. External databases,
network requests, running processes and already-consumed tokens are not undone.
A filesystem restore is not a multi-file crash-atomic transaction; after a crash,
explicitly restore the intended checkpoint again before continuing work.

## 6. Python API for durable execution and speculation

```python
from pathlib import Path
from statetree.project import Project
from statetree.runtime.durable import Step

project = Project(Path("/absolute/path/to/project"))

def write_report(arguments, idempotency_key):
    # The caller owns idempotency. For a remote side effect, pass this key to
    # a service that actually supports deduplication; a key alone is not enough.
    path = project.repo / "report.txt"
    path.write_text(arguments["text"], encoding="utf-8")
    return {"path": str(path)}

steps = [Step("report", "write_report", {"text": "Complete"}, mode="idempotent")]
project.run_steps(steps, {"write_report": write_report}, run_id="report-job-1")
# Reopen and repeat the same run to resume from durable receipts/checkpoints.
```

Use `at_most_once` for operations that must not be replayed automatically after an
uncertain outcome, and reconcile those outcomes yourself. Only registered tool
callables are run. This local API does not restart a dead operating-system process
or offer distributed exactly-once execution.

```python
from copy import deepcopy

def option_a(branch):
    branch.check_budget()  # Do this before every actual model request.
    # Immediately after an observed request, call branch.record_usage(...),
    # including usage=None when the provider did not report its usage.
    state = deepcopy(branch.state)
    (branch.path / "candidate.txt").write_text("Option A", encoding="utf-8")
    return {"state": state, "result": {"score": 1}}

report = project.speculate(
    {"option-a": option_a}, run_id="exploration-1",
    score=lambda candidate: candidate["result"]["score"],
)
# Inspect report["attempts"] and report["revision"] before explicit adoption.
```

Scores in this example are application values, not a claim of model evaluation.
Speculative callables are trusted/cooperative. Worktrees do not isolate external
side effects; use `branch.path` explicitly, never process-wide `os.chdir` in a
concurrent worker. Losing and failed attempts still count in the usage ledger.

## 7. Persistence, compatibility and validation

The new workbench stores its SQLite metadata, immutable snapshots, archives and
branch worktrees under `.statetree/project`. Native `StateTreeRuntime` sessions
from the original code continue to use their configured storage and real SDK
snapshot decoder. They are not silently converted to the new public-session
format. Use portable **public-state** export/import to transfer declared state and
evidence; a bundle is not a backup of Git objects, every checkpoint note, private
provider sessions, operating-system processes or all undeclared archive blobs.

Archive contents and exported public state can contain sensitive project data.
They are not automatically redacted or encrypted. Import only trusted bundles,
protect backups, and treat all recalled evidence as data rather than instructions.

```powershell
python scripts/test_core.py        # Explicit local-core subset; see its scope.
python -m pytest -q                # Full suite with the real pinned SDK installed.
python -m compileall -q statetree deploy benchmarks examples tests
```

The core runner names its exclusions. It is not a substitute for the full suite.
Optional Playwright/Chromium tests exercise real forms through an authenticated
loopback HTTP bridge because the build environment's managed browser blocks
direct loopback navigation. Separate HTTP tests verify authentication, headers,
request validation and origin restrictions. This is not a public-site deployment
test. The added CI workflow runs the full suite on Python 3.11, 3.12 and 3.13 when
pushed to GitHub; that remote workflow was not run during this build.
