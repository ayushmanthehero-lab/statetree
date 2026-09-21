import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

import boto3
from botocore.stub import Stubber

from deploy.sagemaker import main


ROOT = Path(__file__).resolve().parents[1]
IMAGE = '123456789012.dkr.ecr.ap-south-2.amazonaws.com/statetree-qwen-cpu:graviton2'
BASE = ['--profile', 'qwen35-4b-cpu', '--region', 'ap-south-2',
        '--endpoint-name', 'statetree-qwen35-cpu', '--execution-role-arn',
        'arn:aws:iam::123456789012:role/StateTreeExecution']


class SageMakerCPUDeploymentTests(unittest.TestCase):
    def plan(self, *extra):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(['plan', *BASE, '--image-uri', IMAGE, *extra])
        self.assertEqual(code, 0)
        return json.loads(output.getvalue())

    def test_cpu_plan_selects_graviton2_without_gpu_configuration(self):
        plan = self.plan()
        container = plan['create_model']['PrimaryContainer']
        self.assertTrue(plan['create_model']['EnableNetworkIsolation'])
        self.assertEqual(container['Image'], IMAGE)
        env = container['Environment']
        self.assertEqual(env['SM_LLAMA_CPP_ALIAS'], 'Qwen/Qwen3.5-4B')
        self.assertEqual(env['SM_LLAMA_CPP_CTX_SIZE'], '4096')
        self.assertEqual(env['SM_LLAMA_CPP_THREADS'], '4')
        self.assertEqual(env['SM_LLAMA_CPP_PARALLEL'], '1')
        self.assertEqual(env['SM_LLAMA_CPP_JINJA'], 'true')
        self.assertEqual(json.loads(env['SM_LLAMA_CPP_CHAT_TEMPLATE_KWARGS']), {'enable_thinking': False})
        self.assertFalse(any(key.startswith('SM_VLLM_') for key in env))
        variant = plan['create_endpoint_config']['ProductionVariants'][0]
        self.assertEqual(variant['InstanceType'], 'ml.m6g.xlarge')
        self.assertEqual(variant['InitialInstanceCount'], 1)
        self.assertNotIn('InferenceAmiVersion', variant)

    def test_cpu_plan_works_without_site_packages_or_credentials(self):
        result = subprocess.run([sys.executable, '-S', '-B', '-m', 'deploy.sagemaker',
                                 'plan', *BASE, '--image-uri', IMAGE], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['region'], 'ap-south-2')

    def test_cpu_profile_requires_explicit_compatible_image_before_any_client(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            main(['plan', *BASE])
        self.assertEqual(stopped.exception.code, 2)

    def test_cpu_profile_rejects_incompatible_instance_and_gpu_options(self):
        for extra in (['--instance-type', 'ml.g5.2xlarge'], ['--instance-type', 'ml.m6i.xlarge'],
                      ['--tensor-parallel-size', '1'], ['--inference-ami-version', 'gpu-ami']):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    self.plan(*extra)
                self.assertEqual(stopped.exception.code, 2)

    def test_gpu_profile_rejects_cpu_instance_before_deploy(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            main(['deploy', *BASE[2:], '--profile', 'qwen35-4b', '--instance-type', 'ml.m6g.xlarge', '--apply'],
                 client=object())  # No credential lookup or AWS request even if validation regresses.
        self.assertEqual(stopped.exception.code, 2)

    def test_cpu_context_override_is_used(self):
        env = self.plan('--max-model-len', '2048')['create_model']['PrimaryContainer']['Environment']
        self.assertEqual(env['SM_LLAMA_CPP_CTX_SIZE'], '2048')

    def test_cpu_deployment_submits_valid_requests_without_gpu_ami(self):
        plan = self.plan()
        client = boto3.client('sagemaker', region_name='ap-south-2',
                              aws_access_key_id='testing', aws_secret_access_key='testing')
        arn = 'arn:aws:sagemaker:ap-south-2:123456789012:'
        with Stubber(client) as stub:
            for kind, field in [('endpoint', 'EndpointName'), ('endpoint_config', 'EndpointConfigName'),
                                ('model', 'ModelName')]:
                stub.add_client_error('describe_' + kind, service_error_code='ValidationException',
                                      service_message='Could not find resource',
                                      expected_params={field: plan['resources'][kind]})
            for kind, field, prefix in [('model', 'ModelArn', 'model/'),
                                        ('endpoint_config', 'EndpointConfigArn', 'endpoint-config/'),
                                        ('endpoint', 'EndpointArn', 'endpoint/')]:
                stub.add_response('create_' + kind, {field: arn + prefix + plan['resources'][kind]},
                                  plan['create_' + kind])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(['deploy', *BASE, '--image-uri', IMAGE, '--apply'], client=client), 0)
            self.assertEqual(json.loads(output.getvalue())['status'], 'Creating')
            stub.assert_no_pending_responses()
