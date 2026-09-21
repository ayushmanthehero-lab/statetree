"""Private task chat and the preserved hybrid benchmark HTTP frontend."""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import threading
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4


MAX_BODY = 2048
MAX_REPORT = 1024 * 1024
STATIC = Path(__file__).with_name('static')
RUN_ID = re.compile(r'[0-9a-f]{32}\Z')


def _identifier(value):
    if not isinstance(value, str) or not RUN_ID.fullmatch(value):
        raise ValueError('Invalid report identifier')
    return value


def _loopback_url(value):
    parsed = urlsplit(value)
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = False
    if (parsed.scheme != 'http' or not loopback or parsed.username or parsed.password
            or parsed.fragment or parsed.query or parsed.path not in ('/v1/chat/completions', '/invocations')):
        raise ValueError('LOCAL_MODEL_URL must be a loopback HTTP chat-completion or invocation URL')
    parsed.port  # Reject malformed port before a worker starts.
    return value


@dataclass(frozen=True)
class Settings:
    region: str = 'ap-south-1'
    model_id: str = ''
    secret_arn: str = ''
    report_bucket: str = ''
    local_model_url: str = ''
    local_secret_file: str = ''
    port: int = 8080
    host: str = '0.0.0.0'

    @classmethod
    def from_environ(cls, env=None):
        env = os.environ if env is None else env
        local_url, local_file = env.get('LOCAL_MODEL_URL', ''), env.get('LOCAL_SECRET_FILE', '')
        port = int(env.get('PORT', '8080'))
        if not 1 <= port <= 65535:
            raise ValueError('PORT must be between 1 and 65535')
        if local_url or local_file:
            if not local_url or not local_file:
                raise ValueError('Local mode requires LOCAL_MODEL_URL and LOCAL_SECRET_FILE')
            return cls(local_model_url=_loopback_url(local_url), local_secret_file=local_file,
                       port=port, host='127.0.0.1')
        required = ('BEDROCK_MODEL_ID', 'DEMO_SECRET_ARN', 'REPORT_BUCKET')
        if any(not env.get(name) for name in required):
            raise ValueError('Cloud mode requires BEDROCK_MODEL_ID, DEMO_SECRET_ARN and REPORT_BUCKET')
        return cls(region=env.get('AWS_REGION', 'ap-south-1'), model_id=env['BEDROCK_MODEL_ID'],
                   secret_arn=env['DEMO_SECRET_ARN'], report_bucket=env['REPORT_BUCKET'], port=port)


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class LocalClient:
    """Private local integration transport; never accepts a browser-supplied URL."""
    def __init__(self, url, key, *, timeout=55):
        if type(timeout) not in (int, float) or not 1 <= timeout <= 3600:
            raise ValueError("Local model timeout must be between 1 and 3600 seconds")
        self.timeout = timeout
        self.url = _loopback_url(url)
        self._key = key
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def invoke_endpoint(self, **kwargs):
        request = Request(self.url, data=kwargs['Body'], headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self._key})
        with self._opener.open(request, timeout=self.timeout) as response:
            body = response.read(MAX_REPORT + 1)
        if len(body) > MAX_REPORT:
            raise ValueError('Model response exceeded the local transport limit')
        return {'Body': io.BytesIO(body)}


class MemoryReports:
    """Local smoke tests only. Reports disappear when the process exits."""
    persistence = 'ephemeral'

    def __init__(self):
        self._reports = OrderedDict()
        self._lock = threading.Lock()

    def put(self, identifier, report):
        with self._lock:
            self._reports[_identifier(identifier)] = json.loads(_json_bytes(report))
            while len(self._reports) > 100:
                self._reports.popitem(last=False)

    def get(self, identifier):
        with self._lock:
            return self._reports.get(_identifier(identifier))


class S3Reports:
    """Completed JSON reports only; this is not resumable agent session storage."""
    def __init__(self, bucket, client):
        self.bucket, self.client = bucket, client

    def put(self, identifier, report):
        body = _json_bytes(report)
        if len(body) > MAX_REPORT:
            raise ValueError('Report exceeded storage limit')
        self.client.put_object(Bucket=self.bucket, Key='reports/' + _identifier(identifier) + '.json',
                               Body=body, ContentType='application/json', ServerSideEncryption='AES256')

    def get(self, identifier):
        try:
            response = self.client.get_object(Bucket=self.bucket, Key='reports/' + _identifier(identifier) + '.json')
        except Exception as error:
            if getattr(error, 'response', {}).get('Error', {}).get('Code') in ('NoSuchKey', '404'):
                return None
            raise
        body = response['Body']
        try:
            data = body.read(MAX_REPORT + 1)
        finally:
            body.close()
        if len(data) > MAX_REPORT:
            raise ValueError('Stored report exceeded limit')
        return json.loads(data)


class BusyError(Exception):
    pass


