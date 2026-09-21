# Agentic Resume Implementation Plan

**Goal:** Resumable coding tasks with automatic StateTree checkpoints.
**Architecture:** SQLite journal, local tool loop, independent command receipt worker, polling UI.
**Tech stack:** Python stdlib, existing Project and SafeFiles, llama.cpp REST, vanilla JS.
**Spec:** ../specs/2026-09-21-agentic-resume.md

## Global constraints
No new runtime dependency; no complete transcript in inference; no uncertain
command replay; explicit native execution trust; preserve existing state and GPU fixes.

## Review focus
Crash windows, duplicate submissions, rewound checkpoints, Windows child processes,
4096-token prompt fitting, authentication and UI locking.

## Tasks
- [x] 1. Write failing journal/file/command tests. Implement `statetree/agentic/store.py`,
  `tools.py`, `command_worker.py`. TaskStore submit/task/patch, operation intent/receipt,
  event pagination. Tools perform bounded file operations and approved argv commands.
  Reuse SafeFiles with lazy legacy worker import. Verify red then green.
- [x] 2. Write failing resume/context/checkpoint tests. Implement `engine.py`,
  `transport.py`. Persist pending tool batches before dispatch, checkpoint every
  receipt, compact summary plus newest evidence in requests. Test fault recovery.
- [x] 3. Write failing real HTTP tests. Implement `manager.py`, explicit authenticated
  task routes, Agent tasks UI with trust checkbox, event polling, pause/resume,
  retained IDs and manual uncertain-command reconciliation. Preserve archive chat.
- [x] 4. Run targeted and full suites, static checks and real process-death smoke.
  Document limitations. Provide full and update ZIPs with safe update helper,
  no state/model weights/venv. Verify archive content and preservation of hotfixes.

Execute inline; update progress and record observed outcomes in the delivery report.
