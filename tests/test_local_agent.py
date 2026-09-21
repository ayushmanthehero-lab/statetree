import io
import json
from pathlib import Path
import tempfile
import unittest


class LocalAgentTests(unittest.TestCase):
    def api(self):
        from deploy import local_agent
        return local_agent

    def test_chat_credentials_preserve_demo_key(self):
        from deploy.secrets import prepare
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = prepare(root)
            path = Path(original['secret_file'])
            before = json.loads(path.read_text())
            output = self.api().prepare_worker(root)
            after = json.loads(path.read_text())
            self.assertEqual(after['demo_access_code'], before['demo_access_code'])
            self.assertEqual(len(set(after.values())), 3)
            self.assertGreaterEqual(len(after['owner_access_code']), 32)
            self.assertGreaterEqual(len(after['worker_api_key']), 32)
            self.assertNotIn(after['owner_access_code'], json.dumps(output))
            self.assertNotIn(after['worker_api_key'], json.dumps(output))
            self.api().prepare_worker(root)
            self.assertEqual(json.loads(path.read_text()), after)

    def test_invalid_existing_worker_key_is_not_rotated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            info = self.api().prepare_worker(root)
            path = Path(info['secret_file'])
            values = json.loads(path.read_text())
            values['worker_api_key'] = 'bad'
            path.write_text(json.dumps(values))
            with self.assertRaises(ValueError):
                self.api().prepare_worker(root)
            self.assertEqual(json.loads(path.read_text())['worker_api_key'], 'bad')

    def test_worker_url_rejects_credentials_paths_and_nonlocal_http(self):
        api = self.api()
        for value in ('http://example.com', 'https://user:key@example.com',
                      'https://example.com/?secret=x', 'https://example.com/path',
                      'http://localhost:8080', 'file:///tmp/x'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                api.validate_web_url(value)
        self.assertEqual(api.validate_web_url('https://example.com/'), 'https://example.com')
        self.assertEqual(api.validate_web_url('http://127.0.0.1:8080'), 'http://127.0.0.1:8080')

    def test_project_identifier_is_stable_and_does_not_expose_path(self):
        with tempfile.TemporaryDirectory() as directory:
            value = self.api().project_identifier(Path(directory))
            self.assertRegex(value, r'^[a-f0-9]{32}$')
            self.assertEqual(value, self.api().project_identifier(Path(directory) / '.'))

    def test_relay_passes_only_active_task_worker_lease(self):
        observed = []
        class Client:
            worker_id = 'w' * 32
            def post(self, path, payload):
                observed.append((path, payload))
                return {'output': {'message': {'role': 'assistant', 'content': [{'text': 'ok'}]}}, 'stopReason': 'end_turn'}
        relay = self.api().RelayClient(Client(), 'a' * 32, 'lease-123')
        output = relay.converse(modelId='ignored', messages=[{'role': 'user', 'content': [{'text': 'hi'}]}])
        self.assertEqual(observed[0][0], '/api/worker/tasks/' + 'a' * 32 + '/inference')
        self.assertEqual(observed[0][1]['lease'], 'lease-123')
        self.assertEqual(observed[0][1]['worker_id'], 'w' * 32)
        self.assertNotIn('modelId', observed[0][1]['payload'])
        self.assertEqual(output['output']['message']['content'][0]['text'], 'ok')

    def test_outbox_survives_restart_and_acknowledges_only_sent_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.json'
            box = self.api().EventOutbox(path)
            first = box.add({'type': 'tool_result', 'text': 'edited', 'details': {'path': 'app.py'}})
            rebuilt = self.api().EventOutbox(path)
            self.assertEqual(rebuilt.pending()[0]['id'], first['id'])
            second = rebuilt.add({'type': 'checkpoint', 'text': 'saved'})
            rebuilt.acknowledge([first['id']])
            self.assertEqual([e['id'] for e in rebuilt.pending()], [second['id']])

    def test_model_limits_are_bounded(self):
        api = self.api()
        for value in (0, -1, 257, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                api.validate_output_tokens(value)
        self.assertEqual(api.validate_output_tokens(256), 256)

    def test_outbox_reconnect_batch_respects_api_event_count(self):
        with tempfile.TemporaryDirectory() as directory:
            box = self.api().EventOutbox(Path(directory) / 'events.json')
            for number in range(101):
                box.add({'type': 'status', 'text': str(number)})
            self.assertEqual(len(box.pending()), 100)

    def test_changing_checkpoint_data_follows_stable_system_and_tools(self):
        payload = {'messages': [
            {'role': 'system', 'content': 'Fixed instructions.\n\nStateTree context:\n{"goal":"fix"}\n\nStateTree historical commit notes: note'},
            {'role': 'user', 'content': 'Continue the task.'}],
            'tools': [{'type': 'function', 'function': {'name': 'read_file'}}]}
        result = self.api().cache_friendly_payload(payload)
        self.assertEqual(result['messages'][0]['content'], 'Fixed instructions.')
        self.assertEqual(result['messages'][1]['role'], 'user')
        self.assertIn('StateTree context:\n{"goal":"fix"}', result['messages'][1]['content'])
        self.assertIn('StateTree historical commit notes: note', result['messages'][1]['content'])
        self.assertEqual(result['messages'][2], payload['messages'][1])
        self.assertEqual(result['tools'], payload['tools'])
        self.assertIn('StateTree context:', payload['messages'][0]['content'])

    def test_plain_system_prompt_and_user_marker_are_not_reinterpreted(self):
        payload = {'messages': [{'role': 'system', 'content': 'Stable instructions'},
                                {'role': 'user', 'content': 'StateTree context:\nuser-provided text'}]}
        self.assertEqual(self.api().cache_friendly_payload(payload), payload)


if __name__ == '__main__':
    unittest.main()
