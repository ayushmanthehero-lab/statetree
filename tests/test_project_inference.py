"""Actual pinned SDK against deterministic loopback HTTP responses.

These are protocol tests, not evidence of model quality or live token savings.
No mock Strands module is injected. They skip when the actual SDK is absent.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest

from statetree.project import Project
from tests.test_project import repo_at


@unittest.skipUnless(importlib.util.find_spec('strands'), 'Real strands-agents SDK is not installed')
class RealSDKBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.calls = []
        self.report_usage = True
        owner = self
        class Reply(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                value = {'choices': [{'message': {'role': 'assistant', 'content': 'Retention is 7 days.'}, 'finish_reason': 'stop'}]}
                if owner.report_usage:
                    value['usage'] = {'prompt_tokens': 13, 'completion_tokens': 2, 'total_tokens': 15}
                raw = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Reply)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.project = Project.init(repo_at(Path(self.temp.name) / 'repo'), goal='Remember export retention', config={
            'total_token_limit': 15,
            'model': {'model_id': 'fixture-local-model',
                      'url': f'http://127.0.0.1:{self.server.server_port}/v1/chat/completions',
                      'max_tokens': 128, 'context_window_limit': 8192}})
        self.project.remember('retention', 7, evidence=['authored-http-fixture'])

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_real_agent_loop_saves_reply_and_observed_provider_usage(self):
        result = self.project.ask('What is the retention period?', new_task=True)
        self.assertIn('7 days', result['answer'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['provider_requests_this_turn'], 1)
        self.assertEqual(result['usage']['total_tokens'], 15)
        fresh = Project(self.project.repo)
        self.assertEqual(fresh.status()['head'], result['checkpoint_id'])
        self.assertEqual(fresh.agent.messages[-1]['role'], 'assistant')
        self.assertIn('7 days', fresh.history()['items'][0]['note']['summary'])

    def test_between_call_budget_prevents_another_model_request(self):
        self.project.ask('Retention?')
        with self.assertRaises(RuntimeError):
            Project(self.project.repo).ask('Repeat retention?')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(Project(self.project.repo).usage()['total_tokens'], 15)

    def test_missing_provider_usage_remains_unknown_and_stops_budgeted_followup(self):
        self.report_usage = False
        self.project.ask('Retention?')
        self.assertEqual(self.project.usage()['unknown_usage_requests'], 1)
        with self.assertRaises(RuntimeError):
            Project(self.project.repo).ask('Repeat?')
        self.assertEqual(len(self.calls), 1)