class Application:
    def __init__(self, access_code, runner, reports, chat=None):
        if not isinstance(access_code, str) or len(access_code) < 8:
            raise ValueError('A demo access code of at least eight characters is required')
        self._access_code = access_code.encode('utf-8')
        self.runner, self.reports = runner, reports
        self.chat = chat
        self._lock = threading.Lock()
        self._active = None
        self._runs = OrderedDict()

    def authorized(self, code):
        return isinstance(code, str) and len(code) <= 256 and hmac.compare_digest(
            code.encode('utf-8'), self._access_code)

    def start(self):
        with self._lock:
            if self._active:
                raise BusyError('A fixed demo is already running')
            identifier = uuid4().hex
            self._active = identifier
            self._runs[identifier] = {'id': identifier, 'status': 'running',
                                      'started_at': datetime.now(timezone.utc).isoformat()}
            while len(self._runs) > 100:
                self._runs.popitem(last=False)
        threading.Thread(target=self._run, args=(identifier,), daemon=True).start()
        return identifier

    def _run(self, identifier):
        with self._lock:
            value = dict(self._runs[identifier])
        try:
            result = self.runner()
            # Enforce serialization and size before keeping a result in memory.
            if len(_json_bytes(result)) > MAX_REPORT - 2048:
                raise ValueError('Demo report exceeded limit')
            value.update(status='completed', report=result)
        except Exception:
            value.update(status='error', error='The demo could not finish. Check operator logs and try again.')
        value['finished_at'] = datetime.now(timezone.utc).isoformat()
        value['persistence'] = getattr(self.reports, 'persistence', 'saved')
        if value['persistence'] == 'ephemeral':
            value['persistence_note'] = 'Local test report: stored in memory and lost when this process exits.'
        try:
            self.reports.put(identifier, value)
        except Exception:
            value['persistence'] = 'failed'
            value['persistence_note'] = 'This result is only in memory; it will be lost if this worker restarts.'
        with self._lock:
            self._runs[identifier] = value
            self._active = None

    def get(self, identifier):
        _identifier(identifier)
        with self._lock:
            local = self._runs.get(identifier)
            if local is not None:
                return json.loads(_json_bytes(local))
        return self.reports.get(identifier)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(24)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(10)
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_server(app, host='0.0.0.0', port=8080):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'StateTree'
        sys_version = ''

        def log_message(self, *args):
            pass  # No paths, access codes, credentials or inference payloads.

        def _send(self, code, body, content_type='application/json; charset=utf-8'):
            if not isinstance(body, bytes):
                body = _json_bytes(body)
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass

        def do_GET(self):
            if self.path == '/ping':
                return self._send(200, {'status': 'ok'})
            if self.path.startswith(('/api/chat/', '/api/worker/')):
                return self._chat('GET')
            assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/benchmark': ('benchmark.html', 'text/html; charset=utf-8'),
                      '/static/style.css': ('style.css', 'text/css; charset=utf-8'),
                      '/static/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                      '/static/benchmark.css': ('benchmark.css', 'text/css; charset=utf-8'),
                      '/static/benchmark.js': ('benchmark.js', 'text/javascript; charset=utf-8')}
            if self.path in assets:
                filename, content_type = assets[self.path]
                return self._send(200, (STATIC / filename).read_bytes(), content_type)
            match = re.fullmatch(r'/api/runs/([0-9a-f]{32})', self.path)
            if match:
                try:
                    result = app.get(match[1])
                except Exception:
                    return self._send(503, {'error': 'Report storage is temporarily unavailable.'})
                if result is not None:
                    return self._send(200, result)
            return self._send(404, {'error': 'Unknown report or path. A worker restart can interrupt unfinished runs; they are not resumed.'})

        def do_POST(self):
            if self.path.startswith(('/api/chat/', '/api/worker/')):
                return self._chat('POST')
            if self.path != '/api/runs':
                return self._send(404, {'error': 'Unknown path.'})
            if not app.authorized(self.headers.get('X-Demo-Key')):
                return self._send(401, {'error': 'Enter the demo access code supplied by the operator.'})
            lengths = self.headers.get_all('Content-Length', [])
            if self.headers.get('Transfer-Encoding') or len(lengths) > 1:
                return self._send(400, {'error': 'Use one Content-Length header and no transfer encoding.'})
            if not lengths:
                return self._send(411, {'error': 'Content-Length is required.'})
            if not re.fullmatch(r'[0-9]+', lengths[0]):
                return self._send(400, {'error': 'Invalid request length.'})
            length = int(lengths[0])
            if length < 0 or length > MAX_BODY:
                return self._send(413, {'error': 'The request body is too large.'})
            try:
                body = self.rfile.read(length)
                if len(body) != length or json.loads(body) != {}:
                    raise ValueError('Only the fixed demo is accepted')
            except (ValueError, UnicodeError, RecursionError, socket.timeout):
                return self._send(400, {'error': 'Send an empty JSON object to run the fixed demo.'})
            try:
                identifier = app.start()
            except BusyError:
                return self._send(409, {'error': 'A demo is already running. Wait for it to finish.'})
            return self._send(202, {'id': identifier, 'status': 'running'})

        def _chat(self, method):
            from statetree.web.chat import MAX_CHAT_BODY
            if app.chat is None:
                return self._send(503, {'error': 'Private chat is not configured. Ask the operator to prepare owner and worker credentials.'})
            if not app.chat.authorized(self.path, self.headers):
                return self._send(401, {'error': 'Valid private owner or worker credentials are required.'})
            lengths = self.headers.get_all('Content-Length', [])
            if self.headers.get('Transfer-Encoding') or len(lengths) > 1:
                return self._send(400, {'error': 'Use one Content-Length header and no transfer encoding.'})
            if method == 'POST' and not lengths:
                return self._send(411, {'error': 'Content-Length is required.'})
            if lengths and (len(lengths[0]) > 10 or not re.fullmatch(r'[0-9]+', lengths[0])):
                return self._send(400, {'error': 'Invalid request length.'})
            length = int(lengths[0]) if lengths else 0
            if length > MAX_CHAT_BODY:
                return self._send(413, {'error': 'The request body is too large.'})
            if method == 'GET' and length:
                return self._send(400, {'error': 'GET requests do not accept a body.'})
            try:
                body = self.rfile.read(length) if length else b''
                if len(body) != length:
                    return self._send(400, {'error': 'The request body was incomplete.'})
            except (socket.timeout, ConnectionError):
                return self._send(400, {'error': 'The request body was incomplete.'})
            status, result = app.chat.dispatch(method, self.path, self.headers, body)
            return self._send(status, result)

    return _Server((host, port), Handler)


