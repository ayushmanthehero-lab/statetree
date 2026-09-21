"""Authenticated loopback workbench for a single local StateTree project.

Separate from the existing cloud task console. Authenticated Agent tasks can edit
project files and, with explicit per-task consent, execute native commands.
Native execution is not a security sandbox. Model/cloud configuration stays local.
"""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys
import threading
from urllib.parse import parse_qs, urlsplit

from statetree.agent.files import ProjectBusy
from statetree.project import Project
from statetree.storage.local import HeadConflictError
from statetree.workspace.branches import MergeConflictError, StaleParentError

MAX_BODY = 16 * 1024 * 1024
ASSETS = {'/': ('workbench.html', 'text/html; charset=utf-8'),
          '/workbench.css': ('workbench.css', 'text/css; charset=utf-8'),
          '/workbench.js': ('workbench.js', 'text/javascript; charset=utf-8'),
          '/agentic.js': ('agentic.js', 'text/javascript; charset=utf-8')}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")


class RequestError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def strict_json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate JSON field')
            value[key] = item
        return value
    def constant(value):
        raise ValueError('Nonfinite JSON numbers are not accepted')
    value = json.loads(raw, object_pairs_hook=unique, parse_constant=constant)
    if type(value) is not dict:
        raise ValueError('Expected a JSON object')
    return value


def fields(body, allowed, required=()):
    if set(body) - set(allowed) or set(required) - set(body):
        raise RequestError(400, 'Missing or unsupported request fields')


