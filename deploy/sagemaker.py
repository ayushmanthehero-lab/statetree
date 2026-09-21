"""Plan and explicitly manage dedicated Qwen SageMaker endpoints using boto3.

Defaults are deployment starting points, not validated capacity guarantees.
Sources checked 2026-09-20:
https://aws.github.io/deep-learning-containers/reference/available_images/
https://aws.github.io/deep-learning-containers/reference/region_availability/
https://aws.github.io/deep-learning-containers/vllm/configuration/
https://recipes.vllm.ai/Qwen/Qwen3.8-27B
https://huggingface.co/Qwen/Qwen3.5-4B
https://docs.aws.amazon.com/sagemaker/latest/APIReference/API_ProductionVariant.html
"""

import argparse
import json
import re
import sys


PROFILES = {
    "qwen35-4b": ("Qwen/Qwen3.5-4B", "qwen3_coder", "ml.g6.2xlarge", 1),
    "qwen38-27b": ("Qwen/Qwen3.8-27B", "qwen3_xml", "ml.g6.12xlarge", 4),
}
CPU_PROFILE = "qwen35-4b-cpu"
IMAGE_TAG = "0.29.0-gpu-py312-cu130-ubuntu24.04-sagemaker"
TAGS = [{"Key": "StateTreeManaged", "Value": "true"}]
STANDARD_REGIONS = {
    "us-east-1", "us-east-2", "us-west-1", "us-west-2", "ap-south-1",
    "ap-northeast-1", "ap-northeast-2", "ap-southeast-1", "ap-southeast-2",
    "ca-central-1", "eu-central-1", "eu-west-1", "eu-west-2", "eu-west-3",
    "eu-north-1", "sa-east-1",
}
REGISTRIES = dict.fromkeys(STANDARD_REGIONS, "763104351884") | {
    "af-south-1": "626614931356", "ap-east-1": "871362719292",
    "ap-south-2": "772153158452", "ap-southeast-3": "907027046896",
    "ap-southeast-4": "457447274322", "ap-southeast-5": "550225433462",
    "ap-southeast-6": "633930458069", "ap-southeast-7": "590183813437",
    "ap-northeast-3": "364406365360", "ap-east-2": "975050140332",
    "ca-west-1": "204538143572", "eu-south-1": "692866216735",
    "eu-south-2": "503227376785", "eu-central-2": "380420809688",
    "il-central-1": "780543022126", "mx-central-1": "637423239942",
    "me-south-1": "217643126080", "me-central-1": "914824155844",
}
RESOURCE_FIELDS = {"endpoint": "EndpointName", "endpoint_config": "EndpointConfigName", "model": "ModelName"}


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("plan", "deploy", "status", "delete"))
    parser.add_argument("--profile", choices=(*PROFILES, CPU_PROFILE), default="qwen35-4b", help="model configuration")
    parser.add_argument("--region", required=True)
    parser.add_argument("--endpoint-name", required=True)
    parser.add_argument("--execution-role-arn")
    parser.add_argument("--image-uri")
    parser.add_argument("--instance-type")
    parser.add_argument("--tensor-parallel-size", type=positive)
    parser.add_argument("--max-model-len", type=positive, help="CPU default 4096; GPU default 8192")
    parser.add_argument("--inference-ami-version", help="GPU profiles only")
    parser.add_argument("--aws-profile", help="AWS credential profile, separate from model --profile")
    parser.add_argument("--apply", action="store_true", help="required to deploy or delete billable resources")
    parser.add_argument("--wait", action="store_true", help="wait for deployment to become InService")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,54}[A-Za-z0-9])?", args.endpoint_name):
        parser.error("--endpoint-name must be 1-56 alphanumeric/hyphen characters, starting and ending alphanumeric")
    if args.command in ("plan", "deploy") and not args.execution_role_arn:
        parser.error("--execution-role-arn is required for plan/deploy")
    if args.command in ("deploy", "delete") and not args.apply:
        parser.error("--apply is required for deploy/delete")
    if args.command in ("plan", "deploy"):
        if args.profile == CPU_PROFILE:
            if not args.image_uri:
                parser.error("CPU profile requires --image-uri built from deploy/llama_cpp_cpu for Graviton2")
            if args.instance_type and args.instance_type != "ml.m6g.xlarge":
                parser.error("CPU profile currently supports only --instance-type ml.m6g.xlarge")
            if args.tensor_parallel_size is not None or args.inference_ami_version is not None:
                parser.error("--tensor-parallel-size and --inference-ami-version are GPU-only options")
        elif args.instance_type and not re.fullmatch(r"ml\.(?:g|p)[0-9][a-z0-9]*\.[a-z0-9]+", args.instance_type):
            parser.error("GPU profiles require a GPU instance; use --profile qwen35-4b-cpu for ml.m6g.xlarge")
    if args.command in ("plan", "deploy") and not args.image_uri and args.region not in REGISTRIES:
        parser.error("pass --image-uri for this region; no verified default registry is configured")
    return args


