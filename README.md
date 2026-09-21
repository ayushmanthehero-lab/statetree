# StateTree

> **Agent tasks update:** [coding tools, automatic checkpoints and compact-note resume](docs/AGENTIC_TASKS.md). Use the new **Agent tasks** tab; legacy **Local chat** stays archive-only. Native code requires explicit task consent and is **not sandboxed**. [Verification report](docs/AGENTIC_VALIDATION.md).


> **Windows GTX 1650 / local Qwen3.5-4B:** [setup and combined GPU launcher](docs/GTX1650_SETUP.md). Downloads pinned CUDA binaries and Q4_K_M weights, checks actual GPU offload, and starts the local workbench. This does not redeploy the AWS website.

## 0.3.0 — local project workbench

The uploaded project now includes an integrated **Project API, JSON CLI and
local dashboard** over its existing checkpoint, memory, recovery, usage and
branch components. Existing native Strands and hybrid AWS workflows are retained.

```powershell
# Run from the extracted source folder. Python 3.11+ and Git are required.
python -m pip install -e '.[test]'
python -m statetree init --git --goal "Build and maintain StateTree"
python -m statetree serve
```

Open the loopback URL printed in the terminal and enter its local access token.
This starts the workbench, not a model or an AWS deployment. Configure your local
model server separately before using chat. On a restart, run `serve` again rather
than reinitializing the project. Use a virtual environment as described in the
startup guide.

A source-only, zero-model-call demonstration:

```powershell
python -m statetree demo --directory '..\statetree-local-demo'
```

Use a new disposable directory outside the source tree. The demo executes real
local recovery, checkpoints, speculation, verification, adoption and portable
state transfer. Its context-size figures are **UTF-8 byte estimates**, not measured
provider tokens or resource savings.

**Start here:** [Startup and usage guide](docs/PROJECT_WORKBENCH.md) ·
[All ten README features mapped to code](docs/FEATURE_MATRIX.md) ·
[Build changes and exact verification limits](docs/COMPLETION_REPORT.md).

**Validation boundary:** local-core, HTTP and browser-form checks were run, and a
wheel was built. The full suite is not green in the build environment because the
real Strands SDK could not be installed there. Live model, optional framework,
Windows, Docker and AWS paths require their own validation. No public deployment
or new live token-saving result is claimed.

The sections below preserve the supplied project vision, implementation guides
and historical benchmark reports; their historical runs are not new results from
this release.

---

<!-- BEGIN STATETREE WORLD-IMPACT VISION -->
## The future of AI should not require carrying the entire past.

**StateTree — building project memory for a less wasteful AI future.**

> **Our mission: help AI preserve progress without making unnecessary context the price of continuity.**

### The problem: useful intelligence, unnecessary repetition

An AI agent should spend its resources solving the next problem—not repeatedly receiving the entire conversation that led to the last one.

Yet in workflows that replay their growing history, a simple question can arrive wrapped in old messages, tool results, failed attempts and unrelated decisions. The information needed for the next step may be small; the context accompanying it may not be. [3]

**This is the problem StateTree targets: token consumption that grows because history grows, rather than because the next task needs more information.**

Imagine that same design choice repeated across millions of agent interactions. What looks like a few unnecessary paragraphs in one request becomes a systems-level efficiency question:

> **How do we scale useful AI without scaling avoidable work?**

### The stakes go beyond a token counter

**Affordability.** Unnecessary input can put pressure on token-metered budgets. Our goal is to make more of that budget serve useful work, especially for students, independent developers and small teams. Actual savings depend on pricing, caching and the complete workflow. [4]

**Compute capacity.** Long contexts can increase inference-memory requirements and constrain serving throughput. The ambition is to make limited hardware go further by improving what an agent carries into its next request—not by assuming that every problem requires more hardware. [6]

**Environmental responsibility.** AI serving has energy, emissions and cooling-water impacts that must be evaluated across the serving system. StateTree's environmental ambition is to investigate one upstream opportunity: avoiding unnecessary inference work rather than accepting it as inevitable. [9]

**Reliable progress.** More history does not automatically mean better recall. Important decisions can be difficult to retrieve from long inputs, while removing history without preserving evidence risks losing what matters. We should not have to choose between carrying everything and forgetting the project. [7], [3]

**The challenge is not simply to make AI remember more. It is to help AI carry less without losing what matters.**

### StateTree: preserve progress, not an endlessly growing prompt

StateTree approaches project continuity as something to record and manage, rather than something to reconstruct from an ever-longer transcript.

Its documented runtime combines versioned checkpoints, bounded context, recorded facts, retained evidence and request-level usage accounting. Relevant commit notes can be recalled into a new request, while underlying evidence remains available outside the active prompt. The implementation and its operational boundaries are documented below.

The principle is simple:

> **Keep the decision. Keep the evidence. Bring forward the context the next task needs.**

Not every token is waste. Reasoning, exploration and verification can be essential. StateTree targets unnecessary repetition—not the work required to get an answer right.

### A small demonstration of a larger ambition

In the supplied fixed Qwen comparison, the full-history path reported **950 input tokens**. The StateTree commit-context path reported **742**. Both returned the correct answer: **“7 days.”**

