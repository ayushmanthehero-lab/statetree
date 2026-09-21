# Deploy StateTree with Qwen on SageMaker AI

**For the current Hyderabad `ml.m6g.xlarge` experiment, use [the CPU test guide](SAGEMAKER_CPU_DEPLOYMENT.md).** This page describes optional GPU deployments. Its vLLM image cannot run on the selected CPU instance; the current test uses only quantized Qwen3.5-4B.

This guide deploys your own Qwen inference endpoints. StateTree runs in your application and calls those endpoints using AWS credentials. No Bedrock model, OpenAI-hosted model or Qwen Cloud API key is used. The chat JSON follows an OpenAI-compatible protocol; that is a message format, not the inference provider.

The event lists SageMaker AI for the deployed Agents and AI track. It does not mandate these Qwen versions. The Ship It track also asks for an application URL: an authenticated SageMaker endpoint is not a public website for judges. [First Commit requirements](https://www.wemakedevs.org/aws/first-commit)

Implementation status: the adapter, deployment planner and commands are locally tested with stubbed AWS transport. No endpoint has been deployed or invoked in your account. GPU capacity, model startup, tool calling and real token measurements still require the live checks below.

## 1. Choose the first endpoint and region

Start with the 4B model. The 27B profile is an optional future GPU experiment, not part of the current deployment.

| Helper profile | Exact model | Starting instance | Tensor parallel size | Tool parser |
| --- | --- | --- | --- | --- |
| `qwen35-4b` | `Qwen/Qwen3.5-4B` | `ml.g6.2xlarge` | 1 | `qwen3_coder` |
| `qwen38-27b` | `Qwen/Qwen3.8-27B` | `ml.g6.12xlarge` | 4 | `qwen3_xml` |

These are initial capacity configurations, not measured performance guarantees. Both use BF16 weights, an 8,192-token server context and at most four concurrent sequences. Reducing context/concurrency can help memory pressure. Do not select a smaller GPU instance without checking memory and tensor-parallel compatibility.

The tool parsers come from the [Qwen3.5 model card](https://huggingface.co/Qwen/Qwen3.5-4B) and the [official vLLM Qwen3.8 recipe](https://recipes.vllm.ai/Qwen/Qwen3.8-27B). We use the published AWS vLLM `0.29.0-gpu-py312-cu130-ubuntu24.04-sagemaker` container with a CUDA-13-compatible SageMaker AMI, `al2023-ami-sagemaker-inference-gpu-4-1`. [AWS container images](https://aws.github.io/deep-learning-containers/reference/available_images/), [GPU driver compatibility](https://docs.aws.amazon.com/sagemaker/latest/dg/inference-gpu-drivers.html)

Examples below use `us-west-2`. Use one supported region consistently for endpoints, model images and invocation; check actual instance availability and quotas there. SageMaker JumpStart lists Qwen3.5-4B as `huggingface-vlm-qwen3-5-4b`; a Qwen3.8 entry was not found in the published catalog when this guide was written. The supplied commands deploy both models directly through the official container, so no guessed JumpStart ID is needed. [AWS model catalog](https://docs.aws.amazon.com/sagemaker/latest/dg/jumpstart-foundation-models-latest.html)

## 2. Check account access, quota and spending

In the AWS console:

1. Select the intended region.
2. Open **Service Quotas → AWS services → Amazon SageMaker** and search for the chosen instance type's **endpoint usage** quota. Request at least one instance of each type you intend to deploy. A zero quota prevents deployment; availability is separate from quota.
3. Check your credits and set a billing budget alert before creating GPU endpoints. Budget alerts are notifications, not hard spending caps.
4. Check the region's inference price. Real-time endpoints incur hosting charges while provisioned, including when idle. Open model weights do not make GPU hosting free. Deleting the endpoint ends its provisioned hosting; merely closing your terminal does not. [SageMaker pricing](https://aws.amazon.com/sagemaker/ai/pricing/)

Your deployer identity needs SageMaker create/describe/delete permissions for models, endpoint configurations and endpoints, plus `AddTags`, `ListTags`, `ListEndpoints`, `ListEndpointConfigs` and `iam:PassRole` for the execution role. Cleanup uses regional list access and `DescribeEndpoint`/`DescribeEndpointConfig` access across the target account and region to check that other resources do not depend on the model or configuration being deleted. Inference callers need `sagemaker:InvokeEndpoint` on the specific endpoint ARN. Keep deployment and invocation privileges separate for a production app.

## 3. Create or obtain a SageMaker execution role

If your account already provides a SageMaker execution role, use it. Otherwise, in **IAM → Roles → Create role**, select the AWS service **SageMaker** as the trusted service and follow the SageMaker execution-role setup. The role needs access to pull the AWS inference container and publish logs. AWS's getting-started execution-role setup uses `AmazonSageMakerFullAccess`; a production role can be scoped more narrowly. Copy the resulting role ARN. This role is distinct from the identity you use to deploy. [AWS execution-role instructions](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-roles.html)

The supplied configuration downloads public model weights from Hugging Face during startup, so network isolation is disabled and outbound internet must be available. It does not create a VPC, NAT gateway, S3 model bucket or Studio domain. If your account requires isolated networking, first stage the weights in S3 and use a separate approved networking configuration. [Container deployment and model artifacts](https://aws.github.io/deep-learning-containers/vllm/deployment/sagemaker/)

## 4. Prepare your Windows terminal

Install AWS CLI v2 using the [official Windows installer instructions](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html). If your account uses IAM Identity Center, configure and sign in to a named profile:

```powershell
aws configure sso --profile statetree
aws sso login --profile statetree
aws sts get-caller-identity --profile statetree
```

Use your organization's supplied credentials method if it does not use Identity Center; the code uses the normal AWS credential chain. Do not put access keys in source code. [AWS CLI SSO configuration](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html)

For an account that uses IAM user access keys instead, run `aws configure --profile statetree` and enter that user's access key ID, secret access key and region at the prompts, then run `aws sts get-caller-identity --profile statetree`. Use a permitted IAM identity, not root account access keys. This is an alternative to the SSO commands; it does not create the IAM user or grant deployment permissions. [AWS CLI credential configuration](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html)

From your workspace:

```powershell
Set-Location 'D:\aws hackathon\statetree'
.\.venv\Scripts\python.exe -m ensurepip --upgrade
.\.venv\Scripts\python.exe -m pip install -e '.[sagemaker]'

$smRegion = 'us-west-2'
$smAwsProfile = 'statetree'
$smExecutionRole = 'arn:aws:iam::123456789012:role/StateTreeSageMakerExecutionRole'
```

Replace the example role ARN with yours. The project already has boto3 in its current environment; `ensurepip` is only needed if pip is absent. No separate `sagemaker` Python SDK, Docker build or OpenAI package is required for this path. The `deploy` module is run from the repository root.

## 5. Preview the 4B deployment without contacting AWS

```powershell
.\.venv\Scripts\python.exe -B -m deploy.sagemaker plan --profile qwen35-4b --region $smRegion --endpoint-name statetree-qwen35-4b --execution-role-arn $smExecutionRole
```

Inspect the image URI, instance type, role, region and model environment in the JSON output. The helper selects the published registry for the region. The plan requires no AWS credentials and creates nothing.

It will create exactly three named SageMaker resources:

- Model: `statetree-qwen35-4b-model`
- Endpoint configuration: `statetree-qwen35-4b-config`
- Endpoint: `statetree-qwen35-4b`

## 6. Deploy the 4B endpoint

The following command creates chargeable AWS resources. Run it after checking the plan, role, quota and price:

```powershell
.\.venv\Scripts\python.exe -B -m deploy.sagemaker deploy --profile qwen35-4b --region $smRegion --endpoint-name statetree-qwen35-4b --execution-role-arn $smExecutionRole --aws-profile $smAwsProfile --apply --wait
```

`--apply` is required for mutation. `--wait` waits for `InService`; first startup downloads model weights and may take a while. In a second terminal, check progress with (adjust the region and profile if you changed them):

```powershell
Set-Location 'D:\aws hackathon\statetree'
.\.venv\Scripts\python.exe -B -m deploy.sagemaker status --region us-west-2 --endpoint-name statetree-qwen35-4b --aws-profile statetree
```

You can also open **SageMaker AI → Inference → Endpoints**. If deployment fails, inspect `FailureReason` and the CloudWatch log group `/aws/sagemaker/Endpoints/statetree-qwen35-4b`. The helper leaves created resources available for inspection and reports their names; it does not silently roll them back. Clean up failed resources before reusing the same endpoint name.

## 7. Test text, tools, commit recall and usage

Once the endpoint is `InService`:

```powershell
.\.venv\Scripts\python.exe -B -m examples.sagemaker_agent --live --endpoint-name statetree-qwen35-4b --model-id Qwen/Qwen3.5-4B --region $smRegion --aws-profile $smAwsProfile
```

This creates a disposable local Git repository, asks Qwen to call a small local tool, writes a commit note and starts another task that recalls the recorded value. It checks that the tool actually ran, then prints the response, recalled checkpoint IDs and usage. It does not deploy or delete endpoints. `--live` explicitly enables inference.

The adapter uses SigV4-authenticated `InvokeEndpoint`, not an API key. It buffers each response and converts text/tool calls into Strands events. Start with the default `max_tokens=512` and thinking disabled: regular `InvokeEndpoint` requires the container to respond within 60 seconds. Increasing client timeouts does not remove that server limit; long reasoning needs a streaming transport, which this adapter does not implement. [AWS InvokeEndpoint contract](https://docs.aws.amazon.com/sagemaker/latest/APIReference/API_runtime_InvokeEndpoint.html)

If the model emits raw tool markup or never calls the tool, inspect the deployed `SM_VLLM_TOOL_CALL_PARSER` and `SM_VLLM_ENABLE_AUTO_TOOL_CHOICE` settings. The helper sets them for each model. [AWS vLLM configuration](https://aws.github.io/deep-learning-containers/vllm/configuration/)

## 8. Add Qwen3.8-27B and test switching

After approving the larger instance's cost and quota, preview it:

```powershell
.\.venv\Scripts\python.exe -B -m deploy.sagemaker plan --profile qwen38-27b --region $smRegion --endpoint-name statetree-qwen38-27b --execution-role-arn $smExecutionRole
```

Then deploy and test both endpoints:

```powershell
.\.venv\Scripts\python.exe -B -m deploy.sagemaker deploy --profile qwen38-27b --region $smRegion --endpoint-name statetree-qwen38-27b --execution-role-arn $smExecutionRole --aws-profile $smAwsProfile --apply --wait

.\.venv\Scripts\python.exe -B -m examples.sagemaker_agent --live --endpoint-name statetree-qwen35-4b --large-endpoint-name statetree-qwen38-27b --region $smRegion --aws-profile $smAwsProfile
```

The first task runs on 4B, StateTree checkpoints its public state, then the next task runs on 27B using relevant commit context. Both endpoint resources remain provisioned. Switching models does not automatically scale down or delete either endpoint.

Use the same pattern in your application:

```python
from statetree.models.sagemaker import SageMakerModel

large = SageMakerModel(
    'statetree-qwen38-27b', region_name='us-west-2',
    model_id='Qwen/Qwen3.8-27B', profile_name='statetree',
    max_tokens=512, enable_thinking=False, context_window_limit=8192,
)
# runtime must already have bounded context and initialized AgentState.
runtime.switch_model(large)
runtime.run('Continue the task using the recorded decisions', new_task=True)
```

Model choice is explicit; an automatic small/large routing policy has not been added.

## 9. Measure real usage after the smoke test

Run a small matched benchmark on one endpoint first:

```powershell
.\.venv\Scripts\python.exe -B -m benchmarks.live --live --endpoint-name statetree-qwen35-4b --model-id Qwen/Qwen3.5-4B --region $smRegion --aws-profile $smAwsProfile --tasks 1 --steps 2 --output benchmark-results/qwen35-4b.json
```

Repeat with the 27B endpoint and model ID into a different output file. Each benchmark compares full history, Strands automatic context management and StateTree compaction on the existing document-retrieval fixture. It is not a full coding benchmark or an isolated evaluation of commit-note quality. The adapter preserves endpoint-reported token counts; missing counts are flagged unknown rather than converted to measured zeros. vLLM cached prompt counts use the `included` convention.

Compare success as well as input/output tokens and latency. SageMaker instance uptime is a separate cost: reducing prompt tokens may improve speed and throughput, but does not automatically reduce an always-running endpoint's hourly charge.

## 10. Delete endpoints when finished

After recording your results, delete each endpoint you no longer need:

```powershell
.\.venv\Scripts\python.exe -B -m deploy.sagemaker delete --region $smRegion --endpoint-name statetree-qwen35-4b --aws-profile $smAwsProfile --apply
.\.venv\Scripts\python.exe -B -m deploy.sagemaker delete --region $smRegion --endpoint-name statetree-qwen38-27b --aws-profile $smAwsProfile --apply
```

Cleanup checks the `StateTreeManaged=true` tags and resource relationships, including references from other endpoints/configurations. It rejects pending deployments, transitional endpoint states, unexpected shadow variants and incomplete reference scans. It waits for endpoint deletion, then removes that endpoint's configuration and model definition. Keep other deployment changes paused during cleanup: reference scans are snapshots, not an account-wide lock. It does not delete IAM roles, CloudWatch logs or unrelated endpoints. Resources created manually or through JumpStart must be cleaned up through their own workflow. Confirm the endpoints no longer appear as provisioned in the console.

## Troubleshooting and submission boundary

| Symptom | Next action |
| --- | --- |
| `ResourceLimitExceeded` | Check the selected instance's SageMaker endpoint quota in the same region. |
| Insufficient capacity | Try a region/compatible instance where quota and capacity are available; update tensor parallelism as needed. |
| `AccessDenied` | Check the caller's SageMaker permissions, tagging permissions, `iam:PassRole`, and role trust policy. |
| Image/driver/startup failure | Check image regional availability, explicit AMI compatibility and CloudWatch logs; override `--image-uri` or `--inference-ami-version` only with a compatible pair. |
| Out of memory | Lower `--max-model-len`, reduce concurrency in the plan or choose compatible larger GPU capacity. |
| Model download fails | Check outbound access to Hugging Face and available disk space; no HF token is embedded in this setup. |
| Inference exceeds 60 seconds | Reduce input/output, keep thinking disabled; use a streaming implementation for sustained long requests. |
| Endpoint exists | Inspect it; use a new name or explicitly clean up the owned resources before redeploying. |

This work deploys the model-serving layer. StateTree's application process, local Git workspaces and SQLite stores remain where you run your Python program. A public frontend/backend with persistent storage is a separate deployment needed for a judge-accessible Ship It URL. Do not expose AWS credentials in browser code or make a raw arbitrary-code agent public.
