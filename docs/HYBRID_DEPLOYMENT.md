# Deploy StateTree with Amazon Bedrock

This deployment uses an ECS Express web service and Amazon Bedrock Converse. The ECS task role can invoke only the configured foundation model. There is no local model server, Cloudflare tunnel, SageMaker endpoint, or model API key.

## Prerequisites

1. In the AWS console, enable access to your selected Bedrock foundation model in `ap-south-1`. The default command below uses `amazon.nova-lite-v1:0`; choose a model available to your account and region.
2. Start Docker Desktop and sign in with an AWS profile that can create ECR repositories, Secrets Manager secrets, an ECS Express service, IAM roles, and CloudFormation resources.
3. Run commands from the repository root.

## Publish and deploy

```powershell
Set-Location 'D:\aws hackathon\statetree'
.\.venv\Scripts\python.exe -B -m deploy.hybrid prepare
.\.venv\Scripts\python.exe -B -m deploy.hybrid publish --apply
.\.venv\Scripts\python.exe -B -m deploy.hybrid plan --bedrock-model-id amazon.nova-lite-v1:0
.\.venv\Scripts\python.exe -B -m deploy.hybrid deploy --bedrock-model-id amazon.nova-lite-v1:0 --apply
.\.venv\Scripts\python.exe -B -m deploy.hybrid status
```

If the existing hybrid stack was created with the old SageMaker gateway, redeploy it with `--update --apply`. CloudFormation will remove the gateway resources and replace the web service configuration.

## Run the project worker

After `status` reports `CREATE_COMPLETE` or `UPDATE_COMPLETE`, start the local worker. It owns the project files and sends model requests to the cloud application; AWS credentials remain in ECS.

```powershell
.\.venv\Scripts\python.exe -B -m deploy.local_agent prepare
.\.venv\Scripts\python.exe -B -m deploy.local_agent serve --url 'https://YOUR-WEB-URL' --project 'D:\aws hackathon\statetree'
```

Open the web URL, authenticate with the owner key from `build\hybrid\private\secret.json`, and select the registered project. Review generated diffs before applying them.

## Troubleshooting

- `AccessDeniedException` from Bedrock means the selected model is not enabled, unavailable in the region, or does not match the task role's `BEDROCK_MODEL_ID`.
- A model ID change requires `deploy.hybrid deploy --bedrock-model-id <id> --update --apply`.
- The worker does not need AWS credentials or inbound network access.
- Bedrock inference and the ECS service incur AWS charges. Delete the stack when finished:

```powershell
.\.venv\Scripts\python.exe -B -m deploy.hybrid delete --apply
```