**208 fewer reported input tokens. A 21.89% reduction on this one example. The same correct answer.**

The broader ambition is to make **fewer tokens per successful task** a practical foundation for more accessible, accountable and resource-efficient AI.

**Evidence boundary:** This was one seeded fact-retrieval comparison using a caller-authored note. Counts include cached input; cache reuse differed, and the StateTree path was slower in this run. The result establishes reduced reported input-token volume in that example—not measured savings in money, electricity, carbon or water. The complete report, research and evaluation boundaries remain below.

### The vision

We want an AI future where capability is not reserved for those who can afford to keep expanding context and infrastructure. Where project progress survives beyond a conversation. Where efficiency means preserving useful intelligence, not simply cutting words.

**StateTree is a step toward that future: helping AI spend less of its context carrying the past, and more of it solving what comes next.**

**Remember the decision. Carry less history. Build more with less.**
<!-- END STATETREE WORLD-IMPACT VISION -->

---

<!-- BEGIN STATETREE FEATURES -->
## Features

### Git-like control over an agent's progress, memory and execution.

**Pause → checkpoint → branch → experiment → verify → merge or rollback → resume → migrate.**

StateTree brings the project-state ideas in the original proposal into a single feature story: preserve useful progress, keep experiments separate, carry relevant memory forward, and account for the tokens consumed along the way.

**Historical scope:** The feature narrative below came from the supplied documentation. The 0.3.0 implementation and new local test results are tracked separately in `docs/COMPLETION_REPORT.md`. The live Qwen comparison demonstrates one fixed commit-recall example. Distributed cloud workers and unrestricted migration of private model sessions remain roadmap items. Specific boundaries appear alongside the relevant features.

### 1. Versioned Agent Memory, Checkpoints & Rollback

**Give an agent a recoverable project history—not just a growing conversation.**

StateTree records checkpoint notes together with saved agent state and a linked Git workspace snapshot. The application can preserve its goal, constraints, active subgoal, recorded facts and supporting evidence, so later work has an explicit state to restore rather than only a transcript to reinterpret.

Checkpoints separate recoverable progress from the explicitly verified main state. An experiment can be saved without automatically becoming the project's accepted result. Restoring a checkpoint brings back its captured state and workspace while retaining the last explicitly promoted canonical history.

**Example:** Save a checkpoint after implementing an API. If a later authentication change breaks the project, restore the earlier captured state and try another approach.

Checkpoints are taken at a quiet boundary after an agent call ends; this is not a live pause of an in-flight model request. Restoring files and agent state does not undo external database writes, API operations or running processes. See the existing **Checkpoints and branches** section in the full README.

### 2. Forkable Agents, Speculative Execution & Verified Merging

**Explore alternatives without mixing every failed attempt into the main project state.**

StateTree's documented `BranchManager` and `SpeculativeCoordinator` create isolated Git worktrees for alternative strategies. Workers can start from a shared revision, produce separate changes, run application-defined checks and submit their results for integration. The coordinator checks declared dependencies and conservatively merges compatible, disjoint state and file changes.

**Example:** Explore Redis, an in-memory cache and a database-backed cache in separate branches. Compare the candidates using the project's tests and chosen evaluation criteria, then promote the accepted result. This is an illustrative workflow, not a reported three-branch benchmark.

Failed alternatives need not become active canonical context, but their observed token usage still belongs in the ledger. Speculation is a way to explore choices, not an automatic token-saving mechanism. Verification depends on the supplied checks and evidence; a caller's `verified=True` assertion is not an independent guarantee that every fact is correct. The lightweight `runtime.fork()` reference is not a substitute for isolated concurrent worktrees.

### 3. Crash Recovery & Durable Agent Execution

**Continue from saved progress instead of treating every interruption as a new task.**

StateTree's recovery design combines checkpoints, saved application state, retained file evidence and tool receipts. Those records help identify what completed, what remains pending and which tool actions need reconciliation before work continues. The supplied documentation describes a local recovery example that restores a checkpoint in a fresh Python process.

**Example:** A worker stops after API and authentication work has been saved. Restart the worker with the same project and state directory, then resume from that saved progress rather than reconstructing the project solely from old conversation messages.

A note alone is not a resumable session. Commands with uncertain outcomes are not automatically repeated, and a process crash during multi-file restoration may require explicitly restoring the intended checkpoint again. Public task-chat validation is still pending in the supplied documentation. The fixed cloud comparison itself does not resume after a worker restart.

### 4. Model Handoffs & Checkpoint-Based Hot-Swapping

**Let the project continue when the model changes.**

The documented local runtime supports model handoffs using portable public state. The receiving model can be given the goal, recorded decisions, active work and evidence needed for continuation rather than relying exclusively on the original model's conversation history.

**Illustrative workflow:** A smaller Qwen model handles routine steps, an application hands a saved checkpoint to a larger model for a difficult decision, and a later handoff returns routine work to the smaller model. This is the proposed usage pattern—not a measured routing or cost-saving result.

Here, “hot-swapping” means a handoff at a supported execution boundary. It does not mean transferring model weights, hidden reasoning, a KV cache or an arbitrary provider-private session. Automatic difficulty-based model routing is not established by the supplied README. Local public-state handoffs are documented; unrestricted session migration is future work.

