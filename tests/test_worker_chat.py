"""Real loopback HTTP integration; model and filesystem execution are deterministic."""
import asyncio
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

from deploy.local_agent import EventOutbox, LocalWorker, WorkerClient, WorkerError
from statetree.web.app import Application, MemoryReports, make_server
from statetree.web.chat import ChatApplication, FileChatStore


OWNER = 'o' * 48
WORKER = 'w' * 48
WORKER_ID = 'a' * 32


class WorkerChatTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.inferences = []
        def inference(payload):
            self.inferences.append(payload)
            return {'output': {'message': {'role': 'assistant', 'content': [{'text': 'checked'}]}},
                'stopReason': 'end_turn',
                'usage': {'inputTokens': 12, 'outputTokens': 3, 'totalTokens': 15}}
        self.chat = ChatApplication(OWNER, WORKER, FileChatStore(self.root / 'chat.sqlite'), inference)
        self.app = Application('demo-access-code', lambda: {}, MemoryReports(), chat=self.chat)
        self.server = make_server(self.app, '127.0.0.1', 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = WorkerClient('http://127.0.0.1:' + str(self.server.server_port), WORKER, WORKER_ID)
        self.result = {'status': 'completed', 'result': 'fixed', 'checkpoint': {'note': 'receipt saved'},
                       'usage': {'inputTokens': 12, 'outputTokens': 3}, 'diff': 'sample patch'}
        self.run_tasks, self.applied_tasks = [], []
        self.emit_checkpoint = False
        test = self
        class Runner:
            def __init__(self, project_path, state_root, model_factory, **kwargs):
                self.model_factory, self.sink = model_factory, kwargs['event_sink']
            def run(self, task):
                test.run_tasks.append(task)
                if test.emit_checkpoint:
                    self.sink({'id': 'live-checkpoint', 'type': 'checkpoint', 'text': 'Saved checkpoint',
                               'details': {'id': 'checkpoint-one', 'position': 'after_tools'}})
                    test.during_run_checkpoint = test.owner('/api/chat/tasks/' + task['id'])['task']['checkpoint']
                async def infer():
                    return [event async for event in self.model_factory().stream(
                        [{'role': 'user', 'content': [{'text': task['prompt']}]}])]
                response = asyncio.run(infer())
                self.sink({'id': 'edit-' + str(len(test.run_tasks)), 'type': 'tool_result',
                           'text': 'saved file', 'details': {'receipt': 'edit-one'}})
                test.assertTrue(any(event.get('metadata', {}).get('usage', {}).get('totalTokens') == 15
                                    for event in response))
                return test.result
            def apply(self, task_id):
                test.applied_tasks.append(task_id)
                return {'status': 'applied', 'result': 'Changes applied', 'checkpoint': None, 'usage': None}
        self.runner_factory = Runner
        self.worker = self.new_worker()
        self.assertFalse(self.worker.run_once())

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.directory.cleanup()

    def new_worker(self):
        return LocalWorker(self.client, self.project, self.root / 'worker', runner_factory=self.runner_factory)

    def owner(self, path, payload=None):
        status, response = self.chat.dispatch('GET' if payload is None else 'POST', path,
            {'X-Owner-Key': OWNER}, b'' if payload is None else json.dumps(payload).encode())
        self.assertEqual(status, 200, response)
        return response

    def submit(self):
        return self.owner('/api/chat/tasks', {'project_id': self.worker.project_id, 'prompt': 'Fix the test'})

    def execute(self, worker=None):
        with redirect_stdout(io.StringIO()):
            return (worker or self.worker).run_once()

    def test_worker_executes_real_http_model_relay_events_and_owner_apply(self):
        task = self.submit()['task']
        self.assertTrue(self.execute())
        saved = self.owner('/api/chat/tasks/' + task['id'])['task']
        self.assertEqual(saved['status'], 'completed')
        self.assertEqual(saved['events'][0]['details'], {'receipt': 'edit-one'})
        self.assertEqual(saved['usage']['inputTokens'], 12)
        self.assertEqual(self.inferences[0]['inferenceConfig']['maxTokens'], 256)
        self.assertFalse(self.execute())
        self.owner('/api/chat/tasks/' + task['id'] + '/apply', {})
        self.assertTrue(self.execute())
        self.assertEqual(self.applied_tasks, [task['id']])
        self.assertEqual(self.owner('/api/chat/tasks/' + task['id'])['task']['status'], 'applied')
        self.assertFalse(self.execute())

    def test_new_worker_resumes_same_task_and_receives_persisted_checkpoint(self):
        task = self.submit()['task']
        self.result['status'] = 'interrupted'
        self.assertTrue(self.execute())
        repeated = self.submit()
        self.assertTrue(repeated['resumed'])
        self.assertEqual(repeated['task']['id'], task['id'])
        self.result['status'] = 'completed'
        self.assertTrue(self.execute(self.new_worker()))
        self.assertEqual(self.run_tasks[1]['checkpoint'], {'note': 'receipt saved'})
        self.assertEqual(self.run_tasks[1]['resume_count'], 1)
        saved = self.owner('/api/chat/tasks/' + task['id'])['task']
        self.assertEqual([event['id'] for event in saved['events']], ['edit-1', 'edit-2'])
        self.assertEqual(self.submit()['task']['status'], 'completed')

    def test_buffered_events_over_one_batch_flush_after_worker_restart(self):
        task = self.submit()['task']
        outbox = EventOutbox(self.root / 'worker' / 'outbox' / (task['id'] + '.json'))
        for index in range(125):
            outbox.add({'id': 'saved-' + str(index), 'type': 'status', 'text': 'persisted local step'})
        self.assertTrue(self.execute(self.new_worker()))
        events = self.owner('/api/chat/tasks/' + task['id'])['task']['events']
        self.assertEqual(len(events), 126)
        self.assertEqual(len({event['id'] for event in events}), 126)
        self.assertEqual(EventOutbox(outbox.path).pending(), [])

    def test_terminal_task_heartbeat_is_idempotent_and_does_not_start_work(self):
        task = self.submit()['task']
        claim = self.worker._poll()
        self.client.post('/api/worker/tasks/' + task['id'] + '/events', {
            'worker_id': WORKER_ID, 'lease': claim['lease'], 'events': [], 'status': 'completed'})
        late = self.worker._poll(task, claim['lease'])
        self.assertEqual(late['task']['status'], 'completed')
        self.assertFalse(self.execute())
        self.assertEqual(self.run_tasks, [])

    def test_cancellation_after_local_completion_cannot_leave_task_claimed_forever(self):
        task = self.submit()['task']
        post = self.client.post
        def cancel_before_completion_ack(path, payload):
            if payload.get('status') == 'completed':
                self.owner('/api/chat/tasks/' + task['id'] + '/cancel', {})
            return post(path, payload)
        self.client.post = cancel_before_completion_ack
        self.assertTrue(self.execute())
        saved = self.owner('/api/chat/tasks/' + task['id'])['task']
        self.assertEqual(saved['status'], 'completed')
        self.assertTrue(saved['cancel_requested'])
        self.assertTrue(any(event['details'].get('late_cancel') for event in saved['events']))
        self.assertFalse(self.execute())
        self.assertEqual(len(self.run_tasks), 1)

    def test_checkpoint_is_visible_before_the_runner_finishes(self):
        self.emit_checkpoint = True
        self.submit()
        self.assertTrue(self.execute())
        self.assertEqual(self.during_run_checkpoint, {'id': 'checkpoint-one', 'position': 'after_tools'})


if __name__ == '__main__':
    unittest.main()
