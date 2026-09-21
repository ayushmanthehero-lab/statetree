# Hybrid deployment preflight — 20 September 2026

Target: an AWS-hosted StateTree demo with a public HTTPS URL. The proposed inference path is the AWS application → SageMaker Serverless gateway → authenticated HTTPS connection → native Qwen on the owner's PC. This is a proposed deployment; the web application, gateway and tunnel have not been built or deployed.

## Verified AWS account state

Account `446272719493`, Mumbai (`ap-south-1`):

- Applied Fargate On-Demand vCPU quota: **8**. This is a quota, not eight running or reserved CPUs.
- `ecs list-clusters` returned an empty list.
- `sagemaker list-endpoints` returned an empty list.
- Applied SageMaker Serverless limits, checked earlier in the session: **5 endpoints**, **10 total concurrent requests** in both Mumbai and Singapore.
- No billable AWS service was created by this preflight.

The Hyderabad `ml.m6g.xlarge` request and retained model/configuration belong to the earlier deployment attempt. They are separate from this proposed Mumbai setup.

## Native CPU test

Machine: Intel Core i5-11400H, 6 cores / 12 logical processors, approximately 16 GB RAM. The GPU was not used.

- Runtime: official Windows x64 CPU release of `llama.cpp` **b10433**, commit `9b05354ec6fb58b4e665e9a39ebc40285c015638`.
- Runtime archive SHA-256: `ab0c0fdb2da5df8e206175ff494e1ce542a77eb83942285a55592b027838fcd5`, matching the GitHub release asset digest.
- Model: `Qwen3.5-4B-Q4_K_M.gguf`, extracted from the existing local Docker image without downloading the weights again.
- Model SHA-256: `00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4`.
- Settings: CPU only, 6 inference threads, 6 batch threads, 1 slot, 4,096-token context, non-thinking mode, temperature 0, 16 output tokens for text / 64 for agent calls.
- The server listened only on loopback and was stopped after testing. No tunnel or public model listener was created.

| Observation | Result |
| --- | --- |
| Native server startup | 48.847 seconds |
| Text prompt: `What is 2+2? Reply only 4.` | Returned `4` in 1.340 seconds |
| First real Strands tool-selection call | 493 input tokens; 18.386 seconds |
| Response after actual tool execution | Returned the value 42; 2.346 seconds |
| New-task call with a selected commit note | 814 input tokens; 29.230 seconds |
| Final new-task response after tool execution | Returned `42`; 1.655 seconds |

The actual `examples.sagemaker_agent.run_demo()` was exercised through an injected **local HTTP transport**. The existing message/usage adapter, Strands tools and StateTree runtime ran normally; AWS's SageMaker transport did not participate.

The agent portion reported four model requests, **2,714 input tokens and 42 output tokens**. Prompt-cache tokens are included in the reported input count. The separate arithmetic call is not part of that agent total.

### What this establishes

Native local text generation, automatic structured tool calls, execution of the Python tool, checkpoint creation, commit-note selection and a new-task answer worked for this fixture. All five individual requests completed under the 55-second local client timeout; the slowest was 29.230 seconds.

On the second task, the model called `read_demo_value` again despite having a commit selected. This run therefore does **not** prove that it answered exclusively from commit memory or that commit memory saved tokens. There was no full-history comparison.

The result does not establish general coding quality, large-prompt latency, concurrent performance, or an end-to-end AWS/tunnel latency guarantee. Native startup must finish before exposing the local gateway. The 4,096 context setting is capacity, not a promise that a prompt of that size fits SageMaker's response deadline.

### Local artifacts

These files are under the Git-ignored `build/local-inference/` directory:

- `llama-b10433-cpu/llama-server.exe`: native runtime.
- `Qwen3.5-4B-Q4_K_M.gguf`: verified model copy.
- `native_probe.py`: one-off local feasibility probe; uses no AWS inference.
- `native-probe-report.json`: measured responses, usage and timing.
- `native-server.log`: local server startup/inference log.

The probe may be rerun deliberately from the project root. It starts and stops its own local server and makes real local model calls:

```powershell
.\.venv\Scripts\python.exe -B .\build\local-inference\native_probe.py
```

## Remaining implementation and deployment work

1. Add an authenticated local gateway with request-size limits, bounded output, one active inference at a time and short controlled timeouts. Expose only the required model API through HTTPS.
2. Build a small SageMaker Serverless forwarding container for Mumbai. It must obtain its upstream credential securely, avoid logging credentials/prompts, and return a controlled error when the PC is offline. The earlier ARM64 model-hosting image and `EnableNetworkIsolation=true` configuration cannot be reused as this gateway.
3. Add a web UI and AWS backend that run the existing StateTree/Strands fixture against that gateway. Submit a demo run, return a run ID, and poll status instead of holding one browser request open for the entire multi-call agent workflow. Include actual model usage, selected commit notes and verifiable outputs.
4. Give each run a separate disposable repository and serialize runs for the initial demo. Permit only the demo's predefined tools, not arbitrary commands against the owner's workspace.
5. Preserve completed reports and explicitly exported public-state artifacts in private S3 storage. **Existing public-state bundles do not export a full Git/SQLite/agent session.** A replaced backend must report interrupted runs and start fresh; it must not claim seamless session recovery from those bundles.
6. Build and push the web container, configure its IAM task permissions, and deploy one small ECS Express Mode service. Verify public HTTPS access, AWS-to-PC authentication, offline behavior and cleanup before recording the submission.

ECS Express Mode supplies public HTTPS, a load balancer and Fargate resources; these underlying resources incur charges. SageMaker Serverless has a 60-second per-inference response deadline, which includes time spent waiting for the PC through the forwarding container.

The event page describes Ship It as an AWS deployment with a URL, but does not explicitly determine eligibility of this laptop-inference hybrid. The submission must accurately disclose that Qwen runs locally. No Ship It eligibility guarantee follows from this technical preflight.

## References

- [Official llama.cpp b10433 release](https://github.com/ggml-org/llama.cpp/releases/tag/b10433)
- [SageMaker Serverless invocation limits](https://docs.aws.amazon.com/sagemaker/latest/dg/serverless-endpoints-invoke.html)
- [SageMaker Serverless capabilities](https://docs.aws.amazon.com/sagemaker/latest/dg/serverless-endpoints.html)
- [ECS Express Mode](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-overview.html)
- [First Commit competition page](https://www.wemakedevs.org/aws/first-commit)
