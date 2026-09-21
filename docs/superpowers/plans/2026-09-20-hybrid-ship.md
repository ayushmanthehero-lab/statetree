# Hybrid Ship Demo Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for independent tasks, with focused tests and integration review.

**Goal:** Supply a tested local-model launcher, SageMaker forwarding image, AWS web demo and reproducible deployment commands for the approved hybrid architecture.

**Architecture:** Browser → ECS Express Mode web app → SageMaker Serverless forwarding container → authenticated HTTPS tunnel → native local Qwen. The web app runs a bounded, fixed comparison and returns asynchronous run status. Completed reports persist in private S3; live runtime sessions remain disposable.

**Tech Stack:** Existing Python 3.11+ / Strands 1.56.0 / boto3, Python standard-library HTTP servers, vanilla HTML/CSS/JS, native llama.cpp b10433, Docker, CloudFormation, ECR, Secrets Manager, S3, SageMaker Serverless and ECS Express Mode.

**Spec:** `docs/HYBRID_DEPLOYMENT_PREFLIGHT.md`, accepted by the user's request to proceed with deployment.

## Global constraints

- Work in the user's existing workspace: the implementation depends on its extensive uncommitted prior work. Do not stash, stage, commit or overwrite unrelated files.
- Region ap-south-1; one 1-vCPU / 2-GiB web task, one 1-GiB serverless gateway with maximum concurrency one. Cloud costs must be stated.
- Never print API keys, use AWS keys in browser code, bake secrets into images or follow upstream redirects carrying authorization.
- Local model only listens on loopback; authenticated requests, 64 output-token cap, non-thinking mode. Timeout/offline errors are reported honestly.
- No arbitrary user code, shell commands or workspace access through the web UI. Use predefined demo fixtures and separate temporary repositories.
- Measured token comparison is a fixed example, not proof of general savings. Report failures and unavailable usage; do not turn them into successful or zero-token results.
- Public bundle export is not full session recovery. S3 persists completed reports; backend replacement may interrupt a run.
- The user asked for step-by-step deployment. Prepare/test artifacts locally; present explicit cloud commands and costs before operator-run cloud creation.

## Review focus

1. Offline/slow PC, expired tunnel, redirects and non-JSON upstream errors must fail within the proxy deadline without credential leakage.
2. Public HTTP input must be bounded; authorization, concurrency and report IDs cannot be bypassed by malformed requests.
3. Provider token counts, cache inclusion and failed task quality must remain distinguishable from synthetic fixture sizes.
4. Missing quota/VPC, partial cloud setup, OCI manifests and Docker credential-helper failures must produce actionable recovery instructions.
5. Restarted web workers cannot claim to resume a runtime from a report or public-state bundle.

## Task 1: protected native launcher and gateway

Owner: gateway implementer. Files: `deploy/local_qwen.py`, `deploy/hybrid_gateway/`, `tests/test_hybrid_gateway.py`, `tests/test_local_qwen.py`.

- [x] Write and run failing tests for key preservation, loopback binding, bounded requests, token cap, non-thinking mode, timeouts, rejected redirects and redacted errors.
- [x] Implement `python -m deploy.local_qwen prepare` creating `build/hybrid/private/secret.json` with keys `upstream_api_key` and `demo_access_code`, plus `upstream.key`; preserve existing valid keys.
- [x] Implement `python -m deploy.local_qwen serve` using existing verified native executable and model, port18081, API-key file, six threads and one slot; stop children on interruption.
- [x] Implement gateway `GET /ping`, `POST /invocations`; environment `UPSTREAM_URL` full HTTPS chat URL, `UPSTREAM_SECRET_ARN`, `AWS_REGION`. Secret JSON key `upstream_api_key`. Allow local test override via explicit key file and loopback URL only. Maximum64 output tokens, one concurrent request, total proxy deadline below60s, no redirects.
- [x] Add small Linux amd64 Dockerfile using isolated build context and pinned direct dependencies; run focused tests.

## Task 2: fixed real-model web demo

Owner: web implementer. Files: `statetree/web/`, `deploy/hybrid_web/Dockerfile`, `tests/test_hybrid_web.py`, `tests/test_hybrid_demo.py`.

