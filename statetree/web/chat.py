"""Private, durable task queue. Cloud leases fence acknowledgements, not host effects.

Stores expose read() -> (state, version) and compare_and_swap(version, state).
Every mutation, including a claim, is retried from a fresh snapshot after a CAS
conflict. The local worker must additionally hold its process/journal lock.
"""

from contextlib import closing
from datetime import datetime, timezone
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from uuid import uuid4


MAX_CHAT_BODY = 256 * 1024
MAX_INFERENCE_BODY = 64 * 1024
MAX_INFERENCE_RESPONSE = 1024 * 1024
MAX_STATE = 16 * 1024 * 1024
MAX_TASKS = 100
MAX_EVENTS = 2000
MAX_TASK_BYTES = 1536 * 1024
MAX_PROMPT_BYTES = 2000
IDENTIFIER = re.compile(r'[0-9a-f]{32}\Z')
TOKEN = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
ACTIVE = {'running', 'cancel_requested', 'applying'}
RESUMABLE = {'interrupted', 'failed', 'cancelled', 'waiting_for_input'}
WORKER_STATUSES = ACTIVE | RESUMABLE | {'completed', 'applied', 'conflict'}
PUBLIC_FIELDS = ('id', 'project_id', 'prompt', 'status', 'events', 'checkpoint', 'result',
                 'usage', 'diff', 'created_at', 'updated_at', 'resume_count',
                 'cancel_requested', 'apply_requested', 'parent_task_id')


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def _copy(value):
    return json.loads(_encode(value))


def _empty():
    return {'version': 1, 'revision': 0, 'projects': {}, 'tasks': {}}


def _state_bytes(state):
    data = _encode(state)
    if len(data) > MAX_STATE:
        raise ChatError(507, 'Private task storage is full. Export or archive older tasks locally.')
    return data


class MemoryChatStore:
    """Atomic test store; use FileChatStore or S3ChatStore for durable sessions."""
    def __init__(self):
        self._state, self._version = _empty(), 0
        self._lock = threading.Lock()

    def read(self):
        with self._lock:
            return _copy(self._state), self._version

    def compare_and_swap(self, version, state):
        data = _state_bytes(state)
        with self._lock:
            if version != self._version:
                return False
            self._state = json.loads(data)
            self._version += 1
            return True