### 5. Portable Agent State & Framework Interoperability

**Make project continuity depend less on one conversation or framework.**

The proposal describes an **Agent State Protocol (ASP)**: a structured representation of goals, subgoals, verified facts, pending actions, tool state, resources, artifacts, memory and checkpoints. The implementation documentation describes portable public-state transfers, a Strands integration and optional LangGraph and CrewAI adapter tests.

**Example:** Export the supported state from one agent process, then import it into another compatible process so the next worker receives explicit project context rather than having to infer everything from a transcript.

ASP is the proposal's name for an interchange format, not a claimed industry standard. Adapters must preserve the meaning of supported state and tools. The documentation does not establish universal compatibility with every framework or the ability to reconstruct arbitrary private framework sessions. See the [local feature guide][st-features-local].

### 6. Memory Garbage Collection & Bounded Active Context

**Keep the working context focused without throwing away the project's evidence.**

StateTree separates active context from archived history. Its deterministic compaction policy retains the newest user request, goal, supplied state and recent complete exchanges. Older tool outputs can be replaced with archive references, and older complete exchanges can leave the prompt when needed to fit the configured budget.

The documented `FactMemory` handles structured observations and dependency invalidation. The original proposal describes the lifecycle as **keep useful facts, update changed facts, archive superseded information and remove unsupported hypotheses from active use**. Raw messages and pre-compaction histories remain available through content-addressed archives and paginated retrieval.

**Example:** Keep the current database choice active, update a changed API port, and move an abandoned database proposal out of the working context without requiring its full discussion in every request.

“Garbage collection” here means managing active memory, not automatically deleting stored evidence: automatic archive pruning is not implemented in the supplied documentation. Commit recall uses keyword matching, not semantic search, and there is no extra summarization-model call. Applications remain responsible for maintaining accurate facts and evidence.

### 7. Concurrency Control & Conflict-Aware Collaboration

**Prevent one worker from quietly replacing another worker's newer state.**

StateTree keeps separate branch heads, rejects stale writers and checks declared dependencies before integrating concurrent work. Isolated worktrees separate the file changes made by different workers; conservative merging allows compatible changes to be combined without treating every overlapping modification as safe.

**Example:** Agent A advances a schema revision while Agent B is still working from the previous revision. A stale or incompatible commit should require explicit refresh and reconciliation instead of silently becoming accepted state. The original proposal calls this a Git-like rebase; the documented capability is stale-write rejection and conflict checking, not an automatic semantic rebase engine.

Use one active runtime writer per workspace and isolated worktrees for concurrent workers. These controls do not establish distributed transactions across external databases, APIs or cloud services.

### 8. Token Consumption Reduction

**Remember the decision. Carry less history.**

StateTree targets unnecessary input-token consumption by changing what enters the next model request. Instead of automatically replaying the entire conversation, it can supply a relevant checkpoint note and selected active facts within a bounded context, while keeping supporting material outside the prompt until needed.

Its documented mechanisms work together: **keyword-based commit recall** selects relevant recorded notes; **deterministic compaction** limits older exchanges; **tool-output archiving** replaces large historical results with references; and **on-demand retrieval** makes archived evidence accessible when a task actually needs it. These mechanisms reduce the need to keep every old message active; they do not change the model's tokenizer or guarantee a shorter complete workflow.

**The live example:** The question asks for the agreed export retention period. The baseline receives the seeded conversation. The StateTree path receives the selected caller-authored note, the question and StateTree overhead.

| Supplied run measurement | Full conversation | StateTree commit context |
| --- | ---: | ---: |
| Reported input tokens, including cached input | 950 | 742 |
| Output tokens | 3 | 3 |
| Answer | 7 days | 7 days |
| Fixed answer check | Passed | Passed |

**Measured in that fixed example: 208 fewer reported input tokens, a 21.89% reduction, with both answers correct.** The run is `f9b555c3369945e5a954f6bdd2a8b0c0`; the complete report is preserved in the full README.

The note was caller-authored and required no summarization-model call. Cache reuse differed, and StateTree was slower in this run. This result is not a general accuracy guarantee or a measurement of lower bills, latency, GPU memory, electricity, carbon or water use. The separate preserved 22.42% figure belongs to another run.

**The long-term objective is fewer tokens per successful task—not fewer tokens at the expense of correct work.** A complete evaluation must include input and output tokens, unsuccessful branches, retries, verification, retrieval and any memory-maintenance model calls. Archiving and recalling information can introduce extra work, so task-level savings must be measured rather than assumed.

### 9. Persistent Token Accounting & Budget Controls

**Make efficiency visible, including the work that did not succeed.**

StateTree records observed model usage in a persistent ledger that is independent of checkpoint rollback. Discarding a failed branch does not erase the tokens it consumed. Each observed request has an identity to prevent duplicate delivery from inflating totals; usage can be inspected by run, branch or phase.

The ledger retains raw provider counts and distinguishes known usage from unknown or ambiguous usage. Cache inclusion is configurable rather than assumed. Optional run-level thresholds stop subsequent calls after the recorded usage reaches the limit, while context budgets provide a preflight check on individual requests.

