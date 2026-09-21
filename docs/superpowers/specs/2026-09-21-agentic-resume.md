# Local agentic execution and automatic resume

Run Qwen3.5-4B on the working GTX 1650 as a coding agent. Persist a concise
continuation note, files and receipts after each tool. Resubmitted tasks resume
without sending the entire transcript. Retain the existing GPU hotfixes and SDK
archive chat; add Agent tasks to the existing authenticated local dashboard.

Use SQLite task/operation state, an independent command-receipt process, existing
Project checkpoints and SafeFiles. Direct llama.cpp tool-call requests need no
new dependency. Only one task runs per project. UI requests return task IDs and
poll stored progress; disconnecting the browser does not cancel the task.

Exact normalized prompt retries reuse a task; New task intentionally creates one.
Persist model responses before tool dispatch, intents before side effects, receipts
before checkpoints. Reconcile file writes by before/after hashes. Never repeat a
command whose effects may have happened but lack a receipt: require review.
Completed command receipts survive coordinator death. Pausing may leave partial
command effects; checkpoint/restore is not an undo for external side effects.

Every model request carries the original objective, a bounded structured commit
note, tool definitions and the latest bounded result; older receipts are paged
on demand. Automatic notes are receipt-derived, not invented successful claims.
Model-authored plans are explicitly distinct from observed results.

File tools operate on the selected project. Native command execution requires
explicit per-task consent, remembered on resume. Native code runs as the local
user: THIS IS NOT A SANDBOX. No paid providers, automatic deployment or browser
agent are added. StateTree checkpoints are not remote Git pushes.

Windows >= Python3.11 remains supported but must be reported as untested unless
actually run. Tests must exercise real harmless file/command effects, HTTP flows,
restart/replay, duplicate requests, uncertainty, path guards and compact context.
Scripted model test responses are not evidence of Qwen quality or speed.
