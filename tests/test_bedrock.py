import asyncio
import unittest

from statetree.models.bedrock import BedrockModel


class BedrockModelTests(unittest.TestCase):
    def test_converse_maps_tools_response_and_usage(self):
        class Client:
            request = None

            def converse(self, **request):
                self.request = request
                return {
                    'output': {'message': {'role': 'assistant', 'content': [
                        {'toolUse': {'toolUseId': 'call-1', 'name': 'read_file', 'input': {'path': 'README.md'}}},
                    ]}},
                    'stopReason': 'tool_use',
                    'usage': {'inputTokens': 12, 'outputTokens': 4, 'totalTokens': 16},
                }

        client = Client()
        model = BedrockModel('amazon.nova-lite-v1:0', client=client, max_tokens=64, temperature=0)

        async def collect():
            return [event async for event in model.stream(
                [{'role': 'user', 'content': [{'text': 'Inspect the project.'}]}],
                tool_specs=[{'name': 'read_file', 'description': 'Read one file',
                             'inputSchema': {'json': {'type': 'object'}}}],
                tool_choice={'auto': {}}, system_prompt='Be precise.')]

        events = asyncio.run(collect())
        self.assertEqual(client.request['modelId'], 'amazon.nova-lite-v1:0')
        self.assertEqual(client.request['system'], [{'text': 'Be precise.'}])
        self.assertEqual(client.request['inferenceConfig'], {'maxTokens': 64, 'temperature': 0})
        self.assertEqual(client.request['toolConfig']['tools'][0]['toolSpec']['name'], 'read_file')
        self.assertEqual(events[1]['contentBlockStart']['start']['toolUse']['name'], 'read_file')
        self.assertEqual(events[2]['contentBlockDelta']['delta']['toolUse']['input'], {'path': 'README.md'})
        self.assertEqual(events[-1]['metadata']['usage']['inputTokens'], 12)


if __name__ == '__main__':
    unittest.main()