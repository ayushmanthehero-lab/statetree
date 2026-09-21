import http.client
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from statetree.web.app import Application, make_server, Settings, S3Reports, LocalClient, MemoryReports


class Reports:
    def __init__(self):
        self.data = {}
    def put(self, identifier, report):
        self.data[identifier] = json.loads(json.dumps(report))
    def get(self, identifier):
        return self.data.get(identifier)


class HybridWebTests(unittest.TestCase):
    def setUp(self):
        self.release = threading.Event()
        self.reports = Reports()
        def run():
            self.release.wait(3)
            return {'comparison': {'both_tasks_passed': True}}
        self.app = Application('test-demo-code', run, self.reports)
        self.server = make_server(self.app, '127.0.0.1', 0)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
    def tearDown(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)
    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result
    def start(self):
        code, body, _ = self.request('POST', '/api/runs', '{}', {'X-Demo-Key': 'test-demo-code'})
        self.assertEqual(code, 202)
        return json.loads(body)['id']
    def test_start_requires_authentication_and_health_is_public(self):
        self.assertEqual(self.request('POST', '/api/runs', '{}')[0], 401)
        self.assertEqual(self.request('GET', '/ping')[0], 200)
        self.assertEqual(self.reports.data, {})
    def test_only_one_run_then_completed_report_survives_application_replacement(self):
        identifier = self.start()
        self.assertEqual(self.request('POST', '/api/runs', '{}', {'X-Demo-Key': 'test-demo-code'})[0], 409)
        self.release.set()
        for _ in range(100):
            value = self.app.get(identifier)
            if value['status'] == 'completed':
                break
            time.sleep(.01)
        self.assertEqual(value['status'], 'completed')
        self.assertEqual(value['persistence'], 'saved')
        replacement = Application('test-demo-code', lambda: {}, self.reports)
        self.assertEqual(replacement.get(identifier)['status'], 'completed')
        self.assertIsNone(replacement.get('b' * 32))
    def test_payload_static_paths_and_unknown_ids_are_bounded(self):
        headers = {'X-Demo-Key': 'test-demo-code'}
        self.assertEqual(self.request('POST', '/api/runs', '{"prompt":"run arbitrary code"}', headers)[0], 400)
        self.assertEqual(self.request('POST', '/api/runs', 'a' * 3000, headers)[0], 413)
        self.assertEqual(self.request('GET', '/static/../../pyproject.toml')[0], 404)
        self.assertEqual(self.request('GET', '/api/runs/not-a-run')[0], 404)
        status, body, response_headers = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn('Content-Security-Policy', response_headers)
        self.assertNotIn(b'test-demo-code', body)
    def test_background_failure_is_terminal_and_redacted(self):
        def fail():
            raise RuntimeError('secret-demo-code-must-never-leak')
        app = Application('test-demo-code', fail, self.reports)
        identifier = app.start()
        for _ in range(100):
            value = app.get(identifier)
            if value['status'] == 'error':
                break
            time.sleep(.01)
        self.assertEqual(value['status'], 'error')
        self.assertNotIn('secret-demo-code-must-never-leak', json.dumps(value))
    def test_storage_failure_keeps_visible_result_without_claiming_persistence(self):
        class BrokenReports(Reports):
            def put(self, identifier, report):
                raise RuntimeError('private bucket detail')
        app = Application('test-demo-code', lambda: {'comparison': {}}, BrokenReports())
        identifier = app.start()
        for _ in range(100):
            value = app.get(identifier)
            if value['status'] == 'completed':
                break
            time.sleep(.01)
        self.assertEqual(value['status'], 'completed')
        self.assertEqual(value['persistence'], 'failed')
        self.assertNotIn('private bucket detail', json.dumps(value))
    def test_local_memory_reports_are_labeled_ephemeral(self):
        app = Application('test-demo-code', lambda: {'comparison': {}}, MemoryReports())
        identifier = app.start()
        for _ in range(100):
            value = app.get(identifier)
            if value['status'] == 'completed':
                break
            time.sleep(.01)
        self.assertEqual(value['persistence'], 'ephemeral')
        self.assertIn('process exits', value['persistence_note'])
    def test_framing_and_non_ascii_authorization_fail_cleanly(self):
        code, _, _ = self.request('POST', '/api/runs', '{}', {'X-Demo-Key': '\u00e9' * 8})
        self.assertEqual(code, 401)
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        connection.putrequest('POST', '/api/runs')
        connection.putheader('X-Demo-Key', 'test-demo-code')
        connection.putheader('Content-Length', '2')
        connection.putheader('Content-Length', '2')
        connection.endheaders(b'{}')
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        response.read()
        connection.close()
    def test_nested_json_and_signed_content_length_return_controlled_errors(self):
        headers = {'X-Demo-Key': 'test-demo-code'}
        nested = '[' * 1000 + '0' + ']' * 1000
        self.assertEqual(self.request('POST', '/api/runs', nested, headers)[0], 400)
        headers['Content-Length'] = '+2'
        self.assertEqual(self.request('POST', '/api/runs', '{}', headers)[0], 400)
    def test_local_transport_does_not_forward_credentials_across_redirect(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.send_response(302)
                self.send_header('Location', 'http://example.com/steal')
                self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = LocalClient('http://127.0.0.1:' + str(server.server_port) + '/invocations', 'private-key')
            from urllib.error import HTTPError
            with self.assertRaises(HTTPError) as caught:
                client.invoke_endpoint(Body=b'{}')
            self.assertEqual(caught.exception.code, 302)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)
    def test_local_model_url_must_be_loopback_and_secret_is_not_in_settings_repr(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / 'secret.json'
            secret.write_text(json.dumps({'demo_access_code': 'private-demo', 'upstream_api_key': 'private-model'}))
            env = {'LOCAL_SECRET_FILE': str(secret), 'LOCAL_MODEL_URL': 'http://example.com/v1/chat/completions'}
            with self.assertRaises(ValueError):
                Settings.from_environ(env)
            env['LOCAL_MODEL_URL'] = 'http://127.0.0.1:18081/v1/chat/completions'
            settings = Settings.from_environ(env)
            self.assertEqual(settings.host, '127.0.0.1')
            self.assertNotIn('private-model', repr(settings))
            env['LOCAL_MODEL_URL'] = 'http://localhost:18081/v1/chat/completions'
            with self.assertRaises(ValueError):
                Settings.from_environ(env)
    def test_s3_report_uses_only_fixed_report_prefix(self):
        class Client:
            def put_object(self, **kwargs):
                self.written = kwargs
            def get_object(self, **kwargs):
                self.read = kwargs
                return {'Body': io.BytesIO(self.written['Body'])}
        client = Client()
        store = S3Reports('private-reports', client)
        store.put('a' * 32, {'status': 'completed'})
        self.assertEqual(client.written['Key'], 'reports/' + 'a' * 32 + '.json')
        self.assertEqual(store.get('a' * 32), {'status': 'completed'})
        with self.assertRaises(ValueError):
            store.get('../../secret')


if __name__ == '__main__':
    unittest.main()
