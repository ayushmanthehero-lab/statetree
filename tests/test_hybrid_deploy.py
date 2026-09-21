import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from deploy import hybrid


class HybridDeployTests(unittest.TestCase):
    def test_plan_uses_only_web_image_secret_and_bedrock_model(self):
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory)
            artifact = {'account': '123456789012', 'region': 'ap-south-1', 'stack': 'statetree-hybrid',
                        'web_image': '123456789012.dkr.ecr.ap-south-1.amazonaws.com/statetree-hybrid-web@sha256:' + 'a' * 64,
                        'secret_arn': 'arn:aws:secretsmanager:ap-south-1:123456789012:secret:statetree-hybrid/demo'}
            (build / 'statetree-hybrid-ap-south-1.json').write_text(json.dumps(artifact), encoding='utf-8')
            args = SimpleNamespace(stack='statetree-hybrid', region='ap-south-1', bedrock_model_id='amazon.nova-lite-v1:0')
            with patch.object(hybrid, 'BUILD', build):
                hybrid.plan(args)
            parameters = json.loads((build / 'statetree-hybrid-parameters.json').read_text())
            self.assertEqual({row['ParameterKey'] for row in parameters}, {'WebImage', 'SecretArn', 'BedrockModelId'})

    def test_cli_does_not_accept_an_upstream_url(self):
        with self.assertRaises(SystemExit):
            hybrid.main(['plan', '--upstream-url', 'https://old.example/v1/chat/completions'])


if __name__ == '__main__':
    unittest.main()