**Example:** Compare two strategies using the recorded tokens spent across all their attempts—not only the final successful answer.

A between-call threshold is not a hard billing cap: a single request can cross it. The default context counter is a serialized-byte estimate, not an actual provider tokenizer. Calls outside the normal agent loop may require explicit ledger recording, and missing provider usage cannot be treated as zero.

### 10. Open-Model Integration & Hybrid AWS Deployment

**Keep the memory layer useful when inference runs on your own machine.**

The supplied deployment uses Qwen on the operator's PC, while AWS hosts the application and gateway. The documented path is **browser → ECS running StateTree + Strands → SageMaker Serverless gateway → local Qwen through an HTTPS tunnel**. Completed comparison reports are stored privately in S3.

This deployment demonstrates that application hosting and model inference can be separated. The project also documents local operation and an adapter for self-hosted model inference; the shown Qwen path does not require an OpenAI- or Claude-hosted model API.

The PC, model process and tunnel must remain online for new model calls. Private S3 reports are not resumable agent checkpoints. The original proposal's SQS-coordinated distributed workers and multi-worker cloud execution are a roadmap architecture, not capabilities demonstrated by the live hybrid comparison. See the [hybrid deployment guide][st-features-deployment].

### The bigger picture

**Preserve progress. Isolate experiments. Carry relevant memory. Account for every observed request.**

Together, these features frame StateTree as Git-like project memory and execution-state infrastructure for long-running agents—not another model and not a promise that all agent workloads become cheaper. The ambition is more accessible, reliable and resource-conscious AI; the current token demonstration is one measured step toward that goal.

**Feature-documentation pointers:** [Local runtime capabilities][st-features-local] · [Commit-context behavior][st-features-commit-context] · [Verification record][st-features-verification] · [Hybrid deployment][st-features-deployment].

[st-features-local]: https://github.com/ayushmanthehero-lab/statetree/blob/main/docs/LOCAL_FEATURES.md
[st-features-commit-context]: https://github.com/ayushmanthehero-lab/statetree/blob/main/docs/COMMIT_CONTEXT.md
[st-features-verification]: https://github.com/ayushmanthehero-lab/statetree/blob/main/docs/VERIFICATION.md
[st-features-deployment]: https://github.com/ayushmanthehero-lab/statetree/blob/main/docs/HYBRID_DEPLOYMENT.md
<!-- END STATETREE FEATURES -->

---

<!-- BEGIN STATETREE RESEARCHED PROBLEM STATEMENT -->
## Why StateTree exists

**Remember the decision. Carry less history.**

> **Problem statement:** Long-running AI agents need continuity, but carrying an ever-growing conversation into each model request mixes useful project decisions with old messages, logs and intermediate results. StateTree targets this unnecessary context replay: preserve project evidence outside the active prompt, recall relevant recorded decisions, and measure the input tokens needed to complete the task correctly.

The objective is **not to minimize tokens at any cost**. It is to reduce unnecessary context while preserving the information needed for correct work. More reasoning, verification or exploration can be valuable; irrelevant repetition is the target.

### 1. Token consumption grows across repeated agent calls

In a history-replaying workflow, earlier messages become input again on later calls. Anthropic reported that agents used roughly **4 times** as many tokens as chats, and multi-agent systems roughly **15 times**, in its observations. These are workload-specific figures, not universal multipliers or StateTree savings. [1]

**Illustrative arithmetic, not a benchmark:** If 20 successive requests contain 1,000, 2,000, …, 20,000 input tokens, their cumulative input is **210,000 tokens**, although the final request contains only 20,000. This example counts submitted context; it does not calculate fresh computation, bills or energy, and it excludes output tokens.

**StateTree's focus:** Carry the relevant recorded decision rather than automatically replaying every discussion that preceded it.

### 2. Tool output can overwhelm the actual task

Tool definitions, retrieved documents and intermediate results also occupy context. Anthropic's MCP engineering article identifies tool-definition overload and repeated intermediate results as sources of excessive token consumption. [2]

**StateTree's focus:** The documented runtime archives older tool results, retains evidence references and supports paginated retrieval, instead of requiring every raw result to remain in the active prompt. This is not a claim that StateTree implements Anthropic's separate MCP code-execution approach.

### 3. Token volume creates cost and throughput pressure

Token-metered APIs distinguish input, output and cached usage; a shorter prompt does not automatically produce a proportional bill reduction. Token-based rate limits can also constrain concurrent workloads, with cache treatment varying by provider and model. [4], [5]

**StateTree's focus:** Record request-level usage, distinguish known and ambiguous counts, and expose the accounting needed for later cost and throughput evaluations. The current Qwen demo runs inference on the operator's PC; its token counts are not a per-token AWS invoice.

### 4. Long prompts put pressure on inference resources

NVIDIA describes input processing, or prefill, and the key-value cache used by attention-based inference. In the architectures it discusses, longer sequences increase KV-cache requirements and can constrain serving throughput. Actual effects depend on model architecture and serving configuration. [6]

**StateTree's focus:** Reduce unnecessary prompt content. GPU-memory reduction, lower latency and higher concurrency remain measurements to make—not benefits established by the fixed demo.

### 5. More history does not guarantee better recall

