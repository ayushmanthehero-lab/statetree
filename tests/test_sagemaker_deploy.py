import contextlib
import importlib
import io
import json
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.stub import Stubber


ROOT = Path(__file__).resolve().parents[1]
BASE = ["--region", "us-west-2", "--endpoint-name", "statetree-test"]
ROLE = "arn:aws:iam::123456789012:role/StateTreeExecution"
ARN = "arn:aws:sagemaker:us-west-2:123456789012:"
TAGS = [{"Key": "StateTreeManaged", "Value": "true"}]
NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


class SageMakerDeploymentTests(unittest.TestCase):
    def module(self):
        return importlib.import_module("deploy.sagemaker")

    def client(self):
        return boto3.client("sagemaker", region_name="us-west-2",
                            aws_access_key_id="testing", aws_secret_access_key="testing")

    def run_main(self, args, client=None):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = self.module().main(args, client=client)
        return code, output.getvalue(), errors.getvalue()

    def missing(self, stub, method, key, name):
        stub.add_client_error(method, service_error_code="ValidationException",
                              service_message=f"Could not find {name}",
                              expected_params={key: name})

    def stub_missing_resources(self, stub):
        self.missing(stub, "describe_endpoint", "EndpointName", "statetree-test")
        self.missing(stub, "describe_endpoint_config", "EndpointConfigName", "statetree-test-config")
        self.missing(stub, "describe_model", "ModelName", "statetree-test-model")

    def endpoint(self):
        return {"EndpointName": "statetree-test", "EndpointArn": ARN + "endpoint/statetree-test",
                "EndpointConfigName": "statetree-test-config", "EndpointStatus": "InService",
                "CreationTime": NOW, "LastModifiedTime": NOW}

    def config(self):
        return {"EndpointConfigName": "statetree-test-config", "EndpointConfigArn": ARN + "endpoint-config/statetree-test-config",
                "CreationTime": NOW, "ProductionVariants": [{"VariantName": "AllTraffic", "ModelName": "statetree-test-model",
                 "InitialInstanceCount": 1, "InstanceType": "ml.g6.2xlarge"}]}

    def model(self):
        return {"ModelName": "statetree-test-model", "ModelArn": ARN + "model/statetree-test-model",
                "ExecutionRoleArn": ROLE, "CreationTime": NOW}

    def existing_resources(self, stub, config=None, endpoint=None):
        stub.add_response("describe_endpoint", endpoint or self.endpoint(), {"EndpointName": "statetree-test"})
        stub.add_response("describe_endpoint_config", config or self.config(), {"EndpointConfigName": "statetree-test-config"})
        stub.add_response("describe_model", self.model(), {"ModelName": "statetree-test-model"})
        for arn in ("endpoint/statetree-test", "endpoint-config/statetree-test-config", "model/statetree-test-model"):
            stub.add_response("list_tags", {"Tags": TAGS}, {"ResourceArn": ARN + arn})

    def endpoint_summary(self, endpoint):
        return {key: endpoint[key] for key in ("EndpointName", "EndpointArn", "CreationTime", "LastModifiedTime", "EndpointStatus")}

    def config_summary(self, config):
        return {key: config[key] for key in ("EndpointConfigName", "EndpointConfigArn", "CreationTime")}

    def empty_reference_scan(self, stub):
        stub.add_response("list_endpoints", {"Endpoints": []}, {})
        stub.add_response("list_endpoint_configs", {"EndpointConfigs": []}, {})

    def test_plan_runs_without_importing_aws_sdk_or_resolving_credentials(self):
        # -S disables all site packages, including boto3; planning must stay offline.
        result = subprocess.run([sys.executable, "-S", "-m", "deploy.sagemaker", "plan", *BASE,
                                 "--execution-role-arn", ROLE], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["create_model"]["PrimaryContainer"]["Environment"]["SM_VLLM_MODEL"],
                         "Qwen/Qwen3.5-4B")

    def test_profiles_produce_matching_model_parser_and_gpu_sharding(self):
        for profile, model, parser, instance, tp in [
            ("qwen35-4b", "Qwen/Qwen3.5-4B", "qwen3_coder", "ml.g6.2xlarge", "1"),
            ("qwen38-27b", "Qwen/Qwen3.8-27B", "qwen3_xml", "ml.g6.12xlarge", "4"),
        ]:
            code, out, err = self.run_main(["plan", *BASE, "--profile", profile, "--execution-role-arn", ROLE])
            self.assertEqual(code, 0, err)
            plan = json.loads(out)
            env = plan["create_model"]["PrimaryContainer"]["Environment"]
            self.assertEqual((env["SM_VLLM_MODEL"], env["SM_VLLM_SERVED_MODEL_NAME"],
                              env["SM_VLLM_TOOL_CALL_PARSER"], env["SM_VLLM_TENSOR_PARALLEL_SIZE"]),
                             (model, model, parser, tp))
            self.assertEqual(env["SM_VLLM_ENABLE_AUTO_TOOL_CHOICE"], "true")
            self.assertEqual(env["SM_VLLM_MAX_MODEL_LEN"], "8192")
            variant = plan["create_endpoint_config"]["ProductionVariants"][0]
            self.assertEqual(variant["InstanceType"], instance)
            self.assertEqual(variant["InferenceAmiVersion"], "al2023-ami-sagemaker-inference-gpu-4-1")

    def test_mutating_commands_require_apply_before_creating_a_client(self):
        for command in ("deploy", "delete"):
            with self.assertRaises(SystemExit) as error, contextlib.redirect_stderr(io.StringIO()):
                self.module().main([command, *BASE, "--execution-role-arn", ROLE])
            self.assertEqual(error.exception.code, 2)

    def test_invalid_names_and_sizes_are_rejected_offline(self):
        for extra in (["--endpoint-name", "bad/name"], ["--endpoint-name", "a" * 57],
                      ["--max-model-len", "0"], ["--tensor-parallel-size", "-1"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.module().main(["plan", *BASE, "--execution-role-arn", ROLE, *extra])

    def test_deploy_uses_valid_aws_requests_and_retains_partial_resources(self):
        code, out, _ = self.run_main(["plan", *BASE, "--execution-role-arn", ROLE])
        plan = json.loads(out)
        client = self.client()
        with Stubber(client) as stub:
            self.stub_missing_resources(stub)
            stub.add_response("create_model", {"ModelArn": ARN + "model/statetree-test-model"}, plan["create_model"])
            stub.add_client_error("create_endpoint_config", service_error_code="ResourceLimitExceeded",
                                  service_message="Instance quota exceeded", expected_params=plan["create_endpoint_config"])
            code, _, err = self.run_main(["deploy", *BASE, "--execution-role-arn", ROLE, "--apply"], client)
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(err)["retained_resources"], ["statetree-test-model"])
            stub.assert_no_pending_responses()

    def test_name_collision_prevents_creation(self):
        client = self.client()
        with Stubber(client) as stub:
            stub.add_response("describe_endpoint", self.endpoint(), {"EndpointName": "statetree-test"})
            self.missing(stub, "describe_endpoint_config", "EndpointConfigName", "statetree-test-config")
            self.missing(stub, "describe_model", "ModelName", "statetree-test-model")
            code, _, err = self.run_main(["deploy", *BASE, "--execution-role-arn", ROLE, "--apply"], client)
            self.assertEqual(code, 1)
            self.assertIn("already exists", err)
            stub.assert_no_pending_responses()

    def test_deploy_success_creates_model_config_and_endpoint_with_tags(self):
        _, out, _ = self.run_main(["plan", *BASE, "--execution-role-arn", ROLE])
        plan = json.loads(out)
        client = self.client()
        with Stubber(client) as stub:
            self.stub_missing_resources(stub)
            for method, field, arn in [
                ("create_model", "ModelArn", "model/statetree-test-model"),
                ("create_endpoint_config", "EndpointConfigArn", "endpoint-config/statetree-test-config"),
                ("create_endpoint", "EndpointArn", "endpoint/statetree-test"),
            ]:
                self.assertEqual(plan[method]["Tags"], TAGS)
                stub.add_response(method, {field: ARN + arn}, plan[method])
            code, out, err = self.run_main(["deploy", *BASE, "--execution-role-arn", ROLE, "--apply"], client)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["status"], "Creating")
            stub.assert_no_pending_responses()

    def test_status_reports_missing_resources_without_creation(self):
        client = self.client()
        with Stubber(client) as stub:
            self.stub_missing_resources(stub)
            code, out, err = self.run_main(["status", *BASE], client)
            self.assertEqual(code, 0, err)
            self.assertIsNone(json.loads(out)["endpoint"])
            stub.assert_no_pending_responses()

    def test_delete_refuses_an_untagged_endpoint(self):
        client = self.client()
        with Stubber(client) as stub:
            stub.add_response("describe_endpoint", self.endpoint(), {"EndpointName": "statetree-test"})
            self.missing(stub, "describe_endpoint_config", "EndpointConfigName", "statetree-test-config")
            self.missing(stub, "describe_model", "ModelName", "statetree-test-model")
            stub.add_response("list_tags", {"Tags": []}, {"ResourceArn": ARN + "endpoint/statetree-test"})
            code, _, err = self.run_main(["delete", *BASE, "--apply"], client)
            self.assertEqual(code, 1)
            self.assertIn("StateTreeManaged", err)
            stub.assert_no_pending_responses()

    def test_delete_refuses_tagged_configuration_pointing_at_another_model(self):
        client = self.client()
        config = self.config()
        config["ProductionVariants"][0]["ModelName"] = "someone-elses-model"
        with Stubber(client) as stub:
            self.existing_resources(stub, config)
            code, _, err = self.run_main(["delete", *BASE, "--apply"], client)
            self.assertEqual(code, 1)
            self.assertIn("different models", err)
            stub.assert_no_pending_responses()

    def test_delete_waits_for_endpoint_removal_before_deleting_config_and_model(self):
        client = self.client()
        with Stubber(client) as stub:
            self.existing_resources(stub)
            self.empty_reference_scan(stub)
            stub.add_response("delete_endpoint", {}, {"EndpointName": "statetree-test"})
            self.missing(stub, "describe_endpoint", "EndpointName", "statetree-test")
            stub.add_response("delete_endpoint_config", {}, {"EndpointConfigName": "statetree-test-config"})
            stub.add_response("delete_model", {}, {"ModelName": "statetree-test-model"})
            code, out, err = self.run_main(["delete", *BASE, "--apply"], client)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["deleted"]["endpoint"], "statetree-test")
            stub.assert_no_pending_responses()

    def test_delete_refuses_transitional_endpoint_statuses_before_mutation(self):
        for status in ("Creating", "Updating", "SystemUpdating", "RollingBack", "Deleting", "UpdateRollbackFailed"):
            with self.subTest(status=status):
                client = self.client()
                endpoint = self.endpoint()
                endpoint["EndpointStatus"] = status
                with Stubber(client) as stub:
                    self.existing_resources(stub, endpoint=endpoint)
                    code, _, err = self.run_main(["delete", *BASE, "--apply"], client)
                    self.assertEqual(code, 1)
                    self.assertIn("endpoint status", err)
                    stub.assert_no_pending_responses()

    def test_delete_refuses_pending_deployment_even_if_endpoint_is_in_service(self):
        endpoint = self.endpoint()
        endpoint["PendingDeploymentSummary"] = {"EndpointConfigName": "replacement-config"}
        client = self.client()
        with Stubber(client) as stub:
            self.existing_resources(stub, endpoint=endpoint)
            code, _, err = self.run_main(["delete", *BASE, "--apply"], client)
            self.assertEqual(code, 1)
            self.assertIn("pending deployment", err)
            stub.assert_no_pending_responses()

    def test_delete_refuses_shadow_variants_in_owned_configuration(self):
        config = self.config()
        config["ShadowProductionVariants"] = [{"VariantName": "Shadow", "ModelName": "another-model",
                                               "InstanceType": "ml.g6.2xlarge", "InitialInstanceCount": 1}]
        client = self.client()
        with Stubber(client) as stub:
            self.existing_resources(stub, config=config)
            code, _, err = self.run_main(["delete", *BASE, "--apply"], client)
            self.assertEqual(code, 1)
            self.assertIn("shadow variants", err)
            stub.assert_no_pending_responses()

    def test_delete_refuses_paginated_current_or_pending_endpoint_references(self):
        for pending in (False, True):
            with self.subTest(pending=pending):
                other = self.endpoint()
                other.update(EndpointName="other-endpoint", EndpointArn=ARN + "endpoint/other-endpoint")
                if pending:
                    other["EndpointConfigName"] = "other-config"
                    other["PendingDeploymentSummary"] = {"EndpointConfigName": "statetree-test-config"}
                client = self.client()
                with Stubber(client) as stub:
                    self.existing_resources(stub)
                    stub.add_response("list_endpoints", {"Endpoints": [self.endpoint_summary(self.endpoint())], "NextToken": "next"}, {})
                    stub.add_response("list_endpoints", {"Endpoints": [self.endpoint_summary(other)]}, {"NextToken": "next"})
                    stub.add_response("describe_endpoint", other, {"EndpointName": "other-endpoint"})
                    code, _, err = self.run_main(["delete", *BASE, "--apply"], client)
                    self.assertEqual(code, 1)
                    self.assertIn("other-endpoint", err)
                    self.assertIn("references", err)
                    stub.assert_no_pending_responses()

    def test_delete_refuses_paginated_model_references_in_other_configs_including_shadow(self):
        for shadow in (False, True):
            with self.subTest(shadow=shadow):
                other = self.config()
                other.update(EndpointConfigName="other-config", EndpointConfigArn=ARN + "endpoint-config/other-config")
                if shadow:
                    other["ShadowProductionVariants"] = other["ProductionVariants"]
                    other["ProductionVariants"] = [{"VariantName": "Production", "ModelName": "other-model"}]
                client = self.client()
                with Stubber(client) as stub:
                    self.existing_resources(stub)
                    stub.add_response("list_endpoints", {"Endpoints": []}, {})
                    stub.add_response("list_endpoint_configs", {"EndpointConfigs": [self.config_summary(self.config())], "NextToken": "next"}, {})
                    stub.add_response("list_endpoint_configs", {"EndpointConfigs": [self.config_summary(other)]}, {"NextToken": "next"})
                    stub.add_response("describe_endpoint_config", other, {"EndpointConfigName": "other-config"})
                    code, _, err = self.run_main(["delete", *BASE, "--apply"], client)
                    self.assertEqual(code, 1)
                    self.assertIn("other-config", err)
                    self.assertIn("references", err)
                    stub.assert_no_pending_responses()

    def test_delete_preserves_model_when_reference_listing_is_denied(self):
        client = self.client()
        with Stubber(client) as stub:
            self.existing_resources(stub)
            stub.add_client_error("list_endpoints", service_error_code="AccessDeniedException", service_message="denied", expected_params={})
            code, _, err = self.run_main(["delete", *BASE, "--apply"], client)
            self.assertEqual(code, 1)
            self.assertIn("AccessDenied", err)
            self.assertEqual(len(json.loads(err)["retained_resources"]), 3)
            stub.assert_no_pending_responses()

    def test_partial_deployment_model_can_be_deleted_when_not_referenced(self):
        client = self.client()
        with Stubber(client) as stub:
            self.missing(stub, "describe_endpoint", "EndpointName", "statetree-test")
            self.missing(stub, "describe_endpoint_config", "EndpointConfigName", "statetree-test-config")
            stub.add_response("describe_model", self.model(), {"ModelName": "statetree-test-model"})
            stub.add_response("list_tags", {"Tags": TAGS}, {"ResourceArn": ARN + "model/statetree-test-model"})
            self.empty_reference_scan(stub)
            stub.add_response("delete_model", {}, {"ModelName": "statetree-test-model"})
            code, _, err = self.run_main(["delete", *BASE, "--apply"], client)
            self.assertEqual(code, 0, err)
            stub.assert_no_pending_responses()

    def test_access_denied_is_never_treated_as_resource_absence(self):
        client = self.client()
        with Stubber(client) as stub:
            stub.add_client_error("describe_endpoint", service_error_code="AccessDeniedException",
                                  service_message="Not authorized", expected_params={"EndpointName": "statetree-test"})
            code, _, err = self.run_main(["deploy", *BASE, "--execution-role-arn", ROLE, "--apply"], client)
            self.assertEqual(code, 1)
            self.assertIn("AccessDenied", err)
            stub.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main()