- [x] Write and run failing tests for authorized run start, bounded payloads, one live run, terminal error status, report persistence, safe static paths and honest usage calculations.
- [x] Implement `python -m statetree.web.app`; port8080 and `GET /ping`, `/`, static assets; authenticated `POST /api/runs`, `GET /api/runs/<uuid>` with random capability IDs. UI uses `X-Demo-Key` for starts.
- [x] Implement fixed baseline versus StateTree commit-context demo using two real model calls and the same seeded project history/question. Clearly label seeded fixture history and caller-authored notes. Assert task quality and expose measured usage. No free-form executable tools.
- [x] Configuration: `SAGEMAKER_ENDPOINT`, `AWS_REGION`, `DEMO_SECRET_ARN` (JSON key `demo_access_code`), `REPORT_BUCKET`, `PORT`. Local-only tests may use `LOCAL_MODEL_URL` and `LOCAL_SECRET_FILE` at a loopback model origin.
- [x] Persist completed run reports to `reports/<uuid>.json` in private S3; return interrupted/unknown status appropriately after worker replacement. No full-session persistence claims.
- [x] Build UI with visible baseline/context comparison, commit note, outputs, tokens, architecture and laptop dependency. Escape dynamic text; never embed cloud credentials.
- [x] Add web Dockerfile with Git and the existing package; do not copy build/model/secret directories into context.

## Task 3: deployment helper and operator guide

Owner: root. Files: `deploy/hybrid.py`, `.dockerignore`, `docs/HYBRID_DEPLOYMENT.md`, `tests/test_hybrid_deploy.py`.

- [x] Write failing offline tests covering CloudFormation resource shapes, resource naming, absent credentials in templates, serverless settings, IAM scoping and manifest conversion.
- [x] Implement explicit `prepare`/`publish`/`plan`/`deploy`/`status`/`delete` stages. Cloud mutations require `--apply`; plan works without credentials. Keep source image publication separate from billable hosting creation.
- [x] ECR publication uses temporary process-only Docker auth, checks subprocess exit codes and converts supported OCI gzip manifests to Docker V2 metadata when required. Never change the user's Docker config.
- [x] Create CloudFormation-managed hosting resources with Secrets Manager ARN references, narrowly scoped task/model permissions, S3 public access blocked and one-task scaling. Retain completed reports and declare retained artifacts during cleanup.
- [x] Validate quota headroom and a suitable default VPC before creating hosting resources. Detect stack collisions/failed states instead of creating repeated names.
- [x] Document PowerShell steps: prepare secrets, start local model, start authenticated-through-origin HTTPS tunnel, publish images/secret, preview, deploy, open URL, stop/delete; show where billing begins.

## Task 4: integration and review

- [x] Run focused and affected existing tests; inspect every failure.
- [x] Exercise local authenticated model → proxy → actual web demo and verify real usage/results. Stop temporary test processes.
- [x] Build/inspect both Linux amd64 containers where Docker is available and validate offline AWS request schemas/template.
- [x] Independent review of authentication, secret flow, persistence scope and public claims; fix material findings.
- [x] Update verification and provide concise next commands linking the complete guide. Do not claim AWS deployment or Ship It qualification without evidence.

## Execution ledger

- Ruling: proceed with the accepted preflight architecture and user-requested deployment preparation; use parallel workers for disjoint files and preserve the existing workspace. This avoids moving the many uncommitted dependencies into a different checkout.
- Ruling: use a temporary HTTPS tunnel for the operator-run demo, with authentication enforced by native llama.cpp. A stable tunnel can be substituted; the temporary URL and laptop availability are documented limitations.

- Completed: 56 focused hybrid tests pass; both amd64 images built and passed container checks; AWS read-only quota/network preflight and template validation passed. Real v2 local API integration returned correct answers at 950 versus 739 input tokens. Initial v1 increase is preserved in VERIFICATION.md. All test servers/containers stopped.
- Validation boundary: public AWS hosting, ECR publication and HTTPS tunneling remain operator-run steps in HYBRID_DEPLOYMENT.md, not completed deployments. Browser visual check could not initialize; HTTP/static and installed-wheel checks passed.
- Ruling: use an ephemeral private Docker config plus explicit local daemon endpoint for ECR push; DOCKER_AUTH_CONFIG is not a supported standard Docker CLI authentication mechanism. No global Docker settings changed.
