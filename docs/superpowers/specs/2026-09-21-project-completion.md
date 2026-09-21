# StateTree local project completion

## Intent and scope
Finish the uploaded project by making the README's ten numbered features accessible as one usable project workflow. Preserve the existing Strands runtime, safe coding worker, cloud deployment, public benchmark and honest measurement boundaries. No paid API, deployment, hidden-state migration, or general savings claim is introduced.

## Design
A `Project` facade orchestrates the existing checkpoint runtime, GitWorkspace, FactMemory, CommitMemory, portable bundles, durable workflows, BranchManager and usage ledger. An explicit public-state snapshot adapter permits local state management without importing an inference SDK; real model requests still use Strands and never return fabricated model answers. A configurable snapshot decoder is injected into the existing runtime, whose default remains the Strands decoder.

The CLI and loopback-only authenticated workbench expose checkpoints, restoration, facts, recall, context previews, public-state export/import, model bindings, branch candidates and accounting. Destructive restore/adoption requires explicit confirmation at the CLI/UI. Model configuration is stored in a checkpoint; credentials are read from the environment, never serialized. A model handoff is a public-state boundary, not private session transfer. Browser requests cannot choose arbitrary shell commands or inference URLs.

Verification runs only operator-configured argv commands, without a shell, in immutable temporary worktrees. Passing commands and untouched checked files are required for canonical promotion. Worktrees are not security sandboxes. Conservative branch canonical revisions remain separate from legacy runtime HEAD, matching the existing documented design; adoption is explicit.

## Success criteria
Actual Git/SQLite/filesystem integration tests cover reopening, rollback without rewinding usage, structured facts, note recall, corruption/stale writers, durable effects, branch recovery/conflicts, authenticated HTTP, CLI entry points, and honest offline context estimates. Provide a reproducible demo, feature/status matrix, test logs, updated source ZIP and patch. Report unavailable SDK/cloud/model tests distinctly rather than count them as passing.