*Lost in the Middle* found that the models tested could perform worse when relevant information appeared in the middle of long contexts. Separately, Anthropic describes context curation and structured notes as approaches to maintaining useful information over long-running tasks. [7], [3]

**StateTree's focus:** Bring relevant recorded facts into the next request while retaining their underlying evidence. Retrieval can still miss information, and a caller-authored note can be wrong or outdated. The live demo checks one known fact; it does not demonstrate a general accuracy improvement.

### 6. Lost execution state can cause repeated work

A new session needs more than a transcript to know what work has completed. LangGraph's persistence documentation distinguishes checkpoints for execution continuity from stores for durable facts, and explains their role in recovery after interruptions. [8]

**StateTree's focus:** The broader runtime documented below combines checkpoint notes with saved state, tool receipts and file evidence. A note alone is not a resumable agent session. The fixed comparison demonstrates recall, not interruption recovery; public task-chat validation remains pending in the supplied project documentation.

### 7. Avoidable inference is also a resource-efficiency concern

Google's production measurement study treats AI-serving impact as a full-stack problem involving accelerators, host systems, idle capacity and data-center overhead, with separate energy, emissions and cooling-water measurements. [9]

**StateTree's motivation:** Investigate whether avoiding redundant inference can improve resource efficiency. **This project has not measured electricity, carbon or water savings.** An input-token reduction cannot be converted into an equal percentage reduction in these resources.

## What the live hybrid demo demonstrates

**Project memory, one commit at a time.**

The fixed question is: **“What is the agreed export retention period?”**

The baseline receives the question and 20 seeded conversation messages. The StateTree path receives the question, the selected commit note and StateTree overhead. The caller-authored note states:

> Export retention is 7 days. Completed export files are deleted after 7 days.

Both paths use `Qwen/Qwen3.5-4B`, temperature `0`, thinking disabled and a maximum of 64 output tokens. No summarization-model call creates the note in this comparison.

### Supplied completed run — 20 September 2026

Run ID: `f9b555c3369945e5a954f6bdd2a8b0c0`
Fixture: `export-retention-v2`

The values below reproduce the supplied report; they are not a new benchmark execution.

| Reported measurement | Conversation history | StateTree commit context |
| --- | ---: | ---: |
| Input tokens, including cached input | 950 | 742 |
| Cache-read input tokens, included above | 946 | 221 |
| Input not reported as cache-read, calculated by subtraction | 4 | 521 |
| Output tokens | 3 | 3 |
| Total input + output tokens | 953 | 745 |
| Elapsed time reported for the path | 7.133 s | 15.569 s |
| Model answer | 7 days | 7 days |
| Fixed answer check | Passed | Passed |

**Result: 208 fewer reported input tokens, or 21.89%, with the same correct answer on this fixed example.**

Calculation: `(950 - 742) / 950 × 100 = 21.89%` after rounding.

The StateTree path was **slower in this run** and had fewer cache-read tokens. The unequal cache reuse prevents interpreting the input-token percentage as a fresh-computation comparison; these measurements do not establish the cause of the timing difference. Prompt caching and context selection address different things: reuse of computation versus selection of information. Both should be considered when evaluating efficiency. [4]

This is one seeded fact-retrieval example, not a general savings benchmark. It does not measure autonomous note creation, arbitrary project reasoning, billing, environmental impact or recovery of an interrupted agent session. Both the history and note are authored fixtures.

The preserved documentation below also cites a **different completed report**, `ba55ac371d1f4e0bb92d7f8a92fd47c5`, with 950 versus 737 input tokens and a 22.42% reduction. Those figures refer to that separate report, not the run tabulated here.

### Deployment boundary

**Browser → ECS (StateTree + Strands) → SageMaker Serverless gateway → operator PC running Qwen through an HTTPS tunnel.**

AWS hosts the application and gateway; the operator's PC performs model inference. The PC, model process and tunnel must stay online for new calls. Completed cloud reports are stored privately in S3. A saved comparison report is not a resumable agent session, and an interrupted comparison does not resume after a worker restart.

## How to judge progress beyond this example

The practical target is **fewer tokens per successful task**, not merely a shorter prompt. Future matched evaluations should include varied tasks, multiple runs, relevant-fact recall, task success, total input/output usage across all attempts, cache-read usage, memory-maintenance calls, errors and retries. Compare cold-cache and warm-cache conditions separately and measure end-to-end latency.

Cost claims need actual pricing and infrastructure accounting. Resource-efficiency claims need energy measurements with a stated system boundary. If a caller or model must generate and maintain memory, that work belongs in the evaluation too.

## Research supporting the problem statement

These sources motivate the problem and evaluation approach. Their results are **not StateTree benchmark results**. Research checked on 20 September 2026.

[1]: https://www.anthropic.com/engineering/multi-agent-research-system
[2]: https://www.anthropic.com/engineering/code-execution-with-mcp
[3]: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
[4]: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
[5]: https://platform.claude.com/docs/en/api/rate-limits
[6]: https://developer.nvidia.com/blog/mastering-llm-techniques-inference-optimization/
[7]: https://aclanthology.org/2024.tacl-1.9/
[8]: https://docs.langchain.com/oss/python/langgraph/persistence
[9]: https://arxiv.org/abs/2508.15734