def resource_names(endpoint_name):
    return {"endpoint": endpoint_name, "endpoint_config": endpoint_name + "-config", "model": endpoint_name + "-model"}


def build_plan(args):
    names = resource_names(args.endpoint_name)
    cpu = args.profile == CPU_PROFILE
    if cpu:
        instance_type = "ml.m6g.xlarge"
        image = args.image_uri
        env = {
            "SM_LLAMA_CPP_ALIAS": "Qwen/Qwen3.5-4B",
            "SM_LLAMA_CPP_CTX_SIZE": str(args.max_model_len or 4096),
            "SM_LLAMA_CPP_THREADS": "4", "SM_LLAMA_CPP_THREADS_BATCH": "4",
            "SM_LLAMA_CPP_PARALLEL": "1", "SM_LLAMA_CPP_JINJA": "true",
            "SM_LLAMA_CPP_CHAT_TEMPLATE_KWARGS": json.dumps({"enable_thinking": False}),
        }
        note = "Experimental Graviton2 CPU profile; requires the custom image with bundled Q4_K_M weights. Live startup, latency and tool calls are unverified. Endpoints bill while provisioned."
    else:
        model_id, tool_parser, instance_type, tp = PROFILES[args.profile]
        image = args.image_uri or f"{REGISTRIES[args.region]}.dkr.ecr.{args.region}.amazonaws.com/vllm:{IMAGE_TAG}"
        env = {
            "SM_VLLM_MODEL": model_id, "SM_VLLM_SERVED_MODEL_NAME": model_id,
            "SM_VLLM_HOST": "0.0.0.0", "SM_VLLM_DTYPE": "bfloat16",
            "SM_VLLM_TENSOR_PARALLEL_SIZE": str(args.tensor_parallel_size or tp),
            "SM_VLLM_MAX_MODEL_LEN": str(args.max_model_len or 8192), "SM_VLLM_MAX_NUM_SEQS": "4",
            "SM_VLLM_GPU_MEMORY_UTILIZATION": "0.89", "SM_VLLM_REASONING_PARSER": "qwen3",
            "SM_VLLM_ENABLE_AUTO_TOOL_CHOICE": "true", "SM_VLLM_TOOL_CALL_PARSER": tool_parser,
        }
        note = "Unvalidated instance sizing; verify regional GPU quota, image availability and a tool-call smoke test. Endpoints bill while provisioned. HF download requires internet access."
    variant = {
        "VariantName": "AllTraffic", "ModelName": names["model"],
        "InstanceType": args.instance_type or instance_type, "InitialInstanceCount": 1,
        "ContainerStartupHealthCheckTimeoutInSeconds": 1800,
    }
    if not cpu:
        variant["InferenceAmiVersion"] = args.inference_ami_version or "al2023-ami-sagemaker-inference-gpu-4-1"
    return {
        "region": args.region, "resources": names,
        "note": note,
        "create_model": {"ModelName": names["model"], "ExecutionRoleArn": args.execution_role_arn,
                         "EnableNetworkIsolation": cpu, "Tags": TAGS,
                         "PrimaryContainer": {"Image": image, "Environment": env}},
        "create_endpoint_config": {"EndpointConfigName": names["endpoint_config"], "Tags": TAGS,
            "ProductionVariants": [variant]},
        "create_endpoint": {"EndpointName": names["endpoint"], "EndpointConfigName": names["endpoint_config"], "Tags": TAGS},
    }


def describe_resources(client, names):
    from botocore.exceptions import ClientError
    resources = {}
    for kind, field in RESOURCE_FIELDS.items():
        try:
            resources[kind] = getattr(client, "describe_" + kind)(**{field: names[kind]})
        except ClientError as error:
            detail = error.response["Error"]
            missing = detail["Code"] in ("ResourceNotFound", "ResourceNotFoundException") or (
                detail["Code"] == "ValidationException" and any(
                    text in detail.get("Message", "").lower()
                    for text in ("could not find", "does not exist", "was not found")))
            if not missing:
                raise
            resources[kind] = None
    return resources


