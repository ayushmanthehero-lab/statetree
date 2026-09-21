import json
import re
import unittest

from deploy.hybrid_template import build_template


class HybridTemplateTests(unittest.TestCase):
    def test_direct_bedrock_template_has_no_gateway_or_local_upstream(self):
        template = build_template()
        self.assertEqual(set(template['Parameters']), {'WebImage', 'SecretArn', 'BedrockModelId', 'SubnetIds'})
        serialized = json.dumps(template)
        self.assertNotIn('SageMaker', serialized)
        self.assertNotIn('UPSTREAM_URL', serialized)
        self.assertFalse(any(name.startswith('Gateway') for name in template['Resources']))
        statements = template['Resources']['WebTaskRole']['Properties']['Policies'][0]['PolicyDocument']['Statement']
        self.assertTrue(any(item['Action'] == 'bedrock:InvokeModel' for item in statements))
        environment = {item['Name']: item['Value'] for item in template['Resources']['WebService']['Properties']['PrimaryContainer']['Environment']}
        self.assertIn('BEDROCK_MODEL_ID', environment)
        self.assertNotIn('SAGEMAKER_ENDPOINT', environment)

    def test_parameters_require_an_immutable_image_and_model_identifier(self):
        parameters = build_template()['Parameters']
        image = '123456789012.dkr.ecr.ap-south-1.amazonaws.com/demo/web@sha256:' + 'a' * 64
        self.assertIsNotNone(re.fullmatch(parameters['WebImage']['AllowedPattern'], image))
        self.assertIsNone(re.fullmatch(parameters['WebImage']['AllowedPattern'], 'demo:latest'))
        self.assertIsNotNone(re.fullmatch(parameters['BedrockModelId']['AllowedPattern'], 'amazon.nova-lite-v1:0'))
        self.assertIsNone(re.fullmatch(parameters['BedrockModelId']['AllowedPattern'], 'model with spaces'))


if __name__ == '__main__':
    unittest.main()