| Source | Why it matters |
| --- | --- |
| [1 — Anthropic: How we built our multi-agent research system][1] · 13 June 2025 | Observed agent token amplification and the performance–resource trade-off. |
| [2 — Anthropic: Code execution with MCP][2] · 4 November 2025 | Context occupied by tool definitions and intermediate results. |
| [3 — Anthropic: Effective context engineering for AI agents][3] · 29 September 2025 | Context curation, compaction and structured notes. |
| [4 — Claude documentation: Prompt caching][4] | Why cached and uncached usage have different implications. |
| [5 — Claude documentation: Rate limits][5] | Input/output token limits and cache-aware accounting. |
| [6 — NVIDIA: Mastering LLM Techniques — Inference Optimization][6] | Prefill, decoding, KV caches and serving constraints. |
| [7 — Liu et al.: Lost in the Middle][7] · TACL 2024 | Long-context retrieval reliability in the tested models. |
| [8 — LangGraph documentation: Persistence][8] | Execution checkpoints versus durable cross-session facts. |
| [9 — Elsworth et al.: Measuring the environmental impact of delivering AI at Google Scale][9] · 21 August 2025 | Full-stack measurement of serving energy, emissions and water. |

<!-- END STATETREE RESEARCHED PROBLEM STATEMENT -->

---

## Implementation and operational documentation

StateTree adds versioned checkpoints, portable public state, durable tool steps, bounded memory, model handoffs, and verified worktree branches to local agents. It saves evidence outside the active prompt and records request-level token usage.

