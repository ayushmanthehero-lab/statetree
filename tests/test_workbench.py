"""Real loopback HTTP tests; no provider, framework or cloud calls."""
import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest

from statetree.project import Project
from tests.test_project import repo_at


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('statetree.web.workbench'), 'Local project workbench is missing')
        from statetree.web.workbench import make_server
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = repo_at(Path(self.temp.name) / 'repo')
        self.project = Project.init(self.repo, goal='Workbench project')
        self.token = 'test-token-that-is-long-and-local-only'
        self.server = make_server(self.repo, port=0, token=self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None, *, auth=True, extra=None, raw=False):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        headers = {'Content-Type': 'application/json'}
        if auth:
            headers['Authorization'] = 'Bearer ' + self.token
        if extra:
            headers.update(extra)
        connection.request(method, path, body if raw else (json.dumps(body) if body is not None else None), headers=headers)
        response = connection.getresponse()
        payload = response.read()
        status = response.status
        response_headers = dict(response.getheaders())
        connection.close()
        if 'application/json' in response_headers.get('Content-Type', ''):
            payload = json.loads(payload)
        return status, payload, response_headers

    def test_assets_are_public_but_project_data_requires_token(self):
        status, html, headers = self.request('GET', '/', auth=False)
        self.assertEqual(status, 200)
        self.assertIn(b'StateTree', html)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertEqual(self.request('GET', '/api/status', auth=False)[0], 401)
        status, value, _ = self.request('GET', '/api/status')
        self.assertEqual(status, 200)
        self.assertEqual(value['goal'], 'Workbench project')

    def test_dns_rebinding_and_cross_origin_requests_are_rejected(self):
        self.assertEqual(self.request('GET', '/api/status', extra={'Host': 'attacker.example'})[0], 403)
        self.assertEqual(self.request('POST', '/api/checkpoint', {}, extra={'Origin': 'https://attacker.example'})[0], 403)

    def test_mutations_require_current_head_and_restore_confirmation(self):
        head = self.project.status()['head']
        self.assertEqual(self.request('POST', '/api/checkpoint', {'message': 'missing head'})[0], 409)
        status, saved, _ = self.request('POST', '/api/checkpoint', {'message': 'Dashboard checkpoint', 'expected_head': head})
        self.assertEqual(status, 200)
        self.assertEqual(self.request('POST', '/api/checkpoint', {'message': 'stale', 'expected_head': head})[0], 409)
        (self.repo / 'example.txt').write_text('modified')
        body = {'checkpoint_id': head, 'expected_head': saved['id']}
        self.assertEqual(self.request('POST', '/api/restore', body)[0], 400)
        self.assertEqual((self.repo / 'example.txt').read_text(), 'modified')
        status, restored, _ = self.request('POST', '/api/restore', {**body, 'confirm': True})
        self.assertEqual(status, 200)
        self.assertEqual(restored['head'], head)
        self.assertEqual((self.repo / 'example.txt').read_text(), 'initial')

    def test_memory_context_and_portable_export_are_wired_to_real_project(self):
        head = self.project.status()['head']
        body = {'key': 'retention', 'value': 7, 'evidence': ['caller:decision'], 'expected_head': head}
        self.assertEqual(self.request('POST', '/api/remember', body)[0], 200)
        self.assertEqual(self.request('GET', '/api/facts')[1]['active']['retention']['value'], 7)
        self.assertEqual(self.request('GET', '/api/usage')[1]['requests'], 0)
        self.assertEqual(self.request('GET', '/api/export')[1]['format'], 'statetree.bundle')
        status, context, _ = self.request('POST', '/api/context', {'query': 'retention', 'new_task': True})
        self.assertEqual(status, 200)
        self.assertEqual(context['counting_method'], 'utf8_bytes_estimate')

    def test_strict_json_unknown_fields_and_unavailable_arbitrary_commands(self):
        self.assertEqual(self.request('POST', '/api/checkpoint', b'{"message":"x","message":"y"}', raw=True)[0], 400)
        head = self.project.status()['head']
        self.assertEqual(self.request('POST', '/api/checkpoint', {'message': 'x', 'expected_head': head, 'command': ['rm', '-rf', '/']})[0], 400)
        self.assertEqual(self.request('POST', '/api/shell', {'command': 'echo nope'})[0], 404)
        self.assertEqual(self.request('POST', '/api/model', {'url': 'https://example.com'})[0], 404)

    def test_health_has_no_project_data_and_paths_cannot_escape_static_directory(self):
        self.assertEqual(self.request('GET', '/healthz', auth=False)[1], {'ok': True})
        self.assertEqual(self.request('GET', '/../project.json', auth=False)[0], 404)
        self.assertEqual(self.request('GET', '/api/../project.json')[0], 404)

    def test_oversized_body_is_rejected_before_reading(self):
        self.assertEqual(self.request('POST', '/api/checkpoint', b'{}', raw=True,
                                     extra={'Content-Length': '20000000'})[0], 413)

    def test_no_cross_origin_cors_access_or_mutations_with_get(self):
        self.assertEqual(self.request('GET', '/api/restore')[0], 404)
        _, _, headers = self.request('GET', '/api/status')
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertEqual(headers['Cache-Control'], 'no-store')
