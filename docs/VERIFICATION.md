**StateTree verification — 20 September 2026**

**Hybrid deployment: live cloud comparison verified**

On 20 September 2026, the operator's Mumbai (`ap-south-1`) CloudFormation stack `statetree-hybrid` reached `CREATE_COMPLETE` with no recent failures. The [public website](https://st-0d33e7e243cf443cbf2a5a997dfc1906.ecs.ap-south-1.on.aws/) served `/ping`, HTML, JavaScript and CSS with HTTP 200. An unauthenticated run request returned HTTP 401; a request using the locally stored demo access code returned HTTP 202. No secret values were printed.

The successful [saved report](https://st-0d33e7e243cf443cbf2a5a997dfc1906.ecs.ap-south-1.on.aws/#run=ba55ac371d1f4e0bb92d7f8a92fd47c5) exercised the complete public API path: ECS backend, SageMaker Serverless gateway, runtime secret access, Cloudflare tunnel and native Qwen3.5-4B on the operator's PC. Both strategies returned `7 days`, passed the fixture check and reported known usage from one model call each. StateTree selected its actual checkpoint.

| Strategy | Input tokens (including cached) | Cache-read input tokens | Output tokens | Elapsed seconds |
| --- | ---: | ---: | ---: | ---: |
| Full seeded history | 950 | 946 | 3 | 1.595 |
| StateTree commit context | 737 | 221 | 3 | 17.977 |

This run used **213 fewer input tokens (22.42%)** with StateTree. The baseline had much greater prompt-cache reuse and was faster. These are measurements from one caller-authored 20-message fixture and note, not a general savings benchmark or evidence of reduced billing or latency. No summarization model generated the note.

The completed report was readable through the public API without the run access code. A direct S3 read from `statetree-hybrid-reportsbucket-ciic5cpflay1` matched the full returned report and confirmed AES256 server-side encryption. The local copy is `build/hybrid/cloud-report-ba55ac371d1f4e0bb92d7f8a92fd47c5.json` (ignored by Git).

The initial run `07649bc3011c4d9287012f10d52aa9f1` is also preserved: its baseline failed after 55.612 seconds with unknown usage; StateTree returned the correct answer after 30.576 seconds with 737 input and 3 output tokens. Its comparison correctly reports `task_failed` and does not claim savings. CloudWatch recorded one invocation 5xx, maximum model latency of 50.001445 seconds and maximum overhead of 5.459779 seconds in that minute. This supports an inference that the gateway's 50-second deadline was reached, with startup overhead added; the exact request error was not logged. The successful run above was the next retry.

Public API behavior and storage were checked directly; no browser rendering verification was available. New inference requires the PC and tunnel to remain online. Stored reports do not require local inference. Competition eligibility is not established by these technical checks. The earlier sections below retain the evidence and limitations at each historical stage.

**Hybrid ECR publication recovery: before hosting**

The first operator publication uploaded the gateway but timed out committing a web-image layer. Retrying the existing publisher succeeded using Docker's build cache and already-uploaded layers. Both images and the shared app secret are now published in account `446272719493`, Mumbai (`ap-south-1`), under timestamp tag `20260920110939`. No hosting was started by this operation.

Read-back verification found both deployment digests in ECR with Docker V2 manifest media types and confirmed that the secret exists and is not scheduled for deletion. The active tunnel's `/health` returned HTTP 200 with `{"status":"ok"}`. An offline CloudFormation plan and parameters were generated for that tunnel. The exact image digest URIs and secret ARN are saved in `build/hybrid/statetree-hybrid-ap-south-1.json`; no secret values were printed.

- Gateway deployment digest: `sha256:b38b6f40a5f8995be84ef8f70d9294e6543aed9b4659601b8841a9e19fcbeb02`
- Web deployment digest: `sha256:6e24b3621efcbbe2a098548d27e8e009522500e49474cec957901a84119a0331`

At this stage, the remaining operator step was `deploy.hybrid deploy --upstream-url <current URL> --apply`, followed by verification of CloudFormation completion and the live website. That deployment and public comparison have now been verified above. The earlier preparation-only checks below describe their historical boundary.

**Hybrid web deployment preparation: local integration and AWS read-only validation**

Added the protected native launcher, bounded SageMaker forwarding server, fixed web comparison, private S3 report storage, CloudFormation template and guarded publishing/deployment commands. Operator steps are in [HYBRID_DEPLOYMENT.md](HYBRID_DEPLOYMENT.md). No AWS hosting resources were created, no images were pushed and no public tunnel was opened during this preparation.

Two real native Qwen runs exercised the authenticated model → gateway → web API path. Both strategies answered `7 days` correctly and the StateTree strategy selected its actual checkpoint. Each strategy used one real model call with three output tokens; both histories and their note were caller-authored fixtures. Provider counts include cached input, although these four calls reported zero cache-read tokens.

| Fixture | Baseline input | StateTree input | Input reduction | Baseline / StateTree call time |
| --- | ---: | ---: | ---: | --- |
| v1: 12 seeded messages | 598 | 734 | **−22.74%** (136 more tokens) | 32.365 / 30.356 seconds |
| v2: 20 seeded messages, current demo | 950 | 739 | **22.21%** (211 fewer tokens) | 31.540 / 23.709 seconds |

The short example exposed the cost of memory framing and the archive tool schema. The second fixture adds four distinct project discussions while keeping the same fact, question, note and model settings. These results show an overhead threshold on two controlled examples, not general savings, a billing reduction, or a rigorous speed benchmark. Native startup took 62.19 and 70.33 seconds on these runs. All test model/gateway/web processes were stopped afterward. Local raw reports are `build/hybrid/integration-report-short-history.json` and `build/hybrid/integration-report.json` (ignored by Git).

| Deployment check | Evidence |
| --- | --- |
| Mumbai account preflight | 8 Fargate vCPUs, 0 used; 5 Serverless endpoints and 10 total concurrency; three suitable default-VPC public subnets found. Regional ECS Express CloudFormation type exists. |
| CloudFormation validation | AWS `ValidateTemplate` accepted the five-parameter template and reported `CAPABILITY_IAM`. This is template validation, not resource creation or runtime verification. |
| Gateway image | `statetree-hybrid-gateway:local-test`, `linux/amd64`, 229,800,184 bytes; container `/ping` and malformed invocation checks passed after the final response-cleanup rebuild. |
| Web image | `statetree-hybrid-web:local-test`, `linux/amd64`, 444,316,890 bytes; Linux Git/Strands/commit-context workflow with a fixed-response transport and authentication/static-route checks passed. |
| Installed wheel | A separate container ran from `/tmp`, with networking disabled, and confirmed the final 20-message fixture and all three packaged UI assets. |
| Docker recovery | Initial PyPI DNS failure did not recur after the running Docker builder was ready. Both complete builds succeeded without changing global Docker configuration. |
| Secret preparation | Existing generated keys were preserved and validated under the normal Windows account; only paths were printed. They are excluded from Git and Docker contexts. |
| Static verification | Fourteen new/changed Python files and `pyproject.toml` parsed. Whitespace checks passed. HTTP tests fetched the HTML/CSS/JS; a browser-rendering check was unavailable because the browser tool failed to initialize. |

The full test run discovered 295 tests in three subprocess groups. It had two optional framework dependency skips and one Windows socket race in the oversized-request test; the remaining 292 tests passed. That test was corrected to verify rejection from oversized `Content-Length` before sending the body, avoiding a race between unread bytes and connection closure. All 17 gateway/launcher tests passed on rerun. A subsequent regression exposed an upstream HTTP response left unclosed on rejected responses; explicit cleanup fixed it. The final **56-test hybrid suite passed in 19.962 seconds**, including that new regression, with no failures, skips or resource warnings. Existing core implementation files were not changed for this deployment work.

Independent review covered credentials, redirect handling, deadlines, report persistence, IAM scoping, collision/deletion guards and Docker authentication. Push authentication now uses a short-lived private Docker config, preserving the existing local daemon endpoint and avoiding Windows credential helpers. Model and endpoint-configuration names are generated by CloudFormation so a tunnel URL update can replace them while retaining the endpoint name.

At the end of preparation, ECR publication, CloudFormation resource creation, runtime IAM credentials in a SageMaker Serverless container, the public HTTPS tunnel, public URL access and cloud invocation latency were unverified. These technical checks were completed later, as recorded above; Ship It eligibility remains an organizer decision. S3 stores reports, not resumable sessions. The PC and tunnel must remain online for new inference; failed/unknown usage and negative reductions remain visible in the demo.

**Hybrid deployment preflight: real native CPU inference**

The official Windows x64 CPU build of llama.cpp b10433 and the existing Qwen3.5-4B Q4_K_M weights passed SHA-256 verification. The native CPU probe completed with exit 0: text generation returned `4`; the real Strands tool/StateTree example executed its tool, created and selected a commit, and returned `42` on its new task. Five individual model requests took 1.340–29.230 seconds, after a 48.847-second native startup. The temporary loopback server was stopped after the probe.

The second task selected the commit but also called the source tool again. This is evidence of working native inference and agent integration, not isolated commit-memory recall or token savings. SageMaker was replaced by a local-only HTTP transport for this probe; AWS/tunnel latency and the planned public web deployment remain unverified. See [the preflight record](HYBRID_DEPLOYMENT_PREFLIGHT.md) for measured usage, artifacts and remaining implementation work.

**CPU image cross-compilation: local Docker verification**

Replaced the emulated ARM compiler with a native Debian Bookworm cross-compiler targeting Graviton2's Neoverse-N1 CPU. The final ARM64 runtime uses matching Bookworm libraries. The completed, pinned model-download stage is unchanged and was reused from Docker's cache. Upstream's asset helper uses a separate native C++ compiler.

| Check | Result |
| --- | --- |
| Real `docker buildx build --platform linux/arm64 --build-arg BUILD_PARALLEL=4 --provenance=false --load` | Completed successfully, exit 0, under local tag `statetree-qwen35-cpu:cross-b10433`. |
| Compiler stage | Completed in 466.4 seconds on this Intel Windows computer, including configuration and the ARM64 binary check. First-time dependency installation and image export took additional time. |
| Binary architecture | `readelf` reported `AArch64`. |
| ARM executable and runtime libraries | `llama-server --version` ran successfully inside the final ARM image under emulation; commit `9b05354`, GNU 12.2.0, Linux aarch64. |
| nginx configuration | `nginx -t -c /etc/nginx/statetree.conf` passed inside the image. |
| Saved image | `docker image inspect statetree-qwen35-cpu:b10433` confirmed `linux/arm64`. Both `b10433` and `cross-b10433` refer to the same image. |
| Local model startup | Server logs reported model loaded after 4 minutes 45.75 seconds under ARM emulation, with one slot and a 4,096-token context. `/ping` returned HTTP 200 and `{"status":"ok"}`. |
| Local short generation probe | The `2+2` request to `/invocations`, capped at 8 output tokens, hit the client timeout after 55.08 seconds. No completed response was received or validated. Logs showed about 52.6 seconds of prompt evaluation under emulation; this is not a native SageMaker latency measurement. |
| Cleanup | The temporary local test container was stopped and automatically removed. The built image was retained. |
| Focused Python regression checks | All 15 tests in `tests.test_cpu_container` and `tests.test_sagemaker_cpu_deploy` passed in 20.285 seconds. |
| Guide commands | All 13 PowerShell blocks parsed without executing them. |

The image manifest is `sha256:df630872894e6143ef61c9ed7754a77b33830cc668e31e124d8402f263e15ae8`. Build and model startup are verified locally; successful generation, native SageMaker inference speed, tool compatibility and token savings remain unverified. No image was pushed to AWS and no AWS resource was created or invoked during this verification.

**Earlier Hyderabad CPU deployment preparation: local verification**

Added an experimental `qwen35-4b-cpu` profile for `ml.m6g.xlarge`, a custom Graviton2 llama.cpp image definition with pinned Q4_K_M weights, a short text probe, CPU context/output limits for the agent and benchmark, and a Windows deployment guide. The current deployment uses only Qwen3.5-4B.

| Check | Result |
| --- | --- |
| Existing and updated Python suite: 20 modules, three parallel subprocesses | 232 discovered: 230 passed, 2 optional framework tests skipped; no failed modules. |
| New container entrypoint module, run separately after it was added | All 8 tests passed: settings validation, safe argv construction, service failure, partial startup cleanup, SIGTERM before/during launch and forced termination. No real container/model process started. |
| Optional framework environment | Both adapter tests passed in 50.287 seconds; one upstream CrewAI import warning. |
| CPU deployment coverage | 7 tests passed, including an offline `python -S` plan, CPU/GPU mismatch rejection and botocore-validated create requests with injected credentials and Stubber. |
| Probe and benchmark | All 8 example tests and 6 benchmark tests passed, including one-call text validation and context/output budget checks before AWS client creation. |
| Independent CPU review | No material blockers found in the planner, guide, nginx route mapping or supervisor; reviewer independently passed 15 planner/example tests and 8 container tests. |
| CLI / documentation | CPU plan and both help commands succeeded offline. All 13 PowerShell blocks in the CPU guide parsed without executing them. |
| Static checks | All 61 Python files and `pyproject.toml` parsed; `git diff --check` and explicit whitespace checks on the 15 changed/new implementation and guide files passed. |

Across 21 test modules and the two environments, all **240 distinct tests** passed. Tests cover local Python behavior; they do not establish that an ARM image compiles or that Qwen inference succeeds on this instance.

Docker and AWS CLI were not available on PATH in this workspace. No image was built or pushed, model weights downloaded, AWS credentials inspected, or AWS resources created/invoked/deleted. Native image compilation, shared-library loading, nginx routing, model startup, tool compatibility and the 60-second buffered invocation limit still require the build and live checks in [the CPU test guide](SAGEMAKER_CPU_DEPLOYMENT.md). Real token savings, latency and hosting costs remain unmeasured.

**Previous SageMaker Qwen integration: local verification**

Added a lazy boto3 Strands provider for SageMaker-hosted Qwen, explicit deployment planning and guarded resource operations, a tool/commit-recall/model-switch smoke example, and a SageMaker live benchmark CLI. The deployment guide covers both Qwen3.5-4B and Qwen3.8-27B through the published AWS vLLM container.

| Check | Result |
| --- | --- |
| Full unittest suite, all 19 `tests.test_*` modules run in three parallel subprocesses | 218 tests discovered: 216 passed, 2 optional framework tests skipped. All 19 modules completed without failures. |
| Optional adapter environment: `python -X utf8 -B -m unittest tests.test_optional_adapters -v` | Both optional tests passed in 27.234 seconds; one upstream CrewAI import warning. |
| New SageMaker coverage | 20 provider/accounting tests, 19 deployment tests, 3 smoke-example tests and 1 benchmark CLI guard test passed. AWS requests use injected transports or botocore Stubber with fake credentials. |
| Independent review | Nine focused regressions passed after fixing shared-resource cleanup, false-positive recall validation and lost/stale usage on malformed or synthetic responses. |
| Deployment dry runs | Both model profiles produced plans under `python -S`, without importing an AWS SDK or resolving credentials. Model/parser, GPU sharding, image and AMI settings matched the guide. |
| CLI help | Smoke-example and live-benchmark help loaded without remote inference. |
| Offline demonstrations | Commit-note recall retained its 9,742 to 1,778 serialized-byte fixture result; all six local feature demonstrations passed. No remote requests. |
| Static checks | All 58 Python source/test/example/benchmark/deployment files and `pyproject.toml` parsed. `git diff --check` and explicit trailing-whitespace checks on new SageMaker files passed. |

All 218 distinct tests passed across the two environments. Valid endpoint-reported usage survives malformed response content; absent or invalid usage remains unknown, and a later pre-stream failure cannot reuse an earlier request's counts. Cleanup verifies tags, stable endpoint state, pending/shadow variants and reverse references before deleting; scans do not lock against concurrent AWS changes.

No AWS credentials were inspected and no endpoint, model or configuration was created, deleted or invoked in an AWS account. Actual GPU capacity, model/container startup, tool compatibility, latency, token savings and hosting costs remain unmeasured. Instance choices are deployment starting points, not tested performance guarantees. See [the step-by-step SageMaker guide](SAGEMAKER_DEPLOYMENT.md) for live validation and cleanup.

**Commit-note context retrieval: final verification**

Added immutable checkpoint notes, local keyword recall over current ancestry, declared dependency/supersession filtering, whole-request context budgeting, and explicit new-task history archival with linked evidence. This feature uses caller-written notes and no summarization model.

| Check | Result |
| --- | --- |
| Full unittest suite, all 16 `tests.test_*` modules run in three parallel subprocesses | 175 tests discovered: 173 passed, 2 optional framework tests skipped. All 16 modules passed after correcting a test expectation for Strands' wrapped provider error. |
| Optional adapter environment: `python -X utf8 -B -m unittest tests.test_optional_adapters -v` | Both optional tests passed in 12.627 seconds; one upstream CrewAI import warning. |
| `python -B -m examples.commit_context` | Relevant authentication note selected, unrelated UI note excluded, original history recovered exactly. Captured serialized request: 9,742 to 1,778 UTF-8 bytes; preflight estimates: 10,007 to 1,831. Two local scripted replies, zero remote requests. |
| `python -B -m examples.local_features` | All six existing local demonstrations passed; zero model requests or AWS calls. |
| Python AST parsing | All 50 Python source/test/example/benchmark files parsed. |
| `git -c core.autocrlf=false diff --check` | No whitespace errors. |

All 175 distinct tests passed across the two environments. The 32 new tests cover note integrity, relevance, budget limits, branch visibility, supersession, resource versions, real SDK note injection/evidence retrieval, restore, repeated task boundaries, cancellation, preparation failures and preservation of completed tool progress. Independent review reproduced archive-chain and hook-cancellation gaps; regressions now verify their fixes, along with the existing orphaned-runtime ownership check.

Byte-size reductions describe this fixed local fixture, not actual provider tokens, billing, latency or task quality. AWS, credentials and remote inference remain untouched. See [the commit context guide](COMMIT_CONTEXT.md) for usage and limitations.

**Previous local feature expansion**

The local feature expansion adds portable state, public framework adapters, model handoff, structured fact memory, durable checkpointed workflows, isolated Git worktree branches, conservative rebase/merge and independently verified speculative selection. AWS and live model evaluation remain deferred.

**Local feature expansion: final verification**

| Check | Result |
| --- | --- |
| `.venv/Scripts/python.exe -B -m unittest discover -s tests -q` | 143 tests discovered in 333.821 seconds: 141 passed, 2 optional framework tests skipped in this environment. |
| `.venv/framework-tests/Scripts/python.exe -X utf8 -B -m unittest tests.test_optional_adapters -v` | Both skipped tests passed against real LangGraph 1.2.11 and CrewAI 1.15.22, in 14.642 seconds. One upstream CrewAI import warning; no test failures or model requests. |
| `python -B -m examples.local_features` | All six local demonstrations passed: fact lifecycle, portable evidence/state, model bindings, durable replay, verified three-way speculation, conflicting-state rejection. |
| `python -B -m examples.checkpoint_restore` | Fresh process restored agent state and workspace; zero model calls. |
| Python AST parsing | All 46 Python source/test/example/benchmark files parsed successfully. |
| `git -c core.autocrlf=false diff --check` | No whitespace errors. |

Thus all 143 distinct tests passed across the two environments. Framework packages were downloaded into the separate, ignored `.venv/framework-tests` environment after sandbox network approval; the original Strands environment was preserved. SDK telemetry was disabled for optional tests, and CrewAI's import-time credential-store boundary was redirected to disposable test storage.

New coverage includes self-contained evidence bundles, cross-process state/model handoff with retained cumulative usage, public-adapter goal synchronization, model-switch rollback, incomplete tool transactions, type-sensitive fact updates and transitive invalidation, real isolated worktrees and indexes, stale declared file/resource reads, atomic expected-parent publication, verifier mutation, all-fail/loser accounting, mutable-plan preflight, interrupted baseline/step publication, unsafe-effect uncertainty, and concurrent workflow recovery.

Independent reviews found and verified fixes for stale goal/application context on handoff, failed-switch publication, dead runtime ownership, boolean/number fact deduplication, noncanonical file dependency aliases, plan mutation before effects, and workflow checkpoint/progress crash gaps. Branch testing also found and fixed a copied Git index stat-cache bug that could omit equal-length edits. Full findings and remediation evidence are in [the portable review](PORTABLE_REVIEW.md) and [the local feature review](LOCAL_FEATURES_REVIEW.md).

These checks use deterministic local strategies and scripted providers where inference is needed. They do not measure actual model token savings or prove arbitrary framework-private migration, semantic merging, or external exactly-once execution. AWS credentials, configuration, resources, deployments and live Bedrock evaluation were not changed or invoked.

The original MVP results below are retained as baseline evidence.

**Original MVP baseline**

Environment: Windows, Python 3.13.6, Strands Agents 1.56.0. All checks used the existing virtual environment. No paid model request was made.

| Check | Result |
| --- | --- |
| `python -B -m unittest discover -s tests -q` | 61 tests passed in 65.696 seconds. |
| `python -B -m examples.checkpoint_restore` | Agent state and workspace restored successfully in a fresh Python process. |
| `python -B -m benchmarks.replay --steps 30` | All requests stayed within the configured 6,000-unit input estimate budget. |
| `python -B -m benchmarks.live --help` | CLI loads without creating a provider or issuing requests. |
| Python AST parsing | All 27 Python source files parsed successfully. |
| `git -c core.autocrlf=false diff --check` | No whitespace errors. |

The test suite covers content-hash validation, immutable snapshots, stale writers, separate branch and canonical references, process death before a SQLite publication commit, fresh-process restoration, index/worktree separation, untracked-file policy, Git object pinning, restore collisions, index flags, context budgets, tool-call/result pairing, single-request tool loops, stable archive references, cache accounting, unknown usage, idempotent request records and concurrent duplicate delivery.

Strands integration tests run its real event loop against a scripted local provider. They check tool execution, multiple requests per invocation, retried/discarded responses, preparation errors, cumulative stop thresholds and all three live-benchmark policies. Their supplied usage counters are test fixtures, not real provider measurements.

The fixed-trace replay produced 697,545 cumulative full-history input-estimate units versus 153,645 for StateTree, a 77.97% reduction. Units are serialized UTF-8 bytes used as a conservative input estimate. The trace has fixed observations and no model inference: this result does not establish actual token, cost or task-success improvements.

An independent read-only review found two issues: Git index flags could hide working-file changes, and repeated compaction could create chains of archive references. Both were reproduced and corrected with regression tests. Index flags are now explicitly rejected; application metadata preserves direct archive references across recompaction.

The live Bedrock benchmark has not been run against a real provider. At the original MVP stage, isolated workspaces and model/framework migration were not implemented. The subsequent local expansion implements the defined public-state and registered-step workflows; it still does not claim arbitrary external-effect rollback, private-session migration, semantic merging, or real provider savings. See [the feature guide](LOCAL_FEATURES.md) for the current boundaries.