def route(project, path, body):
    """Fixed capability table, not arbitrary reflection over the Project object."""
    if path == '/api/context':
        fields(body, {'query', 'new_task'}, {'query'})
        return project.context(body['query'], new_task=body.get('new_task', False))
    commands = {
        '/api/checkpoint': ({'message', 'verify'}, {'message'}),
        '/api/remember': ({'key', 'value', 'evidence', 'dependencies'}, {'key', 'value', 'evidence'}),
        '/api/forget': ({'key', 'action'}, {'key'}),
        '/api/restore': ({'checkpoint_id', 'confirm'}, {'checkpoint_id', 'confirm'}),
        '/api/compact': (set(), set()),
        '/api/import': ({'bundle'}, {'bundle'}),
        '/api/ask': ({'prompt', 'new_task'}, {'prompt'}),
        '/api/fork': ({'name', 'reads', 'writes'}, {'name'}),
        '/api/complete': ({'name', 'state', 'result', 'expected_candidate'}, {'name', 'expected_candidate'}),
        '/api/merge': ({'candidate_id', 'expected_parent'}, {'candidate_id', 'expected_parent'}),
        '/api/adopt': ({'revision_id', 'confirm'}, {'revision_id', 'confirm'}),
    }
    if path not in commands:
        raise RequestError(404, 'Unknown API route')
    allowed, required = commands[path]
    fields(body, allowed | {'expected_head'})
    if body.get('expected_head') != project.runtime._parent:
        raise RequestError(409, 'The project changed or expected_head is missing. Refresh before changing state.')
    fields(body, allowed | {'expected_head'}, required)
    if path in ('/api/restore', '/api/adopt') and body['confirm'] is not True:
        raise RequestError(400, 'Explicit confirmation is required before changing captured files')
    if path == '/api/checkpoint':
        return project.checkpoint(body['message'], verify=body.get('verify', False))
    if path == '/api/remember':
        return project.remember(body['key'], body['value'], evidence=body['evidence'], dependencies=body.get('dependencies'))
    if path == '/api/forget':
        return project.forget(body['key'], action=body.get('action', 'archive'))
    if path == '/api/restore':
        return project.restore(body['checkpoint_id'])
    if path == '/api/compact':
        return project.compact()
    if path == '/api/import':
        return project.import_state(body['bundle'])
    if path == '/api/ask':
        return project.ask(body['prompt'], new_task=body.get('new_task', False))
    if path == '/api/fork':
        return project.fork(body['name'], reads=body.get('reads'), writes=body.get('writes'))
    if path == '/api/complete':
        manager = project.branch_manager()
        branch = manager.open_branch(body['name'])
        if branch.candidate_id != body['expected_candidate']:
            raise RequestError(409, 'The branch candidate changed. Refresh before completing it.')
        from statetree.core.state import AgentState
        state = AgentState.from_dict(body.get('state', branch.state)).to_dict()
        return manager.complete(branch, state=state, result=body.get('result'))
    if path == '/api/merge':
        if type(body['expected_parent']) is not str or not body['expected_parent']:
            raise RequestError(400, 'Select the current branch canonical parent')
        return project.merge(body['candidate_id'], expected_parent=body['expected_parent'])
    return project.adopt(body['revision_id'])


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_close(self):
        if hasattr(self, 'task_manager'):
            self.task_manager.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = 'StateTreeLocal/0.3'

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        # Never log tokens, prompt query strings or evidence content.
        pass

    def send_bytes(self, status, payload, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', CSP)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, status, value):
        self.send_bytes(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'),
                        'application/json; charset=utf-8')

    def authorize(self, *, api):
        port = self.server.server_port
        hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if len(self.headers.get_all('Host', [])) != 1 or self.headers.get('Host') not in hosts:
            raise RequestError(403, 'Invalid local Host header')
        origin = self.headers.get('Origin')
        if origin is not None and origin not in {'http://' + host for host in hosts}:
            raise RequestError(403, 'Cross-origin access is not allowed')
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise RequestError(403, 'Cross-site access is not allowed')
        if api:
            auth = self.headers.get('Authorization', '')
            if (len(self.headers.get_all('Authorization', [])) != 1 or len(auth) > 512 or
                    not hmac.compare_digest(auth.encode('utf-8'), ('Bearer ' + self.server.token).encode('ascii'))):
                raise RequestError(401, 'Enter the local access token printed by the server')

    def handle_error(self, error):
        if isinstance(error, RequestError):
            code, message = error.status, str(error)
        elif isinstance(error, (HeadConflictError, MergeConflictError, StaleParentError, ProjectBusy)):
            code, message = 409, str(error)
        elif isinstance(error, (ValueError, TypeError, KeyError, UnicodeError)):
            code, message = 400, str(error)
        elif isinstance(error, FileNotFoundError):
            code, message = 404, 'The requested local object was not found'
        elif isinstance(error, RuntimeError):
            code, message = 422, str(error)
        else:
            code, message = 500, 'Local operation failed; inspect the project before retrying'
        self.send_json(code, {'error': message, 'type': type(error).__name__})

    def do_GET(self):
        try:
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc:
                raise RequestError(400, 'Absolute request targets are not supported')
            path = parsed.path
            self.authorize(api=path.startswith('/api/'))
            if path in ASSETS:
                filename, content_type = ASSETS[path]
                return self.send_bytes(200, (Path(__file__).parent / 'static' / filename).read_bytes(), content_type)
            if path == '/healthz':
                return self.send_json(200, {'ok': True})
            query = parse_qs(parsed.query, max_num_fields=12, strict_parsing=True)
            if any(len(values) != 1 for values in query.values()):
                raise RequestError(400, 'Repeated query fields are not supported')
            one = lambda key, default=None: query.get(key, [default])[0]
            if path == '/api/tasks':
                fields(query, set())
                return self.send_json(200, self.server.task_manager.list())
            if path == '/api/task':
                fields(query, {'id', 'after'}, {'id'})
                return self.send_json(200, self.server.task_manager.get(one('id'), int(one('after', '0'))))
            with self.server.operation_lock:
                project = Project(self.server.repo)
                if path == '/api/status':
                    value = project.status()
                elif path == '/api/history':
                    value = project.history(limit=int(one('limit', '50')), cursor=one('cursor'))
                elif path == '/api/facts':
                    value = project.facts()
                elif path == '/api/branches':
                    value = project.branches()
                elif path == '/api/usage':
                    fields(query, {'run_id', 'branch', 'phase'})
                    value = project.usage(**{key: val[0] for key, val in query.items()})
                elif path == '/api/recall':
                    value = project.recall(one('query', ''), budget=int(one('budget')) if one('budget') else None)
                elif path == '/api/archive':
                    value = project.read_archive(one('id'), offset=int(one('offset', '0')), limit=int(one('limit', '1000')))
                elif path == '/api/export':
                    value = project.export_state()
                else:
                    raise RequestError(404, 'Unknown route')
            self.send_json(200, value)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            self.handle_error(error)

    def do_POST(self):
        try:
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.query:
                raise RequestError(400, 'POST requires a plain local route')
            self.authorize(api=True)
            if self.headers.get('Transfer-Encoding'):
                raise RequestError(400, 'Chunked request bodies are not supported')
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1:
                raise RequestError(411, 'A single Content-Length is required')
            length = int(lengths[0])
            if not 0 <= length <= MAX_BODY:
                raise RequestError(413, 'Request body exceeds the 16 MiB limit')
            if self.headers.get_content_type() != 'application/json':
                raise RequestError(415, 'Use application/json')
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise RequestError(400, 'Incomplete body')
            body = strict_json(raw)
            manager = self.server.task_manager
            if parsed.path == '/api/tasks':
                fields(body, {'prompt', 'new_task', 'allow_execution'}, {'prompt'})
                result = manager.submit(body['prompt'], new_task=body.get('new_task', False),
                                        allow_execution=body.get('allow_execution', False))
                return self.send_json(202, result)
            if parsed.path in ('/api/task/resume', '/api/task/pause'):
                fields(body, {'task_id'}, {'task_id'})
                result = (manager.resume(body['task_id']) if parsed.path.endswith('/resume')
                          else manager.pause(body['task_id']))
                return self.send_json(202 if parsed.path.endswith('/resume') else 200, result)
            if parsed.path == '/api/task/resolve':
                fields(body, {'task_id', 'operation_id', 'note', 'confirm'},
                       {'task_id', 'operation_id', 'note', 'confirm'})
                return self.send_json(200, manager.resolve(body['task_id'], body['operation_id'],
                                      body['note'], confirm=body['confirm']))
            with self.server.operation_lock:
                if parsed.path != '/api/context' and (manager.busy() or manager.unresolved()):
                    raise RequestError(409, 'Pause/reconcile the active agent task before changing project state here.')
                result = route(Project(self.server.repo), parsed.path, body)
            self.send_json(200, result)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            self.close_connection = True
            self.handle_error(error)


def make_server(repo, *, port=8765, token=None):
    project = Project(repo)
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('Port must be an integer between 0 and 65535')
    token = secrets.token_urlsafe(32) if token is None else token
    if type(token) is not str or not 24 <= len(token) <= 256 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError('Use a 24-256 character ASCII access token without whitespace')
    server = WorkbenchServer(('127.0.0.1', port), Handler)
    server.repo, server.token, server.operation_lock = project.repo, token, threading.RLock()
    from statetree.agentic.manager import TaskManager
    server.task_manager = TaskManager(project.repo)
    return server


def serve(repo, *, port=8765):
    server = make_server(repo, port=port)
    print(f'StateTree workbench: http://127.0.0.1:{server.server_port}\n'
          f'Local access token: {server.token}\n'
          'Open the URL and enter the token. Keep this terminal open; Ctrl+C stops the server.\n'
          'Verification runs trusted commands from project.json; this is not a sandbox.', file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