class FileChatStore:
    """Single-document SQLite storage with CAS across local HTTP processes."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS chat (id INTEGER PRIMARY KEY, version INTEGER NOT NULL, body BLOB NOT NULL)')
            db.execute('INSERT OR IGNORE INTO chat VALUES (1, 0, ?)', (_encode(_empty()),))

    def read(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            version, data = db.execute('SELECT version, body FROM chat WHERE id=1').fetchone()
            return json.loads(data), version

    def compare_and_swap(self, version, state):
        data = _state_bytes(state)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            result = db.execute('UPDATE chat SET version=version+1, body=? WHERE id=1 AND version=?', (data, version))
            return result.rowcount == 1


class S3ChatStore:
    """Private encrypted state; S3 ETags fence writers across ECS instances."""
    def __init__(self, bucket, client, key='private-chat/state.json'):
        if key != 'private-chat/state.json':
            raise ValueError('Chat state must use the private-chat/state.json key')
        self.bucket, self.client, self.key = bucket, client, key

    def read(self):
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.key)
        except Exception as error:
            if getattr(error, 'response', {}).get('Error', {}).get('Code') in ('NoSuchKey', '404'):
                return _empty(), None
            raise
        body = response['Body']
        try:
            data = body.read(MAX_STATE + 1)
        finally:
            body.close()
        if len(data) > MAX_STATE:
            raise ValueError('Stored private state exceeded limit')
        return json.loads(data), response['ETag']

    def compare_and_swap(self, version, state):
        condition = {'IfNoneMatch': '*'} if version is None else {'IfMatch': version}
        try:
            self.client.put_object(Bucket=self.bucket, Key=self.key, Body=_state_bytes(state),
                                   ContentType='application/json', ServerSideEncryption='AES256', **condition)
        except Exception as error:
            if getattr(error, 'response', {}).get('Error', {}).get('Code') in (
                    'PreconditionFailed', 'ConditionalRequestConflict', '409', '412'):
                return False
            raise
        return True


class ChatError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


def _object(value, allowed, required=()):
    if not isinstance(value, dict) or value.keys() - set(allowed) or set(required) - value.keys():
        raise ChatError(400, 'Unsupported or missing JSON fields.')
    return value


def _text(value, label, limit, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ChatError(400, f'Invalid {label}.')
    return value


def _id(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ChatError(400, 'Invalid task or project identifier.')
    return value


def _token(value, label):
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise ChatError(400, f'Invalid {label}.')
    return value


def _bounded_object(value, limit, label):
    if not isinstance(value, dict) or len(_encode(value)) > limit:
        raise ChatError(400, f'Invalid or oversized {label}.')
    return value


def _stamp(now):
    return datetime.fromtimestamp(now, timezone.utc).isoformat()


def _public(task):
    return _copy({key: task.get(key) for key in PUBLIC_FIELDS})


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON fields')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('Nonfinite JSON numbers are not supported')


class ChatApplication:
    def __init__(self, owner_key, worker_key, store, inference, *, clock=time.time,
                 lease_seconds=90, max_output_tokens=256):
        if any(not isinstance(key, str) or not 16 <= len(key) <= 256 for key in (owner_key, worker_key)):
            raise ValueError('Separate owner and worker keys of 16 to 256 characters are required')
        if hmac.compare_digest(owner_key.encode(), worker_key.encode()):
            raise ValueError('Owner and worker credentials must differ')
        if not 30 <= lease_seconds <= 600 or not 1 <= max_output_tokens <= 8192:
            raise ValueError('Invalid lease or output token limit')
        self._owner_key, self._worker_key = owner_key.encode(), worker_key.encode()
        self.store, self.inference = store, inference
        self.clock, self.lease_seconds, self.max_output_tokens = clock, lease_seconds, max_output_tokens

    def authorized(self, path, headers):
        worker = path.startswith('/api/worker/')
        name, expected = ('x-worker-key', self._worker_key) if worker else ('x-owner-key', self._owner_key)
        values = [v for k, v in headers.items() if k.lower() == name]
        return (len(values) == 1 and isinstance(values[0], str) and len(values[0]) <= 256
                and hmac.compare_digest(values[0].encode('utf-8'), expected))

    def dispatch(self, method, path, headers, body=b''):
        """Return a bounded HTTP status/JSON pair; never expose storage/provider errors."""
        if '?' in path or not path.startswith(('/api/chat/', '/api/worker/')):
            return 404, {'error': 'Unknown path.'}
        if not self.authorized(path, headers):
            return 401, {'error': 'Valid private owner or worker credentials are required.'}
        try:
            if not isinstance(body, bytes) or len(body) > MAX_CHAT_BODY:
                raise ChatError(413, 'The request body is too large.')
            payload = json.loads(body, object_pairs_hook=_pairs, parse_constant=_constant) if body else {}
            if not isinstance(payload, dict):
                raise ChatError(400, 'Send a JSON object.')
            if method == 'GET' and body:
                raise ChatError(400, 'GET requests do not accept a body.')
            if method == 'GET' and path == '/api/chat/state':
                return 200, self._transaction(self._state)
            if method == 'POST' and path == '/api/chat/tasks':
                return 200, self._transaction(lambda state, now: self._submit(state, now, payload))
            match = re.fullmatch(r'/api/chat/tasks/([0-9a-f]{32})(?:/(cancel|apply))?', path)
            if match and ((method == 'GET' and match[2] is None) or (method == 'POST' and match[2])):
                _object(payload, ())
                return 200, self._transaction(lambda state, now: self._owner_task(state, now, match[1], match[2]))
            if method == 'POST' and path == '/api/worker/poll':
                return 200, self._transaction(lambda state, now: self._poll(state, now, payload))
            match = re.fullmatch(r'/api/worker/tasks/([0-9a-f]{32})/(events|inference)', path)
            if method == 'POST' and match:
                if match[2] == 'inference':
                    return 200, self._infer(match[1], payload)
                return 200, self._transaction(lambda state, now: self._events(state, now, match[1], payload))
            return 404, {'error': 'Unknown path.'}
        except ChatError as error:
            return error.status, {'error': error.message}
        except (ValueError, UnicodeError, RecursionError, TypeError):
            return 400, {'error': 'Invalid JSON request.'}
        except Exception:
            return 503, {'error': 'Private task storage or model transport is temporarily unavailable.'}

    def _transaction(self, operation):
        for _ in range(16):
            state, version = self.store.read()
            before = _encode(state)
            now = self.clock()
            self._expire(state, now)
            result = operation(state, now)
            if _encode(state) == before:
                return result
            state['revision'] = state.get('revision', 0) + 1
            if self.store.compare_and_swap(version, state):
                return result
        raise ChatError(503, 'Private task storage is busy. Retry the request.')

    def _expire(self, state, now):
        for task in state['tasks'].values():
            if task['status'] in ACTIVE and task.get('_lease_until', 0) <= now:
                task.update(status='interrupted', updated_at=_stamp(now))
                task['_lease'] = None
                # Apply must be requested explicitly again after an uncertain interruption.
                task['apply_requested'] = False

    def _state(self, state, now):
        projects = [{key: project[key] for key in ('id', 'name', 'last_seen')} |
                    {'online': any(expiry > now for expiry in project.get('_workers', {}).values())}
                    for project in state['projects'].values()]
        tasks = sorted(state['tasks'].values(), key=lambda task: task['created_at'], reverse=True)
        return {'projects': projects, 'tasks': [_public(task) for task in tasks]}

    def _submit(self, state, now, body):
        _object(body, ('project_id', 'prompt', 'fresh', 'task_id', 'parent_task_id'), ('project_id', 'prompt'))
        project_id = _id(body['project_id'])
        prompt = _text(body['prompt'], 'prompt', MAX_PROMPT_BYTES)
        if len(prompt.encode('utf-8')) > MAX_PROMPT_BYTES:
            raise ChatError(400, 'Keep the task prompt within 2000 UTF-8 bytes so it fits the local model context.')
        normalized = ' '.join(prompt.split())
        fresh = body.get('fresh', False)
        if type(fresh) is not bool or (fresh and 'task_id' in body):
            raise ChatError(400, 'Fresh execution cannot specify an existing task.')
        if project_id not in state['projects']:
            raise ChatError(404, 'Register this project with the local worker first.')
        parent_id = body.get('parent_task_id')
        if parent_id is not None:
            parent = self._task(state, _id(parent_id))
            if parent['project_id'] != project_id or parent['status'] not in {'completed', 'applied'}:
                raise ChatError(409, 'Follow-up tasks require a completed or applied task in the same project.')
        selected = None
        if 'task_id' in body:
            selected = self._task(state, _id(body['task_id']))
            if selected['project_id'] != project_id or selected['_prompt_identity'] != normalized:
                raise ChatError(409, 'The existing task belongs to a different project or prompt.')
            if 'parent_task_id' in body and selected.get('parent_task_id') != parent_id:
                raise ChatError(409, 'The existing task belongs to a different conversation.')
        elif not fresh:
            matches = [task for task in state['tasks'].values()
                       if task['project_id'] == project_id and task['_prompt_identity'] == normalized
                       and task.get('parent_task_id') == parent_id]
            if matches:
                selected = max(matches, key=lambda task: (task['created_at'], task['_order']))
        if selected is not None:
            resumed = selected['status'] in RESUMABLE
            if resumed:
                selected.update(status='queued', cancel_requested=False, apply_requested=False,
                                resume_count=selected['resume_count'] + 1, updated_at=_stamp(now))
                selected.update(_lease=None, _worker_id=None, _lease_until=0)
            return {'task': _public(selected), 'resumed': resumed}
        if len(state['tasks']) >= MAX_TASKS:
            raise ChatError(507, 'Task history is full. Archive tasks locally before starting another task.')
        identifier = uuid4().hex
        task = {'id': identifier, 'project_id': project_id, 'prompt': prompt.strip(), 'status': 'queued',
                'events': [], 'checkpoint': None, 'result': '', 'usage': None, 'diff': '',
                'created_at': _stamp(now), 'updated_at': _stamp(now), 'resume_count': 0,
                'cancel_requested': False, 'apply_requested': False, '_prompt_identity': normalized,
                'parent_task_id': parent_id,
                '_order': state.get('revision', 0), '_worker_id': None, '_lease': None, '_lease_until': 0}
        state['tasks'][identifier] = task
        return {'task': _public(task), 'resumed': False}

    def _task(self, state, identifier):
        task = state['tasks'].get(identifier)
        if task is None:
            raise ChatError(404, 'Unknown task.')
        return task

    def _owner_task(self, state, now, identifier, action):
        task = self._task(state, identifier)
        if action == 'cancel':
            if task['status'] in ACTIVE | {'queued', 'waiting_for_input'}:
                task['cancel_requested'] = True
                task['status'] = 'cancelled' if task['status'] in {'queued', 'waiting_for_input'} else 'cancel_requested'
                task['updated_at'] = _stamp(now)
        if action == 'apply':
            if task['status'] not in {'completed', 'conflict', 'applying', 'applied'}:
                raise ChatError(409, 'Only a completed task can be applied.')
            if task['status'] not in {'applying', 'applied'}:
                task.update(apply_requested=True, cancel_requested=False, updated_at=_stamp(now))
        return {'task': _public(task)}

    def _lease(self, task, now, worker_id, lease):
        if (not isinstance(lease, str) or len(lease) > 128 or not task.get('_lease')
                or not hmac.compare_digest(task['_lease'], lease) or task.get('_worker_id') != worker_id
                or task.get('_lease_until', 0) <= now):
            raise ChatError(409, 'The worker lease is stale. Reconcile local work before resuming.')

    def _response(self, task):
        return {'task': _public(task) if task else None, 'lease': task['_lease'] if task else None,
                'cancel_requested': bool(task and task['cancel_requested']),
                'apply_requested': bool(task and task['apply_requested'])}

    def _poll(self, state, now, body):
        _object(body, ('worker_id', 'projects', 'active_task_id', 'lease'), ('worker_id', 'projects'))
        worker_id = _token(body['worker_id'], 'worker ID')
        projects = body['projects']
        if not isinstance(projects, list) or len(projects) > 32:
            raise ChatError(400, 'Invalid project registration.')
        ids = set()
        for project in projects:
            _object(project, ('id', 'name'), ('id', 'name'))
            identifier = _id(project['id'])
            if identifier in ids:
                raise ChatError(400, 'Duplicate registered project.')
            ids.add(identifier)
            name = _text(project['name'], 'project name', 120)
            if '/' in name or '\\' in name:
                raise ChatError(400, 'Send only the project name, not a local path.')
            registered = state['projects'].setdefault(identifier, {'id': identifier, '_workers': {}})
            registered.update(name=name, last_seen=_stamp(now))
            registered['_workers'] = {key: expiry for key, expiry in registered['_workers'].items() if expiry > now}
            registered['_workers'][worker_id] = now + self.lease_seconds
        if len(state['projects']) > 100:
            raise ChatError(400, 'Too many registered projects.')
        active_id = body.get('active_task_id')
        if active_id is not None:
            task = self._task(state, _id(active_id))
            if task['project_id'] not in ids:
                raise ChatError(409, 'The active task project must remain registered.')
            self._lease(task, now, worker_id, body.get('lease'))
            task['_lease_until'] = now + self.lease_seconds
            return self._response(task)
        if body.get('lease') is not None:
            raise ChatError(400, 'A lease requires its active task identifier.')
        # A lost claim response must return the same lease to this worker.
        active = [task for task in state['tasks'].values() if task['project_id'] in ids
                  and task['status'] in ACTIVE and task.get('_worker_id') == worker_id]
        if active:
            task = min(active, key=lambda item: (item['created_at'], item['_order']))
            task['_lease_until'] = now + self.lease_seconds
            return self._response(task)
        busy_projects = {task['project_id'] for task in state['tasks'].values() if task['status'] in ACTIVE}
        candidates = [task for task in state['tasks'].values() if task['project_id'] in ids
                      and task['project_id'] not in busy_projects and
                      (task['status'] == 'queued' or (task['apply_requested'] and task['status'] in {'completed', 'conflict'}))]
        if not candidates:
            return self._response(None)
        task = min(candidates, key=lambda item: (item['created_at'], item['_order']))
        task.update(_worker_id=worker_id, _lease=secrets.token_urlsafe(32), _lease_until=now + self.lease_seconds,
                    status='applying' if task['apply_requested'] else 'running', updated_at=_stamp(now))
        return self._response(task)

    def _events(self, state, now, identifier, body):
        _object(body, ('worker_id', 'lease', 'events', 'status', 'checkpoint', 'result', 'usage', 'diff'),
                ('worker_id', 'lease', 'events'))
        worker_id = _token(body['worker_id'], 'worker ID')
        task = self._task(state, identifier)
        self._lease(task, now, worker_id, body['lease'])
        events = body['events']
        if not isinstance(events, list) or len(events) > 100:
            raise ChatError(400, 'Send at most 100 events per request.')
        existing = {event['id']: event for event in task['events']}
        for event in events:
            _object(event, ('id', 'type', 'text', 'details', 'created_at'), ('id', 'type', 'text'))
            _token(event['id'], 'event ID')
            _text(event['type'], 'event type', 64)
            _text(event['text'], 'event text', 32000, empty=True)
            if 'details' in event:
                _bounded_object(event['details'], 64 * 1024, 'event details')
            if 'created_at' in event:
                _text(event['created_at'], 'event timestamp', 64)
            normalized = {'details': {}, 'created_at': _stamp(now), **event}
            if event['id'] in existing:
                previous = existing[event['id']]
                # A retry without an explicit timestamp reuses its original server timestamp.
                if 'created_at' not in event:
                    normalized['created_at'] = previous['created_at']
                if normalized != previous:
                    raise ChatError(409, 'An event ID already records different evidence.')
                continue
            if len(task['events']) >= MAX_EVENTS:
                raise ChatError(507, 'The task event limit has been reached; checkpoint locally.')
            task['events'].append(normalized)
            existing[event['id']] = normalized
        if 'status' in body:
            status = body['status']
            if not isinstance(status, str) or status not in WORKER_STATUSES:
                raise ChatError(400, 'Unsupported task status.')
            previous = task['status']
            if previous not in ACTIVE and status != previous:
                raise ChatError(409, 'A finished task cannot return to execution without explicit resume.')
            applying = previous == 'applying' or (previous == 'cancel_requested' and task['apply_requested'])
            if status in {'applied', 'conflict'} and not applying and previous != status:
                raise ChatError(409, 'Applying a task requires the owner request.')
            if status == 'applying' and previous != 'applying':
                raise ChatError(409, 'Applying a task requires the owner request.')
            if task['cancel_requested'] and status in ACTIVE:
                status = 'cancel_requested'
            if task['cancel_requested'] and status in {'completed', 'applied'}:
                event_id = 'cloud-late-cancel-' + identifier
                if not any(event['id'] == event_id for event in task['events']):
                    task['events'].append({'id': event_id, 'type': 'status',
                        'text': 'The worker reported completion after cancellation was requested; the saved result was retained.',
                        'details': {'late_cancel': True}, 'created_at': _stamp(now)})
            task['status'] = status
            if status in {'applied', 'conflict', 'failed', 'cancelled', 'interrupted'}:
                task['apply_requested'] = False
        for field, limit in (('checkpoint', 64 * 1024), ('usage', 32 * 1024)):
            if field in body:
                task[field] = _bounded_object(body[field], limit, field)
        for field, limit in (('result', 64000), ('diff', 128000)):
            if field in body:
                task[field] = _text(body[field], field, limit, empty=True)
        task.update(updated_at=_stamp(now), _lease_until=now + self.lease_seconds)
        if len(_encode(task)) > MAX_TASK_BYTES:
            raise ChatError(507, 'This task has reached its cloud history limit. Its remaining evidence is retained locally.')
        response = self._response(task)
        response.pop('lease')
        return response

    def _infer(self, identifier, body):
        _object(body, ('worker_id', 'lease', 'payload'), ('worker_id', 'lease', 'payload'))
        worker_id = _token(body['worker_id'], 'worker ID')
        payload = _bounded_object(body['payload'], MAX_INFERENCE_BODY - 1024, 'inference payload')
        bedrock = 'inferenceConfig' in payload
        if bedrock:
            _object(payload, ('messages', 'system', 'inferenceConfig', 'toolConfig'), ('messages', 'inferenceConfig'))
        else:
            _object(payload, ('model', 'messages', 'max_tokens', 'temperature', 'stream',
                              'chat_template_kwargs', 'tools', 'tool_choice'), ('messages',))
        messages = payload['messages']
        if not isinstance(messages, list) or not 1 <= len(messages) <= 256:
            raise ChatError(400, 'Send a bounded chat message list.')
        for message in messages:
            roles = ('user', 'assistant') if bedrock else ('system', 'user', 'assistant', 'tool')
            if not isinstance(message, dict) or message.get('role') not in roles:
                raise ChatError(400, 'Unsupported inference message.')
        config = payload['inferenceConfig'] if bedrock else payload
        if not isinstance(config, dict):
            raise ChatError(400, 'Invalid inference configuration.')
        tokens = config.get('maxTokens' if bedrock else 'max_tokens', self.max_output_tokens)
        if type(tokens) is not int or not 1 <= tokens <= self.max_output_tokens:
            raise ChatError(400, 'The inference output token limit was exceeded.')
        temperature = config.get('temperature', 0)
        if type(temperature) not in (int, float) or not 0 <= temperature <= 2:
            raise ChatError(400, 'Invalid inference temperature.')
        if not bedrock and payload.get('stream', False) is not False:
            raise ChatError(400, 'Streaming inference is not supported by the worker relay.')
        if bedrock:
            payload = {**payload, 'inferenceConfig': {**config, 'maxTokens': tokens, 'temperature': temperature}}
        else:
            payload = {**payload, 'max_tokens': tokens, 'stream': False,
                       'chat_template_kwargs': {'enable_thinking': False}}
        def validate(state, now):
            task = self._task(state, identifier)
            self._lease(task, now, worker_id, body['lease'])
            if task['status'] != 'running' or task['cancel_requested']:
                raise ChatError(409, 'This task is not accepting new inference steps.')
            task['_lease_until'] = now + self.lease_seconds
        self._transaction(validate)
        try:
            response = self.inference(payload)
            if not isinstance(response, dict) or len(_encode(response)) > MAX_INFERENCE_RESPONSE:
                raise ValueError('Invalid inference response')
        except Exception:
            raise ChatError(502, 'Model inference failed or timed out; its usage may be unavailable.') from None
        return response