StateTree runs locally and can invoke self-hosted Qwen models through Amazon SageMaker AI. It includes deterministic compaction and structured fact lifecycle policies; it does not use an extra summarization model. The [hybrid deployment guide](https://github.com/ayushmanthehero-lab/statetree/blob/main/docs/HYBRID_DEPLOYMENT.md) provides the current path: an ECS website, SageMaker Serverless request forwarding, private S3 reports and authenticated Qwen3.5-4B inference on the operator's PC. The PC and its HTTPS tunnel must remain online for new model calls. The public AWS deployment passed a real comparison on 20 September 2026: both answers were correct, with 950 baseline input tokens versus 737 using commit context (22.42% fewer) on one seeded fixture. See the [saved demo report](https://st-0d33e7e243cf443cbf2a5a997dfc1906.ecs.ap-south-1.on.aws/#run=ba55ac371d1f4e0bb92d7f8a92fd47c5) and [verification record](https://github.com/ayushmanthehero-lab/statetree/blob/main/docs/VERIFICATION.md), including the initial timeout and cache counts; this is not a general savings, billing or speed benchmark.

The [Hyderabad CPU test guide](https://github.com/ayushmanthehero-lab/statetree/blob/main/docs/SAGEMAKER_CPU_DEPLOYMENT.md) remains available for full AWS inference on `ml.m6g.xlarge` once its instance quota is approved. The [GPU deployment guide](https://github.com/ayushmanthehero-lab/statetree/blob/main/docs/SAGEMAKER_DEPLOYMENT.md) covers later experiments; [the local feature guide](https://github.com/ayushmanthehero-lab/statetree/blob/main/docs/LOCAL_FEATURES.md) covers runtime APIs.

[Commit-context notes](https://github.com/ayushmanthehero-lab/statetree/blob/main/docs/COMMIT_CONTEXT.md) add opt-in keyword recall of checkpoint summaries and an explicit new-task boundary, with original evidence retained locally.

## Local project task chat

The new website interface accepts project tasks and shows actual worker activity, tool results, diffs, checkpoints and reported usage. An outbound worker on your PC registers a project selected locally; the website cannot choose an arbitrary computer path. The original comparison remains at `/benchmark`, and existing `#run=<id>` links redirect to it.

**Public task-chat validation is pending.** The interface and API have local checks, including desktop/mobile browser inspection. The historical cloud comparison above verifies the benchmark path, not the new project-agent workflow. See [the deployment guide](https://github.com/ayushmanthehero-lab/statetree/blob/main/docs/HYBRID_DEPLOYMENT.md#7-open-task-chat-and-connect-the-local-worker) for startup and the current validation boundary.

After the updated website is deployed, keep Qwen and its model tunnel running, then open another PowerShell window for the project worker:

```
Set-Location 'D:\aws hackathon\statetree'
.\.venv\Scripts\python.exe -B -m deploy.local_agent prepare
.\.venv\Scripts\python.exe -B -m deploy.local_agent serve --url 'https://st-0d33e7e243cf443cbf2a5a997dfc1906.ecs.ap-south-1.on.aws' --project 'D:\aws hackathon\statetree'
```

Sign in with the private **owner key**, not the benchmark demo code. The worker reads its separate credential locally; neither credential belongs in a URL or a task prompt. The owner key authorizes control of the registered project, so keep it private. Docker Desktop and the locally selected command image are needed for command/test tools. Keep each task within **2,000 UTF-8 bytes**; use follow-ups for additional work.

Tasks edit an isolated task workspace. Review the result and diff, then choose **Apply changes to project** to update the original files after conflict checks. The same normalized prompt attaches to existing work or resumes an interrupted task; **Run fresh** explicitly creates another run. A follow-up after a completed or applied task continues with its files and saved context. Restart a killed worker process before requesting resume, using the same project and state directory. A browser request cannot restart a stopped Windows process.

Recovery uses checkpoint notes **plus saved state, tool receipts and retained file evidence**. A commit message alone is insufficient, and resumed model requests still consume tokens. Commands with an uncertain outcome are not automatically repeated. Unknown or ambiguous usage remains unknown; known subtotals are available in the usage record. Keep the worker, Qwen, tunnel and PC online while work is running.

## Run locally

Python 3.11+ and Git are required. The native integration pins `strands-agents==1.56.0`. Install it in your own environment; the delivered ZIP does not contain a `.venv`. The Windows paths below are historical examples.

```
# From D:\aws hackathon\statetree
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -B -m examples.checkpoint_restore
.\.venv\Scripts\python.exe -B -m examples.local_features
.\.venv\Scripts\python.exe -B -m examples.commit_context
.\.venv\Scripts\python.exe -B -m benchmarks.replay --steps 30
```

The checkpoint and feature demos and replay benchmark make **no model calls**. The commit-context demo uses a scripted local provider; model-loop tests also simulate usage. None of these require remote inference. The recovery demo uses a temporary repository and launches a fresh Python process to restore its checkpoint. The replay report is written to `benchmark-results/replay.json`.

Optional real-framework adapter tests use LangGraph 1.2.11 and CrewAI 1.15.22. The historical validation used a separate environment; it is not included in this source archive:

```
.\.venv\framework-tests\Scripts\python.exe -X utf8 -B -m unittest tests.test_optional_adapters -v
```

New installations can install the `frameworks` optional dependency group. Tests disable SDK telemetry and use temporary storage; no model/API credentials are needed.

For a new environment, create a virtual environment and run `python -m pip install -e .`. Tests use standard-library `unittest`; no test dependency is needed.

## Attach an agent

```
from strands import Agent
from statetree.models.sagemaker import SageMakerModel
from statetree.runtime.runtime import StateTreeRuntime

agent = Agent(
    model=SageMakerModel(
        endpoint_name="statetree-qwen35-cpu", region_name="ap-south-2",
        model_id="Qwen/Qwen3.5-4B", max_tokens=64, enable_thinking=False,
        context_window_limit=4096,
    ),
    system_prompt="Use evidence and preserve the project's requirements.",
    tools=[],  # Supply your application tools here.
    context_manager=False,  # Let StateTree own this policy for the comparison.
)
runtime = StateTreeRuntime(
    agent,
    goal="Repair the application and preserve API compatibility",
    repo_path="/path/to/your/git/repository",  # Repository root with an initial commit.
    max_input_tokens=3000,
    recent_turns=2,
    run_id="repair-001",  # Reuse this ID to continue cumulative accounting after restart.
    commit_context_budget=1600,
    input_cache_convention="included",  # llama.cpp prompt_tokens includes cached prompt tokens.
)
agent.state.set("statetree_context", {
    "constraints": ["Preserve API compatibility"],
    "active_subgoal": "Diagnose the failing tests",
    "facts": {},
})

# This line invokes the selected model and uses its configured credentials.
result = runtime.run("Inspect the application and identify the failing behavior.")
checkpoint = runtime.commit(current_subgoal="diagnosis", message="Recorded diagnosis and supporting evidence")
print(checkpoint.id)
print(runtime.usage.summary(run_id="repair-001"))
```

`max_input_tokens` is enforced against a configurable **preflight estimate**, not a provider billing counter. The dependency-free default counts serialized UTF-8 bytes, including tool schemas and the system prompt. It is conservative for ordinary text but does not model provider framing or multimedia. Supply `counter(serialized_request) -> int` for your chosen tokenizer and leave room for output and provider overhead. Configure the model's own output limit separately.

`recent_turns` means complete conversational/tool exchanges. StateTree pins the newest user request, goal, supplied state and recent exchanges. Older tool results are archived and replaced with references; older complete exchanges can be dropped to fit. Oversized required content raises `ContextBudgetError` before a model request. StateTree never invents a fact or silently summarizes one. Your application maintains `statetree_context`, including evidence references and superseded facts.

The runtime registers `statetree_read_archive` for paginated retrieval. Raw messages and pre-compaction histories are stored as content-addressed JSON in `.statetree/archive/`. Calling `store.read_archive(id)` also works outside the agent. `FactMemory` manages structured observations and dependency invalidation; `runtime.set_memory(...)` injects selected active facts and checkpoints their history. Commit recall uses local keyword matching; there is no automatic semantic search or LLM memory agent.

## Checkpoints and branches

```
# Always take checkpoints at a quiescent boundary, after an agent call ends.
runtime.restore(checkpoint.id)
runtime.fork("experiment", checkpoint.id)

# To create canonical verified state, run your own checks first and attach evidence.
verified = runtime.commit(
    verified=True,
    verification={"passed": True, "checks": ["test command, result and artifact reference"]},
)
print(runtime.store.get_head())            # Latest main progress, possibly unverified.
print(runtime.store.get_canonical_head())  # Latest verified main checkpoint.
```

The evidence is a caller assertion, not an independent verifier or proof that every fact is true. A Git snapshot is bound into the same hashed manifest as the agent snapshot. StateTree stores separate branch heads and rejects stale writers. Unverified progress remains recoverable. Only verified `main` commits advance canonical state. Restoring rewinds the working branch, while canonical history remains at its last explicit verified promotion.

Snapshots include tracked files, nonignored untracked files and a separate index tree. Git objects are pinned under `refs/statetree/workspaces/`. The adapter excludes `.statetree`, `.venv`, `__pycache__` and an explicit in-repository store path. It restores original staging separately from working-file content. Later unrelated untracked files are preserved; conflicting untracked paths cause a preflight error. Git branch and HEAD remain unchanged.

`runtime.fork()` remains a lightweight legacy checkpoint reference. For isolated concurrent workers, use `BranchManager` and `SpeculativeCoordinator`: they create separate Git worktrees, check declared dependencies, verify candidates and conservatively merge disjoint state/file changes. Their canonical portable revision is separate from the legacy checkpoint reference; the selected integration workspace is returned explicitly. Use one active runtime writer per workspace. Symlinks, junction collisions, submodules and Git assume-unchanged/skip-worktree flags are rejected. A checkpoint does not undo database operations, external APIs or running processes.

Immutable objects are published before the SQLite branch transaction commits. An interrupted publication leaves orphaned objects, not a head pointing at missing content. Restore validates manifests, blobs and Git objects first and attempts rollback on an ordinary exception. A machine/process crash during the multi-file restore is not an atomic filesystem transaction: reopen the runtime and explicitly restore the intended checkpoint again. Do not run the agent against a partly restored workspace.

The original 12-character prototype IDs are incompatible with this schema; automatic migration is intentionally unavailable. Keep legacy stores separately. There is no automatic archive pruning yet.

## Usage and evaluation

Usage persists independently of checkpoint rollback in `.statetree/usage.sqlite3`. Every observed model request gets a distinct ID; duplicate delivery of that ID cannot inflate totals. Retries, errors and discarded branches remain in the ledger. Filter by run, branch or phase. Summarization and verifier calls outside the normal agent loop must use `UsageLedger.record` themselves.

Raw provider counts are retained. Normalized totals include full prompt input plus output. Providers disagree on cache inclusion: use `input_cache_convention="included"` or `"excluded"` only after checking the provider. The conservative `"auto"` mode flags ambiguous cached usage rather than guessing. When `unknown_usage_requests` or `ambiguous_usage_requests` is nonzero, normalized totals are **incomplete subtotals**. Errors without reported usage are not counted as known zero.

An optional `total_token_limit` stops subsequent calls once the persisted run reaches that threshold, and stops if prior usage is incomplete. It is a **between-call stop threshold**, not a hard billing cap: one request can cross it. Normal model-loop calls are supported; direct `structured_output()`, direct model calls, provider-internal retries and interrupted requests without SDK usage may need separate accounting. A crash before usage is delivered cannot be fully reconciled without provider-side records. Context compaction supports stateless providers with JSON-serializable text/tool messages and plain-text system prompts.

The offline replay measures cumulative serialized-input estimates on a fixed trace. It does not measure actual model tokens, costs, latency or task success. Do not present its reduction percentage as end-to-end token savings.

An optional small live retrieval suite compares full history, Strands automatic management and StateTree using the same model, documents and task checks. StateTree additionally injects deterministically extracted document facts. The report records actual provider usage, success, errors and tokens per successful task, including failed attempts in the numerator. It is a smoke benchmark, not a coding benchmark or a statistical claim about general performance.

```
# Invokes an existing SageMaker endpoint using your AWS credentials.
# GPU endpoints incur hosting charges while provisioned, including between calls.
.\.venv\Scripts\python.exe -B -m benchmarks.live --live --endpoint-name statetree-qwen35-4b --model-id Qwen/Qwen3.5-4B --region us-west-2 --tasks 1 --steps 2
```

The live suite was not run as part of offline verification. The SageMaker adapter uses AWS-authenticated endpoint inference; it does not call Bedrock or an OpenAI-hosted model. It supports text and tool calls through the vLLM chat protocol, with non-thinking mode enabled by default. Direct structured output and multimodal input are outside this adapter's scope. The current buffered invocation must finish within SageMaker's 60-second processing window. Local public-state transfers, model handoffs, worktree coordination, and classified tool-effect reconciliation are implemented; their limits are in the [feature guide](https://github.com/ayushmanthehero-lab/statetree/blob/main/docs/LOCAL_FEATURES.md). Larger matched coding-task evaluations, calibrated tokenization, arbitrary private-session migration, distributed workers, and live provider evaluation remain future work.

See [the research guide](https://github.com/ayushmanthehero-lab/statetree/blob/main/docs/RESEARCH_AND_BUILD_GUIDE.md) for papers and evaluation rationale, [the original MVP plan](https://github.com/ayushmanthehero-lab/statetree/blob/main/docs/IMPLEMENTATION_PLAN.md), and [the local feature plan](https://github.com/ayushmanthehero-lab/statetree/blob/main/docs/LOCAL_FEATURES_PLAN.md).
