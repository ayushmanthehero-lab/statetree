# Test Qwen3.5-4B on ml.m6g.xlarge in Hyderabad

Use this guide for your available **endpoint usage** quota in **Hyderabad (`ap-south-2`)**. It deploys one CPU endpoint for Qwen3.5-4B and runs StateTree on your Windows computer. You do not need a Studio domain or notebook instance.

`ml.m6g.xlarge` has 4 vCPUs, 16 GiB RAM, a Graviton2 ARM64 processor and no GPU. Use a quantized model for this experiment. The published AWS llama.cpp ARM image targets **Graviton3**, so this project supplies a separate Graviton2 build. The previous vLLM GPU deployment instructions do not apply. [Instance specifications](https://docs.aws.amazon.com/ec2/latest/instancetypes/gp.html), [AWS image architecture](https://aws.github.io/deep-learning-containers/llama-cpp/)

**Status:** The ARM64 image built successfully on this Windows computer using native cross-compilation. The executable and nginx checks passed, and the local container loaded the model and returned HTTP 200 from `/ping`. A short generation request timed out after 55 seconds under ARM emulation; successful generation and native SageMaker performance remain unverified. Python behavior is tested locally with fake model/AWS transports. Follow the text check before trying the agent. This is an experimental CPU test, not a performance guarantee.

## 1. Install the local tools

Install [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/) with its WSL 2 backend and Linux containers, and [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html). Start Docker Desktop. Python and Git are already used by this project.

Open PowerShell and run the commands from the project directory. Run each step separately and stop if a command fails.

Copy only the commands inside the PowerShell code blocks. Do not copy terminal prompts such as `PS D:\...>`, continuation markers (`>>`), or Docker progress lines starting with `=>` or `#12`. Progress lines are output to read; wait for the command to finish before entering the next one.

```powershell
Set-Location 'D:\aws hackathon\statetree'
docker version
docker buildx version
aws --version
.\.venv\Scripts\python.exe -m ensurepip --upgrade
.\.venv\Scripts\python.exe -m pip install -e '.[sagemaker]'
```

Docker must report a running Linux server. This build downloads approximately 2.74 GB of model weights plus the base images and build dependencies; leave additional disk space for image layers, compiler output and image export.

**Check Docker's storage drive, even when the project is on D:.** An earlier build on this machine exhausted C: because Docker's WSL disk was stored there. Docker normally stores WSL data under `%LOCALAPPDATA%\Docker\wsl`; the project directory does not control that location. In Docker Desktop, open **Settings > Resources > Advanced > Disk image location** to select a directory on a drive with enough space, such as `D:\DockerData`, then apply and restart. If C: is already full, free some space there first. Verify the new location before rebuilding. Use Docker's relocation control rather than moving an active VHD file manually. [Docker WSL storage settings](https://docs.docker.com/desktop/features/wsl/)

## 2. Set up AWS access and the execution role

Sign in using your existing AWS CLI profile. For IAM Identity Center, use `aws configure sso`, then `aws sso login --profile YOUR_PROFILE`. If you use a named profile, set `$env:AWS_PROFILE = 'YOUR_PROFILE'` in this PowerShell session so both AWS CLI and Python use it. Do not put credentials in project files.

In the AWS console, select **Asia Pacific (Hyderabad)**. Confirm the `ml.m6g.xlarge for endpoint usage` applied quota is at least **1** and is not already consumed. Quota does not guarantee immediately available capacity. Check Hyderabad's **real-time inference** hourly price, rather than Studio notebook pricing. Endpoints bill while provisioned, including when idle. [SageMaker pricing](https://aws.amazon.com/sagemaker/ai/pricing/)

In **IAM > Roles**, use an existing SageMaker execution role or create a role trusted by `sagemaker.amazonaws.com` using the SageMaker service role workflow. It needs ECR image-pull and CloudWatch logging access. Copy its ARN. The AWS getting-started role uses `AmazonSageMakerFullAccess`; use your organization's narrower role if provided. [Execution-role setup and permissions](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-roles.html)

Your local deployment identity separately needs ECR repository/create/push/read permissions, SageMaker create/describe/delete/tag/list permissions and `iam:PassRole` for that role. Invocation needs `sagemaker:InvokeEndpoint`. The cleanup helper needs regional list/describe access to check for shared resources.

```powershell
$Region = 'ap-south-2'
$Endpoint = 'statetree-qwen35-cpu'
$RoleArn = 'arn:aws:iam::YOUR_ACCOUNT_ID:role/YOUR_SAGEMAKER_EXECUTION_ROLE'
aws sts get-caller-identity
$AccountId = (aws sts get-caller-identity --query Account --output text).Trim()
$Registry = "${AccountId}.dkr.ecr.${Region}.amazonaws.com"
$Repository = 'statetree-qwen35-cpu'
$ImageTag = 'b10433-q4km-graviton2'
$ImageUri = "${Registry}/${Repository}:${ImageTag}"
```

Replace the role ARN before deployment. Use a fresh endpoint name if the suggested name already exists.

## 3. Build the Graviton2 container locally

```powershell
docker buildx build --platform linux/arm64 --provenance=false --build-arg BUILD_PARALLEL=4 --progress=plain --load -t statetree-qwen35-cpu:b10433 .\deploy\llama_cpp_cpu
docker image inspect statetree-qwen35-cpu:b10433 --format '{{.Os}}/{{.Architecture}}'
```

The second command must print `linux/arm64`. On an Intel/AMD Windows computer, the Debian Bookworm builder runs the compiler natively and cross-compiles llama.cpp `b10433` for ARM64 Neoverse-N1. This avoids running the large compilation through QEMU. The final image also uses Debian Bookworm, with matching runtime libraries. The smaller ARM package-installation and executable startup checks still use emulation. [Docker cross-platform builds](https://docs.docker.com/build/building/multi-platform/)

The verified image is already present on this development computer as `statetree-qwen35-cpu:b10433`; inspect it instead of rebuilding when the Dockerfile and serving files are unchanged. The compiler stage took about 7 minutes 46 seconds; first-time package installation and image export took additional time.

The command above uses four compiler jobs for this 16 GiB development computer and prints the build stages as plain text. If memory becomes tight or the compiler is killed, use `--build-arg BUILD_PARALLEL=2`; two jobs remain the Dockerfile default.

The separate Amazon Linux 2023 model-download stage is unchanged, allowing Docker to reuse the completed model layer from the earlier build when it remains cached. The image bundles the server libraries and SHA256-verified GGUF. The model is stored outside `/opt/ml/model`, which SageMaker mounts separately. No model download is needed at endpoint startup; the CPU plan enables network isolation.

The weights are a **third-party Unsloth quantization** of Qwen3.5-4B, not an official Qwen GGUF release:

| Artifact | Pinned value |
| --- | --- |
| Repository | `unsloth/Qwen3.5-4B-GGUF` |
| File | `Qwen3.5-4B-Q4_K_M.gguf` |
| Revision | `720bb031aae5488eae5d6a78768e6d826662b2ae` |
| SHA256 | `00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4` |
| File size | 2,740,937,888 bytes; total RAM usage is larger |

[Artifact and license metadata](https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/blob/main/Qwen3.5-4B-Q4_K_M.gguf), [pinned upload](https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/commit/720bb031aae5488eae5d6a78768e6d826662b2ae). If the build reports a checksum mismatch, stop and investigate rather than disabling the check.

Before pushing, check that the image starts locally:

```powershell
docker run --detach --rm --platform linux/arm64 --name statetree-cpu-check -p 127.0.0.1:8080:8080 statetree-qwen35-cpu:b10433 serve
docker logs statetree-cpu-check
Invoke-RestMethod 'http://localhost:8080/ping'
```

Loading takes time; repeat the ping after the logs show the model is ready. On this Intel Windows computer, the model loaded in about 4 minutes 46 seconds under ARM emulation. If it fails, inspect the logs. An ARM container running under emulation on your PC is a startup check, not a SageMaker speed measurement. Stop the disposable container after this check:

```powershell
docker stop statetree-cpu-check
```

## 4. Push to ECR in Hyderabad

This step creates a private image repository and uploads the image. ECR storage can incur charges. Skip `create-repository` if your repository already exists.

```powershell
aws ecr create-repository --region $Region --repository-name $Repository
aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $Registry
docker tag statetree-qwen35-cpu:b10433 $ImageUri
docker push $ImageUri
$ImageDigest = (aws ecr describe-images --region $Region --repository-name $Repository --image-ids "imageTag=$ImageTag" --query 'imageDetails[0].imageDigest' --output text).Trim()
$DeployImage = "${Registry}/${Repository}@${ImageDigest}"
```

The deployment uses the uploaded digest so changing a tag later does not silently change the chosen image. [AWS ECR push instructions](https://docs.aws.amazon.com/AmazonECR/latest/userguide/docker-push-ecr-image.html)

## 5. Preview, then deploy one endpoint

Previewing is offline and creates nothing:

```powershell
.\.venv\Scripts\python.exe -B -m deploy.sagemaker plan --profile qwen35-4b-cpu --region $Region --endpoint-name $Endpoint --execution-role-arn $RoleArn --image-uri $DeployImage
```

Confirm the plan selects `ml.m6g.xlarge`, one instance, 4,096 context tokens, four CPU threads and one concurrent inference slot. Thinking is disabled. The served alias `Qwen/Qwen3.5-4B` identifies the bundled quantized model in requests; the actual artifact is the Q4_K_M file listed above.

**The next command creates the billable SageMaker endpoint:**

```powershell
.\.venv\Scripts\python.exe -B -m deploy.sagemaker deploy --profile qwen35-4b-cpu --region $Region --endpoint-name $Endpoint --execution-role-arn $RoleArn --image-uri $DeployImage --apply --wait
```

Wait for `InService`. Check status at any time:

```powershell
.\.venv\Scripts\python.exe -B -m deploy.sagemaker status --region $Region --endpoint-name $Endpoint
```

Deployment errors retain any resources already created and print their names. Inspect **SageMaker AI > Inference > Endpoints** and the CloudWatch log group `/aws/sagemaker/Endpoints/statetree-qwen35-cpu` (substitute your name). Do not repeatedly deploy under new names without cleaning up failed attempts.

## 6. Test a very short text response

```powershell
.\.venv\Scripts\python.exe -B -m examples.sagemaker_agent --live --mode text --region $Region --endpoint-name $Endpoint --max-output-tokens 16 --context-window-limit 4096
```

Success returns JSON containing `"text": "4"`, endpoint-reported `raw_usage` and `elapsed_seconds`. This makes one tool-free model request. It does not test token savings yet.

The current adapter uses buffered `InvokeEndpoint`, whose model response must finish within **60 seconds**. A longer client timeout does not remove this limit. If this tiny probe times out, stop here: 4B on this CPU is not suitable for the current real-time path without further changes. A smaller model, faster instance or a separately implemented streaming/asynchronous path would be the next experiment. [AWS invocation limit](https://docs.aws.amazon.com/sagemaker/latest/APIReference/API_runtime_InvokeEndpoint.html)

## 7. Test tool execution and commit recall

Only after the text probe succeeds:

```powershell
.\.venv\Scripts\python.exe -B -m examples.sagemaker_agent --live --mode agent --region $Region --endpoint-name $Endpoint --max-output-tokens 64 --context-window-limit 4096
```

This creates a temporary Git repository, asks the model to call `read_demo_value`, records a StateTree commit note, starts a new task and recalls the value from the saved context. Success includes `"recall_response": "42"`, a checkpoint ID, selected commit IDs and recorded usage. It uses only this one endpoint.

The 64-token cap keeps the first CPU trial short. If logs show a truncated tool call (`finish_reason: length`), a 128-token retry may be needed, provided latency leaves enough room. Tool calling relies on llama.cpp's Jinja template and automatic selection; this guide does not promise forced named-tool compatibility. Multimodal requests are outside this test. [Pinned server protocol](https://github.com/ggml-org/llama.cpp/blob/b10433/tools/server/README.md)

## 8. Run a small comparison after both smoke tests pass

```powershell
.\.venv\Scripts\python.exe -B -m benchmarks.live --live --region $Region --endpoint-name $Endpoint --tasks 1 --steps 1 --budget 3000 --max-output-tokens 64 --context-window-limit 4096 --output benchmark-results/sagemaker-cpu-smoke.json
```

This runs the same small task under full history, Strands automatic context management and StateTree. Check each policy's success and incomplete usage counts before interpreting token totals. One short task may show no savings; it is a plumbing check. Increase steps gradually only if latency and context size permit. These results are not evidence of general coding-agent quality. Q4_K_M also changes model quality relative to an unquantized model, so keep the same endpoint and settings for all policies.

Self-hosting costs primarily depend on provisioned instance time. Reducing prompt tokens may improve latency or throughput, but it does not directly reduce an hourly SageMaker bill while the endpoint remains running.

## 9. Delete the endpoint after testing

```powershell
.\.venv\Scripts\python.exe -B -m deploy.sagemaker delete --region $Region --endpoint-name $Endpoint --apply
```

The helper checks StateTree tags and shared references, then deletes the endpoint, its configuration and its model. Wait until deletion finishes and verify the endpoint is gone in the console. If deployment is still `Creating` or changing state, wait for a stable status before using this guarded cleanup. Closing PowerShell does not delete an endpoint.

The ECR image and CloudWatch logs remain. Once you no longer need the image, delete the dedicated image/repository through **ECR > Private repositories**; only remove the repository created for this experiment. Review retained logs through CloudWatch. No Studio instance was created by these steps.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `exec format error` | Image must be `linux/arm64`; rebuild with the exact platform command. |
| `Illegal instruction` | Confirm this project's Neoverse-N1 build was used, not the Graviton3 AWS image. |
| Image build or model checksum failure | Stop before creating the endpoint; inspect the Docker build output and pinned download. |
| `ResourceLimitExceeded` | Confirm `ap-south-2`, endpoint quota at least one, and existing usage. |
| `InsufficientInstanceCapacity` | Quota and physical capacity differ; retry later after checking retained resources. |
| ECR pull / `AccessDenied` | Check image region, execution-role pull access, deployer permissions and `iam:PassRole`. |
| Failed health check | Inspect CloudWatch for model loading, shared-library, memory or argument errors. |
| Text works, tool test fails | Inspect tool template/output truncation and latency; do not claim the project is validated yet. |
| Timeout | Reduce request size/output; the 60-second buffered response limit still applies. |
