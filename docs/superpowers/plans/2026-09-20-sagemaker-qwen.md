# SageMaker Qwen Integration Plan

**Goal:** Run StateTree against the user's own SageMaker AI endpoints hosting Qwen3.5-4B and Qwen3.8-27B, and provide executable deployment instructions.

**Design:** Keep the existing Strands runtime, commit notes, archives, checkpoints and usage ledger. Add a stateless Strands model adapter using boto3 `sagemaker-runtime.invoke_endpoint` and OpenAI-compatible JSON accepted by the AWS vLLM container. Provision two independent real-time endpoints using the official vLLM DLC and model-specific tool parsers. No Bedrock inference is involved.

**Constraints:** Make local code/documentation changes only in this turn. No AWS account changes, credentials inspection, deployment, billable requests or model downloads. Existing uncommitted work remains in place. No implicit endpoint creation/deletion from application examples. Deployment plan/help must work without credentials. Actual GPU deployment compatibility, quota, inference latency and cost remain to be validated in the user's AWS account.

## Interfaces and ownership

- Provider worker: `statetree/models/sagemaker.py`, package exports and `tests/test_sagemaker.py`. `SageMakerModel(endpoint_name, region_name=..., model_id=..., client=None, max_tokens=512, temperature=0.7, enable_thinking=False, context_window_limit=8192, inference_component_name=None, profile_name=None)`. Lazy AWS client; detached safe config; text/tool conversion; exact provider usage via `last_provider_usage`; no fabricated zero counts.
- Deployment worker: `deploy/sagemaker.py`, package marker and `tests/test_sagemaker_deploy.py`. Model profiles `qwen35-4b` and `qwen38-27b`; credential-free `plan`; explicit `--apply` on resource mutations; existing resource collision/ownership checks; status and cleanup.
- Controller: SageMaker example, live benchmark migration, raw usage integration, dependency declaration, README and step-by-step deployment guide.

## Steps

- [x] Write failing provider/deployment tests before implementation, exercising real SDK data contracts through stubbed transport.
- [x] Implement the provider and raw provider-usage propagation; test tool exchanges, errors, missing counts and checkpoint/model handoff.
- [x] Implement the deployment planner and guarded resource operations; test dry-run, profiles, image/AMI settings and cleanup ownership.
- [x] Replace Bedrock in active usage examples and live benchmark with explicit SageMaker endpoint arguments; preserve offline demos and benchmark policies.
- [x] Document IAM roles, billing/quotas, region, deployment/status, smoke checks, model handoff, benchmark usage and cleanup with primary-source links.
- [x] Independently review the change; run the complete test suite, offline demos, CLI dry runs/help and whitespace checks.

## Review focus

- No credentials lookup or AWS call during import, construction, help, plan or offline tests.
- The SDK's default zero usage must not mask missing endpoint-reported usage.
- Qwen3.8 and Qwen3.5 require different tool parsers; no guessed JumpStart ID for 3.8.
- Regular InvokeEndpoint has a 60-second container response deadline; short non-thinking requests first, streaming deferred.
- GPU sizing and CUDA-compatible AMI are explicit and remain unvalidated until deployed.
- SageMaker endpoints are authenticated model services, not a public application URL for event judges.
- GPU hosting is billed for uptime; token reduction is not automatically an equal billing reduction.