def assert_exclusive_references(client, names):
    """Fail closed if another endpoint or configuration shares these artifacts.

    These are regional snapshots, not a lock: operators must avoid concurrent
    deployment changes during cleanup. Any incomplete scan aborts deletion.
    """
    for page in client.get_paginator("list_endpoints").paginate():
        for summary in page["Endpoints"]:
            name = summary["EndpointName"]
            if name == names["endpoint"]:
                continue
            endpoint = client.describe_endpoint(EndpointName=name)
            pending = endpoint.get("PendingDeploymentSummary") or {}
            if names["endpoint_config"] in (endpoint.get("EndpointConfigName"), pending.get("EndpointConfigName")):
                raise ValueError(f"Refusing deletion: endpoint {name} references {names['endpoint_config']}")
    for page in client.get_paginator("list_endpoint_configs").paginate():
        for summary in page["EndpointConfigs"]:
            name = summary["EndpointConfigName"]
            if name == names["endpoint_config"]:
                continue
            config = client.describe_endpoint_config(EndpointConfigName=name)
            variants = config.get("ProductionVariants", []) + config.get("ShadowProductionVariants", [])
            if any(variant.get("ModelName") == names["model"] for variant in variants):
                raise ValueError(f"Refusing deletion: configuration {name} references {names['model']}")


def delete_resources(client, names, resources, retained):
    # Validate the whole set before making any deletion; support partial failed deployments.
    for kind, resource in resources.items():
        if resource is None:
            continue
        arn = resource[RESOURCE_FIELDS[kind].replace("Name", "Arn")]
        tags, token = [], None
        while True:
            page = client.list_tags(ResourceArn=arn, **({"NextToken": token} if token else {}))
            tags.extend(page["Tags"])
            token = page.get("NextToken")
            if not token:
                break
        if {"Key": "StateTreeManaged", "Value": "true"} not in tags:
            raise ValueError(f"Refusing deletion: {names[kind]} lacks StateTreeManaged=true")
    endpoint, config = resources["endpoint"], resources["endpoint_config"]
    if endpoint and endpoint.get("EndpointStatus") not in ("InService", "Failed", "OutOfService"):
        raise ValueError(f"Refusing deletion: endpoint status {endpoint.get('EndpointStatus')} is not stable")
    if endpoint and endpoint.get("PendingDeploymentSummary") is not None:
        raise ValueError("Refusing deletion: endpoint has a pending deployment")
    if any(resource and resource.get("ShadowProductionVariants") for resource in (endpoint, config)):
        raise ValueError("Refusing deletion: unexpected shadow variants")
    if endpoint and endpoint["EndpointConfigName"] != names["endpoint_config"]:
        raise ValueError("Refusing deletion: endpoint uses a different configuration")
    if config and (len(config["ProductionVariants"]) != 1 or
                   config["ProductionVariants"][0].get("ModelName") != names["model"]):
        raise ValueError("Refusing deletion: configuration uses different models")
    if any(resources.values()):
        assert_exclusive_references(client, names)
    for kind, field in RESOURCE_FIELDS.items():
        if resources[kind] is not None:
            getattr(client, "delete_" + kind)(**{field: names[kind]})
            if kind == "endpoint":
                client.get_waiter("endpoint_deleted").wait(EndpointName=names[kind], WaiterConfig={"Delay": 15, "MaxAttempts": 120})
            retained.remove(names[kind])


def main(argv=None, *, client=None):
    args = arguments(argv)
    names = resource_names(args.endpoint_name)
    if args.command == "plan":
        print(json.dumps(build_plan(args), indent=2))
        return 0
    retained = []
    try:
        if client is None:
            import boto3
            client = boto3.Session(profile_name=args.aws_profile, region_name=args.region).client("sagemaker")
        resources = describe_resources(client, names)
        if args.command == "status":
            print(json.dumps(resources, indent=2, default=str))
            return 0
        if args.command == "delete":
            retained = [names[k] for k, value in resources.items() if value is not None]
            delete_resources(client, names, resources, retained)
            print(json.dumps({"deleted": names}))
            return 0
        existing = [names[k] for k, value in resources.items() if value is not None]
        if existing:
            raise ValueError(f"Resource already exists; choose a new endpoint name: {existing}")
        plan = build_plan(args)
        for kind in ("model", "endpoint_config", "endpoint"):
            getattr(client, "create_" + kind)(**plan["create_" + kind])
            retained.append(names[kind])
        if args.wait:
            client.get_waiter("endpoint_in_service").wait(EndpointName=names["endpoint"], WaiterConfig={"Delay": 30, "MaxAttempts": 120})
        print(json.dumps({"resources": names, "status": "InService" if args.wait else "Creating"}))
        return 0
    except Exception as error:
        print(json.dumps({"error": str(error), "resources": names, "retained_resources": retained,
                          "next_step": "Inspect status and CloudWatch logs. No automatic rollback was attempted."}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