def create_application(settings):
    from statetree.models.bedrock import BedrockModel
    from statetree.web.chat import ChatApplication, FileChatStore, S3ChatStore, MAX_INFERENCE_RESPONSE
    from statetree.web.demo import run_demo
    if settings.local_model_url:
        secret = json.loads(Path(settings.local_secret_file).read_text(encoding='utf-8'))
        reports = MemoryReports()
        transport = LocalClient(settings.local_model_url, secret['upstream_api_key'])
        model_id = 'local-hybrid-test'
        chat_store = FileChatStore(Path(settings.local_secret_file).with_name('chat.sqlite')) if secret.get('owner_access_code') and secret.get('worker_api_key') else None
        def factory():
            from statetree.models.sagemaker import SageMakerModel
            return SageMakerModel(model_id, client=transport,
                                  temperature=0, max_tokens=64, enable_thinking=False, context_window_limit=4096)
    else:
        import boto3
        from botocore.config import Config
        config = Config(connect_timeout=5, read_timeout=10, retries={'total_max_attempts': 1})
        session = boto3.Session(region_name=settings.region)
        secrets = session.client('secretsmanager', config=config)
        secret = json.loads(secrets.get_secret_value(SecretId=settings.secret_arn)['SecretString'])
        s3 = session.client('s3', config=config)
        reports = S3Reports(settings.report_bucket, s3)
        chat_store = S3ChatStore(settings.report_bucket, s3)
        transport = session.client('bedrock-runtime', config=Config(
            connect_timeout=5, read_timeout=55, retries={'total_max_attempts': 1}))
        model_id = settings.model_id
        def factory():
            return BedrockModel(settings.model_id, region_name=settings.region, temperature=0,
                                max_tokens=64, context_window_limit=4096)
    def inference(payload):
        if settings.local_model_url:
            response = transport.invoke_endpoint(EndpointName=model_id, ContentType='application/json',
                                                 Accept='application/json', Body=_json_bytes(payload))
            body = response['Body']
            try:
                data = body.read(MAX_INFERENCE_RESPONSE + 1)
            finally:
                body.close()
            if len(data) > MAX_INFERENCE_RESPONSE:
                raise ValueError('Model response exceeded relay limit')
            return json.loads(data)
        response = transport.converse(modelId=model_id, **payload)
        if len(_json_bytes(response)) > MAX_INFERENCE_RESPONSE:
            raise ValueError('Model response exceeded relay limit')
        return response
    chat = None
    if secret.get('owner_access_code') and secret.get('worker_api_key'):
        keys = [secret[name] for name in ('demo_access_code', 'owner_access_code', 'worker_api_key')]
        if len(set(keys)) != len(keys):
            raise ValueError('Benchmark, owner and worker credentials must be distinct')
        chat = ChatApplication(secret['owner_access_code'], secret['worker_api_key'], chat_store, inference,
                               max_output_tokens=int(os.environ.get('CHAT_MAX_OUTPUT_TOKENS', '256')))
    return Application(secret['demo_access_code'], lambda: run_demo(factory), reports, chat=chat)


def main():
    settings = Settings.from_environ()
    app = create_application(settings)
    with make_server(app, settings.host, settings.port) as server:
        print(f'StateTree demo listening on {settings.host}:{settings.port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
