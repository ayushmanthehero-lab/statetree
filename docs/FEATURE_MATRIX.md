# README feature coverage — local integration release

This matrix maps the **ten numbered features in the supplied README** to actual
code and a usable entry point. “Local checks” means the executed component tests,
not a cloud deployment or a claim of model accuracy. Exact results and remaining
verification limits are in [COMPLETION_REPORT.md](COMPLETION_REPORT.md).

| README feature | Implementation and usable entry point | Verification boundary |
|---|---|---|
| 1. Versioned memory, checkpoints, rollback | `Project`, immutable snapshots/commit notes, Git snapshots, `checkpoint`, `history`, `restore`; timeline and evidence viewer | Local save/reopen/restore, preserved index, canonical head and usage tested. Not external-effect rollback. |
| 2. Forking, speculation, verified merging | Existing `BranchManager`/`SpeculativeCoordinator`; added persisted candidate reopening/CAS and `Project` integration; `branch fork/complete/merge/adopt`, dashboard, Python `speculate` | Actual worktrees, concurrent strategies, independent argv checks, conflict rejection and explicit adoption tested. Trusted cooperative workers, not security sandboxes. |
| 3. Crash recovery and durable execution | Existing durable receipts/checkpoint recovery; explicit public-session decoder; `Project.run_steps`; reopen through CLI/API | Fresh-process reopen, completed-effect replay prevention and checkpoint/progress publication-gap recovery tested. Not distributed exactly-once execution or automatic process restart. |
| 4. Public-state model handoffs | Existing native runtime handoff retained; `Project.switch_model`, CLI `model`, archive chain and checkpointed endpoint binding; real lazy Strands bridge for `ask` | Local binding/retained state tested without inference. Actual provider handoff and real-SDK bridge tests require dependencies and are not claimed as run here. No hidden reasoning/KV-cache transfer or automatic difficulty router. |
| 5. Portable state/framework interoperability | Existing ASP `AgentState`, bundles and Strands/LangGraph/CrewAI adapters; CLI and dashboard import/export | Public-state/hash validation, no partial live-state publication, evidence round trip and foreign branch-receipt handling tested. Optional real-framework and SDK tests remain unverified here. |
| 6. Memory lifecycle/bounded active context | Existing `FactMemory` and `ContextBuilder`; project remember/archive/invalidate/delete, compaction, retained archive chains, bounded archive-index projection | Dependency invalidation, bounded facts/context and retained evidence tested. Required oversized context fails closed rather than dropping essential state. No physical deletion of immutable historical evidence. |
| 7. Concurrency/conflict-aware collaboration | Persisted expected-parent checks, branch candidate CAS, conservative three-way state/file merge; dashboard stale-head protection | Actual concurrent local branch tests, stale writers and dirty-main adoption rejection tested. Not a distributed cloud-worker coordinator. |
| 8. Token consumption reduction | Bounded history, keyword commit recall, explicit new-task requests, paginated archive tool, context lab and local demo; existing measured benchmark retained | The new demo measures serialized **bytes** only and makes zero model calls. No new provider-token reduction, answer-quality, speed, cost, energy or water result is claimed. |
| 9. Persistent accounting/budget controls | Existing SQLite `UsageLedger` and actual SDK hooks; project/branch reporting, fixed run ID and between-call thresholds; CLI `usage` and dashboard | Ledger persistence/normalization, loser accounting and unknown-usage budget rejection tested with explicit fixtures. Live bridge/provider counts not validated here. Thresholds are not hard billing caps. |
| 10. Open-model/hybrid AWS integration | Original SageMaker adapter, model container, gateway, AWS deployment templates and private coding worker preserved; new operator-bound loopback chat integration | Selected deployment/rendering/HTTP tests executed. No AWS resources created, public site redeployed, Docker worker launched, or live Qwen endpoint tested in this build. |

## New integrated surfaces

`statetree/project.py` joins the existing components; `project_state.py` makes local
state operations explicitly independent of inference. `cli.py` and `__main__.py`
provide JSON commands. `web/workbench.py` and its three static assets provide the
new local interface. `verification.py` retains actual command-check evidence.
`project_inference.py` imports the **real** SDK on demand, with no fake substitute.

The original cloud task console remains distinct. The new local chat has only an
archive-reading tool and does not silently become a shell-capable coding worker.
Native runtime callers still control their own tools, verification policies,
resource declarations and model choices.
