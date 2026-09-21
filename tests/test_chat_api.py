import http.client
import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from statetree.web import chat
from statetree.web.app import Application, MemoryReports, Settings, create_application, make_server


OWNER = 'private-owner-key-123456'
WORKER = 'private-worker-key-123456'
PROJECT = 'a' * 32


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = chat.MemoryChatStore()
        self.calls = []
        def infer(payload):
            self.calls.append(payload)
            return {'choices': [{'message': {'role': 'assistant', 'content': 'done'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}}
        self.app = chat.ChatApplication(OWNER, WORKER, self.store, infer, clock=self.clock, lease_seconds=90)

    def request(self, path, body=None, *, worker=False, key=None, app=None):
        header = 'X-Worker-Key' if worker else 'X-Owner-Key'
        key = (WORKER if worker else OWNER) if key is None else key
        return (app or self.app).dispatch('GET' if body is None else 'POST', path, {header: key},
                                         b'' if body is None else json.dumps(body).encode())

    def poll(self, worker_id='worker-a', **extra):
        return self.request('/api/worker/poll', {'worker_id': worker_id,
            'projects': [{'id': PROJECT, 'name': 'sample'}], **extra}, worker=True)

    def task(self, **extra):
        self.poll()
        code, body = self.request('/api/chat/tasks', {'project_id': PROJECT, 'prompt': 'Fix the test', **extra})
        self.assertEqual(code, 200, body)
        return body['task']

    def claimed(self):
        task = self.task()
        code, claim = self.poll()
        self.assertEqual(code, 200)
        self.assertEqual(claim['task']['id'], task['id'])
        return task, claim

    def events(self, task, claim, **extra):
        return self.request('/api/worker/tasks/' + task['id'] + '/events', {
            'worker_id': 'worker-a', 'lease': claim['lease'], 'events': [], **extra}, worker=True)

    def test_roles_are_separate_and_private_state_never_uses_demo_access(self):
        for key in ('', WORKER, 'test-demo-code'):
            self.assertEqual(self.request('/api/chat/state', key=key)[0], 401)
        self.assertEqual(self.request('/api/worker/poll', {}, worker=True, key=OWNER)[0], 401)
        self.assertEqual(self.request('/api/chat/state?key=' + OWNER)[0], 404)
        self.assertEqual(self.request('/api/chat/state')[1], {'projects': [], 'tasks': []})

    def test_same_prompt_attaches_and_fresh_creates_new_task(self):
        first = self.task()
        code, second = self.request('/api/chat/tasks', {'project_id': PROJECT, 'prompt': '  Fix  the\r\n test  '})
        self.assertEqual(code, 200)
        self.assertEqual(second['task']['id'], first['id'])
        self.assertFalse(second['resumed'])
        fresh = self.task(fresh=True)
        self.assertNotEqual(first['id'], fresh['id'])

    def test_expired_lease_interrupts_without_replay_and_explicit_resume_keeps_checkpoint(self):
        task, claim = self.claimed()
        self.assertEqual(self.events(task, claim, checkpoint={'note': 'edit saved'})[0], 200)
        self.clock.now += 91
        code, poll = self.poll('worker-b')
        self.assertEqual(code, 200)
        self.assertIsNone(poll['task'])
        saved = self.request('/api/chat/tasks/' + task['id'])[1]['task']
        self.assertEqual(saved['status'], 'interrupted')
        self.assertEqual(self.events(task, claim, status='completed')[0], 409)
        code, resumed = self.request('/api/chat/tasks', {'project_id': PROJECT, 'prompt': 'Fix the test'})
        self.assertEqual(code, 200)
        self.assertTrue(resumed['resumed'])
        self.assertEqual(resumed['task']['id'], task['id'])
        self.assertEqual(resumed['task']['resume_count'], 1)
        self.assertEqual(resumed['task']['checkpoint'], {'note': 'edit saved'})
        new_claim = self.poll()[1]
        self.assertNotEqual(new_claim['lease'], claim['lease'])
        self.assertEqual(self.events(task, claim, status='completed')[0], 409)

    def test_heartbeat_extends_lease_and_does_not_claim_another_task(self):
        task, claim = self.claimed()
        self.task(fresh=True)
        self.clock.now += 70
        code, heartbeat = self.poll(active_task_id=task['id'], lease=claim['lease'])
        self.assertEqual(code, 200)
        self.assertEqual(heartbeat['task']['id'], task['id'])
        self.clock.now += 70
        self.assertEqual(self.events(task, claim, result='still alive')[0], 200)

    def test_events_retry_deduplicates_and_cannot_replace_an_existing_event(self):
        task, claim = self.claimed()
        event = {'id': 'edit-one', 'type': 'tool_result', 'text': 'wrote file', 'details': {'ok': True},
                 'created_at': '2026-09-20T12:00:00Z'}
        self.assertEqual(self.events(task, claim, events=[event])[0], 200)
        self.assertEqual(self.events(task, claim, events=[event])[0], 200)
        self.assertEqual(self.events(task, claim, events=[{**event, 'text': 'forged'}])[0], 409)
        saved = self.request('/api/chat/tasks/' + task['id'])[1]['task']
        self.assertEqual([e['text'] for e in saved['events'] if e['id'] == 'edit-one'], ['wrote file'])

    def test_completed_repeated_prompt_returns_result_and_apply_requires_owner(self):
        task, claim = self.claimed()
        self.assertEqual(self.events(task, claim, status='completed', result='fixed', diff='diff --git a/x b/x')[0], 200)
        again = self.task()
        self.assertEqual(again['id'], task['id'])
        self.assertEqual(again['result'], 'fixed')
        apply_path = '/api/chat/tasks/' + task['id'] + '/apply'
        self.assertEqual(self.request(apply_path, {}, key=WORKER)[0], 401)
        self.assertEqual(self.request(apply_path, {})[0], 200)
        apply_claim = self.poll()[1]
        self.assertEqual(apply_claim['task']['id'], task['id'])
        self.assertTrue(apply_claim['apply_requested'])
        self.assertEqual(self.events(task, apply_claim, status='applied')[0], 200)
        self.assertFalse(self.request('/api/chat/tasks/' + task['id'])[1]['task']['apply_requested'])

    def test_cancel_is_delivered_and_worker_cannot_erase_owner_intent(self):
        task, claim = self.claimed()
        self.assertEqual(self.request('/api/chat/tasks/' + task['id'] + '/cancel', {})[0], 200)
        response = self.events(task, claim, status='running')
        self.assertEqual(response[0], 200)
        self.assertTrue(response[1]['cancel_requested'])
        self.assertEqual(response[1]['task']['status'], 'cancel_requested')
        self.assertEqual(self.events(task, claim, status='cancelled')[0], 200)
        code, resumed = self.request('/api/chat/tasks', {'project_id': PROJECT, 'prompt': 'Fix the test'})
        self.assertTrue(resumed['resumed'])
        self.assertFalse(resumed['task']['cancel_requested'])

    def test_cancel_racing_finished_apply_accepts_the_durable_receipt(self):
        task, claim = self.claimed()
        self.events(task, claim, status='completed')
        self.request('/api/chat/tasks/' + task['id'] + '/apply', {})
        claim = self.poll()[1]
        self.request('/api/chat/tasks/' + task['id'] + '/cancel', {})
        code, result = self.events(task, claim, status='applied', result='Applied before cancellation arrived')
        self.assertEqual(code, 200)
        self.assertEqual(result['task']['status'], 'applied')
        self.assertFalse(result['apply_requested'])
        self.assertIsNone(self.poll()[1]['task'])

    def test_unregistered_project_and_unknown_fields_are_rejected(self):
        self.assertEqual(self.request('/api/chat/tasks', {'project_id': PROJECT, 'prompt': 'hi'})[0], 404)
        self.poll()
        for extra in ({'project_path': 'C:/secret'}, {'fresh': 'false'}, {'lease': 'fake'}):
            self.assertEqual(self.request('/api/chat/tasks', {'project_id': PROJECT, 'prompt': 'hi', **extra})[0], 400)
        self.assertEqual(self.request('/api/worker/poll', {'worker_id': 'worker-a',
            'projects': [{'id': PROJECT, 'name': 'sample', 'path': 'C:/secret'}]}, worker=True)[0], 400)
        self.assertEqual(self.request('/api/chat/tasks', {'project_id': PROJECT, 'prompt': 'x' * 20000})[0], 400)

    def test_prompt_limit_counts_utf8_bytes_so_unicode_cannot_exhaust_context(self):
        self.poll()
        code, _ = self.request('/api/chat/tasks', {'project_id': PROJECT, 'prompt': '\u00e9' * 1000})
        self.assertEqual(code, 200)
        code, _ = self.request('/api/chat/tasks', {'project_id': PROJECT, 'prompt': '\u00e9' * 1001})
        self.assertEqual(code, 400)

    def test_duplicate_json_fields_nonfinite_numbers_and_huge_bodies_are_rejected(self):
        headers = {'X-Owner-Key': OWNER}
        for raw in (b'{"prompt":"one","prompt":"two"}', b'{"prompt":NaN}', b'[' * 1000 + b']' * 1000):
            self.assertEqual(self.app.dispatch('POST', '/api/chat/tasks', headers, raw)[0], 400)
        self.assertEqual(self.app.dispatch('POST', '/api/chat/tasks', headers, b'x' * (chat.MAX_CHAT_BODY + 1))[0], 413)

    def test_task_identity_cannot_override_project_or_prompt(self):
        task = self.task()
        self.assertEqual(self.request('/api/chat/tasks', {'project_id': PROJECT, 'prompt': 'different',
                                                        'task_id': task['id']})[0], 409)
        self.assertEqual(self.request('/api/chat/tasks', {'project_id': PROJECT, 'prompt': 'Fix the test',
                                                        'task_id': task['id'], 'fresh': True})[0], 400)

    def test_owner_views_strip_execution_credentials_and_mark_offline(self):
        task, claim = self.claimed()
        state = self.request('/api/chat/state')[1]
        self.assertTrue(state['projects'][0]['online'])
        self.assertNotIn(claim['lease'], json.dumps(state))
        self.assertNotIn('worker-a', json.dumps(state))
        self.clock.now += 91
        self.assertFalse(self.request('/api/chat/state')[1]['projects'][0]['online'])

    def test_two_api_instances_can_only_create_and_claim_once(self):
        other = chat.ChatApplication(OWNER, WORKER, self.store, lambda p: {}, clock=self.clock)
        self.poll()
        def submit(app):
            return self.request('/api/chat/tasks', {'project_id': PROJECT, 'prompt': 'Fix the test'}, app=app)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, [self.app, other]))
        self.assertEqual({r[1]['task']['id'] for r in results}, {results[0][1]['task']['id']})
        def claim(pair):
            app, worker_id = pair
            return self.request('/api/worker/poll', {'worker_id': worker_id,
                'projects': [{'id': PROJECT, 'name': 'sample'}]}, worker=True, app=app)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, [(self.app, 'worker-a'), (other, 'worker-b')]))
        self.assertEqual(sum(r[1]['task'] is not None for r in results), 1)

    def test_another_worker_cannot_claim_a_second_task_in_a_busy_project(self):
        first, claim = self.claimed()
        second = self.task(fresh=True)
        response = self.poll('worker-b')[1]
        self.assertIsNone(response['task'])
        self.events(first, claim, status='completed')
        response = self.poll('worker-b')[1]
        self.assertEqual(response['task']['id'], second['id'])

    def test_follow_up_tracks_completed_parent_and_has_separate_prompt_identity(self):
        parent, claim = self.claimed()
        self.events(parent, claim, status='completed')
        body = {'project_id': PROJECT, 'prompt': 'Add another check', 'parent_task_id': parent['id']}
        code, followup = self.request('/api/chat/tasks', body)
        self.assertEqual(code, 200)
        self.assertEqual(followup['task']['parent_task_id'], parent['id'])
        again = self.request('/api/chat/tasks', body)[1]
        self.assertEqual(again['task']['id'], followup['task']['id'])
        standalone = self.request('/api/chat/tasks', {'project_id': PROJECT, 'prompt': 'Add another check'})[1]
        self.assertNotEqual(standalone['task']['id'], followup['task']['id'])
        self.assertIsNone(standalone['task']['parent_task_id'])

    def test_follow_up_rejects_unfinished_or_other_project_parent(self):
        parent, claim = self.claimed()
        body = {'project_id': PROJECT, 'prompt': 'Continue', 'parent_task_id': parent['id']}
        self.assertEqual(self.request('/api/chat/tasks', body)[0], 409)
        self.events(parent, claim, status='completed')
        self.request('/api/worker/poll', {'worker_id': 'worker-b', 'projects': [
            {'id': 'b' * 32, 'name': 'other'}]}, worker=True)
        self.assertEqual(self.request('/api/chat/tasks', {**body, 'project_id': 'b' * 32})[0], 409)

    def test_durable_file_store_reloads_and_stale_compare_swap_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'chat.sqlite'
            a = chat.FileChatStore(path)
            b = chat.FileChatStore(path)
            state, revision = a.read()
            state['projects'][PROJECT] = {'name': 'persisted'}
            self.assertTrue(a.compare_and_swap(revision, state))
            self.assertFalse(b.compare_and_swap(revision, {'projects': {}, 'tasks': {}}))
            self.assertEqual(chat.FileChatStore(path).read()[0]['projects'][PROJECT]['name'], 'persisted')

    def test_cas_collision_retries_without_losing_a_task(self):
        class CollisionStore(chat.MemoryChatStore):
            def __init__(self):
                super().__init__()
                self.collisions = 0
            def compare_and_swap(self, revision, state):
                if self.collisions < 2:
                    self.collisions += 1
                    return False
                return super().compare_and_swap(revision, state)
        self.app.store = CollisionStore()
        self.assertEqual(self.task()['status'], 'queued')

    def test_inference_requires_current_lease_and_cannot_exceed_output_limit(self):
        task, claim = self.claimed()
        path = '/api/worker/tasks/' + task['id'] + '/inference'
        body = {'worker_id': 'worker-a', 'lease': claim['lease'],
                'payload': {'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 256}}
        self.assertEqual(self.request(path, body, key=OWNER)[0], 401)
        self.assertEqual(self.request(path, {**body, 'lease': 'bad'}, worker=True)[0], 409)
        oversized = {**body, 'payload': {**body['payload'], 'max_tokens': 257}}
        self.assertEqual(self.request(path, oversized, worker=True)[0], 400)
        self.assertEqual(self.calls, [])
        code, response = self.request(path, body, worker=True)
        self.assertEqual(code, 200)
        self.assertEqual(response['usage']['total_tokens'], 12)
        self.assertEqual(len(self.calls), 1)
        self.request('/api/chat/tasks/' + task['id'] + '/cancel', {})
        self.assertEqual(self.request(path, body, worker=True)[0], 409)
        self.assertEqual(len(self.calls), 1)

    def test_event_history_stops_before_worker_response_size_limit(self):
        task, claim = self.claimed()
        rejected = False
        for batch in range(12):
            events = [{'id': f'event-{batch}-{index}', 'type': 'tool_result', 'text': 'x' * 30000}
                      for index in range(6)]
            code, _ = self.events(task, claim, events=events)
            if code == 507:
                rejected = True
                break
            self.assertEqual(code, 200)
        self.assertTrue(rejected, 'Task events must remain deliverable to the bounded worker transport')
        saved = self.request('/api/chat/tasks/' + task['id'])[1]
        self.assertLess(len(json.dumps(saved).encode()), 2 * 1024 * 1024)

    def test_model_failure_does_not_leak_upstream_details(self):
        task, claim = self.claimed()
        def fail(payload):
            raise RuntimeError('private upstream key must never leak')
        self.app.inference = fail
        response = self.request('/api/worker/tasks/' + task['id'] + '/inference', {
            'worker_id': 'worker-a', 'lease': claim['lease'],
            'payload': {'messages': [{'role': 'user', 'content': 'hi'}]}}, worker=True)
        self.assertEqual(response[0], 502)
        self.assertNotIn('private upstream key', json.dumps(response))

    def test_tasks_and_events_reload_in_a_replacement_application(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'chat.sqlite'
            self.app.store = chat.FileChatStore(path)
            task, claim = self.claimed()
            self.events(task, claim, events=[{'id': 'saved', 'type': 'checkpoint', 'text': 'edit receipt saved'}],
                        checkpoint={'receipt': 'saved'}, status='interrupted')
            self.app = chat.ChatApplication(OWNER, WORKER, chat.FileChatStore(path), lambda p: {}, clock=self.clock)
            restored = self.request('/api/chat/tasks/' + task['id'])[1]['task']
            self.assertEqual(restored['checkpoint'], {'receipt': 'saved'})
            self.assertEqual(restored['events'][0]['text'], 'edit receipt saved')
            self.assertEqual(restored['status'], 'interrupted')


class S3ChatTests(unittest.TestCase):
    def test_private_encrypted_conditional_writes_and_reload(self):
        class Conflict(Exception):
            def __init__(self, code):
                self.response = {'Error': {'Code': code}}
        class S3:
            def __init__(self):
                self.data = None
                self.etag = None
            def get_object(self, **kwargs):
                self.key = kwargs['Key']
                if self.data is None:
                    raise Conflict('NoSuchKey')
                return {'Body': io.BytesIO(self.data), 'ETag': self.etag}
            def put_object(self, **kwargs):
                self.last = kwargs
                if kwargs.get('IfNoneMatch') == '*' and self.data is not None:
                    raise Conflict('PreconditionFailed')
                if 'IfMatch' in kwargs and kwargs['IfMatch'] != self.etag:
                    raise Conflict('ConditionalRequestConflict')
                self.data = kwargs['Body']
                self.etag = '"new-etag"'
                return {'ETag': self.etag}
        s3 = S3()
        store = chat.S3ChatStore('private-bucket', s3)
        state, revision = store.read()
        self.assertTrue(store.compare_and_swap(revision, state))
        self.assertEqual(s3.last['IfNoneMatch'], '*')
        self.assertEqual(s3.last['ServerSideEncryption'], 'AES256')
        self.assertEqual(s3.last['Key'], 'private-chat/state.json')
        self.assertFalse(store.compare_and_swap(revision, state))
        state, revision = chat.S3ChatStore('private-bucket', s3).read()
        self.assertTrue(store.compare_and_swap(revision, state))
        self.assertEqual(s3.last['IfMatch'], '"new-etag"')
        self.assertNotIn('ACL', s3.last)


class ChatHTTPTests(unittest.TestCase):
    def test_local_application_keeps_chat_durable_and_legacy_benchmark_available(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'secret.json'
            secrets = {'demo_access_code': 'legacy-demo-code', 'upstream_api_key': 'local-upstream-key'}
            path.write_text(json.dumps(secrets))
            settings = Settings(local_secret_file=str(path), local_model_url='http://127.0.0.1:18081/invocations')
            legacy = create_application(settings)
            self.assertIsNone(legacy.chat)
            self.assertTrue(legacy.authorized('legacy-demo-code'))
            path.write_text(json.dumps({**secrets, 'owner_access_code': OWNER, 'worker_api_key': WORKER}))
            application = create_application(settings)
            code, _ = application.chat.dispatch('POST', '/api/worker/poll', {'X-Worker-Key': WORKER},
                json.dumps({'worker_id': 'worker-one', 'projects': [{'id': PROJECT, 'name': 'sample'}]}).encode())
            self.assertEqual(code, 200)
            replacement = create_application(settings)
            code, state = replacement.chat.dispatch('GET', '/api/chat/state', {'X-Owner-Key': OWNER})
            self.assertEqual(code, 200)
            self.assertEqual(state['projects'][0]['id'], PROJECT)
            self.assertFalse(replacement.chat.authorized('/api/chat/state', {'X-Owner-Key': 'legacy-demo-code'}))

    def test_application_rejects_reused_benchmark_credential_for_private_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'secret.json'
            path.write_text(json.dumps({'demo_access_code': OWNER, 'upstream_api_key': 'local-upstream-key',
                                        'owner_access_code': OWNER, 'worker_api_key': WORKER}))
            settings = Settings(local_secret_file=str(path), local_model_url='http://127.0.0.1:18081/invocations')
            with self.assertRaises(ValueError):
                create_application(settings)

    def test_handler_routes_authenticates_and_bounds_chat_requests(self):
        app = Application('demo-access-code', lambda: {}, MemoryReports(), chat=chat.ChatApplication(
            OWNER, WORKER, chat.MemoryChatStore(), lambda p: {}))
        server = make_server(app, '127.0.0.1', 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def request(method, path, body=None, headers=None):
            connection = http.client.HTTPConnection(*server.server_address, timeout=3)
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            result = response.status, response.read()
            connection.close()
            return result
        try:
            self.assertEqual(request('GET', '/api/chat/state')[0], 401)
            self.assertEqual(request('GET', '/api/chat/state', headers={'X-Demo-Key': 'demo-access-code'})[0], 401)
            code, body = request('GET', '/api/chat/state', headers={'X-Owner-Key': OWNER})
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body), {'projects': [], 'tasks': []})
            self.assertEqual(request('POST', '/api/chat/tasks', 'x' * (chat.MAX_CHAT_BODY + 1),
                {'X-Owner-Key': OWNER})[0], 413)
            self.assertEqual(request('GET', '/ping')[0], 200)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)


if __name__ == '__main__':
    unittest.main()
