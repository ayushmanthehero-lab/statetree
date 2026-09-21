"""Local engine integration tests: real Strands loop, deterministic model only."""

import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tests.helpers import ScriptedModel, git, init_repo

try:
    from statetree.agent import LocalTaskRunner
except ImportError:
    LocalTaskRunner = None


TASK = {'id': '1' * 32, 'project_id': '2' * 32, 'prompt': 'Replace initial with fixed and verify the file.'}


def call(name, arguments, identifier='tool1'):
    return {'id': identifier, 'tool': name, 'input': arguments}


class AgentRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = init_repo(self.root / 'project')
        self.state = self.root / 'state'
        self.assertIsNotNone(LocalTaskRunner, 'The local task engine must exist')

    def tearDown(self):
        self.temporary.cleanup()

    def runner(self, responses, **kwargs):
        self.model = ScriptedModel(responses)
        return LocalTaskRunner(self.project, self.state, lambda: self.model, **kwargs)

    def task_files(self):
        return self.state / 'tasks' / TASK['id'] / 'repo' / 'files'

    def test_real_tool_loop_preserves_original_and_reports_diff_and_usage(self):
        events = []
        result = self.runner([
            call('read_file', {'path': 'file.txt'}),
            call('edit_file', {'path': 'file.txt', 'old': 'initial', 'new': 'fixed'}, 'tool2'),
            'Updated the file.',
        ], event_sink=events.append).run(TASK)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual((self.project / 'file.txt').read_text(), 'initial')
        self.assertEqual((self.task_files() / 'file.txt').read_text(), 'fixed')
        self.assertIn('-initial', result['diff'])
        self.assertIn('+fixed', result['diff'])
        self.assertEqual(result['usage']['requests'], 3)
        self.assertEqual(result['usage']['input_tokens'], 300)
        self.assertTrue(result['checkpoint']['id'])
        self.assertTrue(any(e['type'] == 'tool_result' for e in events))

    def test_apply_preserves_index_and_rejects_changed_original(self):
        runner = self.runner([call('edit_file', {'path': 'file.txt', 'old': 'initial', 'new': 'fixed'}), 'Done'])
        runner.run(TASK)
        (self.project / 'file.txt').write_text('user change')
        result = runner.apply(TASK['id'])
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual((self.project / 'file.txt').read_text(), 'user change')
        (self.project / 'file.txt').write_text('initial')
        staged = git(self.project, 'diff', '--cached')
        self.assertEqual(runner.apply(TASK['id'])['status'], 'applied')
        self.assertEqual((self.project / 'file.txt').read_text(), 'fixed')
        self.assertEqual(git(self.project, 'diff', '--cached'), staged)
        self.assertEqual(runner.apply(TASK['id'])['status'], 'applied')

    def test_completed_task_is_returned_without_another_model_call(self):
        first = self.runner(['Finished.']).run(TASK)
        runner = self.runner([RuntimeError('must not invoke')])
        second = runner.run(TASK)
        self.assertEqual(first, second)
        self.assertEqual(self.model.requests, [])

    def test_resume_after_confirmed_edit_does_not_repeat_effect(self):
        first = self.runner([call('edit_file', {'path': 'file.txt', 'old': 'initial', 'new': 'fixed'}), RuntimeError('offline')]).run(TASK)
        self.assertEqual(first['status'], 'failed')
        self.assertEqual((self.task_files() / 'file.txt').read_text(), 'fixed')
        events = []
        resumed = self.runner([call('edit_file', {'path': 'file.txt', 'old': 'initial', 'new': 'fixed'}), 'Done'], event_sink=events.append).run(TASK)
        self.assertEqual(resumed['status'], 'completed', resumed)
        self.assertEqual((self.task_files() / 'file.txt').read_text(), 'fixed')
        self.assertTrue(any(e['details'].get('replayed') for e in events if e['type'] == 'tool_result'))
        self.assertIn('Confirmed', json.dumps(self.model.requests[0]))

    def test_paths_and_private_files_are_not_exposed_or_changed(self):
        (self.project / '.env').write_text('TOP_SECRET=not-for-model')
        (self.project / 'secret.json').write_text('{"key":"not-for-model"}')
        (self.root / 'outside.txt').write_text('outside')
        result = self.runner([
            call('read_file', {'path': '../outside.txt'}),
            call('read_file', {'path': '.env'}, 'tool2'),
            call('write_file', {'path': '../escape.txt', 'content': 'bad'}, 'tool3'),
            'Done',
        ]).run(TASK)
        self.assertEqual(result['status'], 'completed', result)
        self.assertFalse((self.state / 'tasks' / TASK['id'] / 'repo' / 'escape.txt').exists())
        self.assertFalse((self.task_files() / '.env').exists())
        self.assertFalse((self.task_files() / 'secret.json').exists())
        self.assertNotIn('not-for-model', json.dumps(self.model.requests))
        self.assertIn('error', json.dumps(self.model.requests).lower())

    def test_task_id_cannot_be_reused_for_another_prompt(self):
        original = self.runner(['Done']).run(TASK)
        result = self.runner(['Must not execute']).run({**TASK, 'prompt': 'Different task'})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.model.requests, [])
        self.assertEqual(self.runner([RuntimeError('must not run')]).run(TASK), original)

    def test_cancellation_after_model_prevents_tool_effect(self):
        cancelled = [False]
        def events(event):
            if event['type'] == 'checkpoint' and event['details'].get('position') == 'after_model':
                cancelled[0] = True
        result = self.runner([call('edit_file', {'path': 'file.txt', 'old': 'initial', 'new': 'fixed'})],
                             event_sink=events, cancel_requested=lambda: cancelled[0]).run(TASK)
        self.assertEqual(result['status'], 'cancelled', result)
        self.assertEqual((self.task_files() / 'file.txt').read_text(), 'initial')

    def test_readonly_review_is_real_and_metered(self):
        models = iter([ScriptedModel([call('delegate_review', {'question': 'Inspect file.txt.'}), 'Done']),
                       ScriptedModel([call('read_file', {'path': 'file.txt'}), 'The file contains initial.'])])
        events = []
        result = LocalTaskRunner(self.project, self.state, lambda: next(models), event_sink=events.append).run(TASK)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['usage']['requests'], 4)
        self.assertTrue(any(e['type'] == 'agent' for e in events))

    def test_new_process_resumes_checkpoint_after_abrupt_exit(self):
        script = r'''
import json, os, sys
from statetree.agent import LocalTaskRunner
from tests.helpers import ScriptedModel
task=json.loads(sys.argv[3])
model=ScriptedModel([{'id':'edit1','tool':'edit_file','input':{'path':'file.txt','old':'initial','new':'fixed'}},'Done'])
def events(event):
    if event['type']=='checkpoint' and event['details'].get('position')=='after_tools':
        os._exit(23)
LocalTaskRunner(sys.argv[1],sys.argv[2],lambda:model,event_sink=events).run(task)
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.project), str(self.state), json.dumps(TASK)],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 23, result.stderr.decode())
        resumed = self.runner(['Resumed from the saved edit.']).run(TASK)
        self.assertEqual(resumed['status'], 'completed', resumed)
        self.assertEqual((self.task_files() / 'file.txt').read_text(), 'fixed')
        self.assertEqual((self.project / 'file.txt').read_text(), 'initial')
        self.assertEqual(resumed['usage']['requests'], 2)
        self.assertIn('Confirmed', json.dumps(self.model.requests[0]))

    def test_followup_inherits_unapplied_files_and_relevant_note(self):
        self.runner([call('edit_file', {'path': 'file.txt', 'old': 'initial', 'new': 'fixed'}), 'Replaced initial with fixed.']).run(TASK)
        child = {**TASK, 'id': '3' * 32, 'parent_task_id': TASK['id'], 'prompt': 'Inspect the fixed file from the earlier task.'}
        result = self.runner([call('read_file', {'path': 'file.txt'}), 'The inherited file is fixed.']).run(child)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual((self.state / 'tasks' / child['id'] / 'repo/files/file.txt').read_text(), 'fixed')
        self.assertEqual((self.project / 'file.txt').read_text(), 'initial')
        self.assertIn('Replaced initial with fixed', json.dumps(self.model.requests[0]))
        self.assertIn('+fixed', result['diff'])

    def test_pending_command_is_stopped_and_never_replayed(self):
        from statetree.agent.journal import Journal
        self.runner([RuntimeError('offline')]).run(TASK)
        journal = Journal(self.state / 'tasks' / TASK['id'] / 'journal.sqlite3')
        journal.begin('uncertain', 'command', {'container': 'statetree-test-uncertain',
                                             'tool': 'run_command', 'arguments': {'command': 'python job.py'}})
        class DockerResult:
            returncode = 0
            stderr = b''
        runner = self.runner([RuntimeError('must not invoke')])
        with patch('statetree.agent.runner.subprocess.run', return_value=DockerResult()) as docker:
            result = runner.run(TASK)
        self.assertEqual(result['status'], 'waiting_for_input', result)
        self.assertEqual(self.model.requests, [])
        self.assertIsNone(journal.step('uncertain')['receipt'])
        self.assertEqual(docker.call_args.args[0][1:], ['rm', '-f', 'statetree-test-uncertain'])

    def test_link_created_after_snapshot_is_rejected_by_file_tool(self):
        from statetree.agent.files import SafeFiles, UnsafePath
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'private.txt').write_text('not-for-model')
        self.runner([RuntimeError('offline')]).run(TASK)
        link = self.task_files() / 'escape'
        if os.name == 'nt':
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        else:
            link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises((UnsafePath, OSError)):
            SafeFiles(self.task_files()).read('escape/private.txt')

    def test_project_process_lock_prevents_a_second_worker(self):
        from statetree.agent.files import project_lock
        runner = self.runner(['Must not run'])
        lock = self.state / ('project-' + runner.project_key + '.lock')
        script = r'''
import json,sys
from statetree.agent import LocalTaskRunner
from tests.helpers import ScriptedModel
print(json.dumps(LocalTaskRunner(sys.argv[1],sys.argv[2],lambda:ScriptedModel(['bad'])).run(json.loads(sys.argv[3]))))
'''
        with project_lock(lock):
            child = subprocess.run([sys.executable, '-B', '-c', script, str(self.project), str(self.state), json.dumps(TASK)], capture_output=True, timeout=20)
        self.assertEqual(child.returncode, 0, child.stderr.decode())
        self.assertEqual(json.loads(child.stdout)['status'], 'interrupted')
        self.assertFalse(self.task_files().exists())

    def test_crash_during_inference_records_unknown_usage_on_resume(self):
        from statetree.agent.journal import Journal
        self.runner([RuntimeError('offline')]).run(TASK)
        journal = Journal(self.state / 'tasks' / TASK['id'] / 'journal.sqlite3')
        known_before = journal.get('usage')['requests']
        journal.begin('lost-model-call', 'inference', {'requests_before': known_before})
        result = self.runner(['Resumed']).run(TASK)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['usage']['requests'], known_before + 2)
        self.assertGreaterEqual(result['usage']['unknown_usage_requests'], 1)

    def test_command_uses_only_docker_and_refuses_missing_local_image(self):
        class MissingImage:
            returncode = 1
            stderr = b'not installed'
        runner = self.runner([call('run_command', {'command': 'python -V'}), 'No test ran because the image was unavailable.'])
        real_run = subprocess.run
        docker_calls = []
        def command(arguments, **kwargs):
            if Path(arguments[0]).name.lower() in ('docker', 'docker.exe'):
                docker_calls.append(arguments)
                return MissingImage()
            return real_run(arguments, **kwargs)
        with patch('statetree.agent.runner.subprocess.run', side_effect=command):
            result = runner.run(TASK)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(len(docker_calls), 1)
        self.assertEqual(docker_calls[0][1:3], ['image', 'inspect'])
        self.assertIn('unavailable', json.dumps(self.model.requests).lower())

    def test_project_attributes_cannot_enable_host_git_filters(self):
        configuration = self.root / 'git-config'
        configuration.write_text('[filter "unsafe"]\n\tclean = echo FILTER_RAN\n')
        (self.project / '.gitattributes').write_text('*.txt filter=unsafe\n')
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(configuration)}):
            result = self.runner(['Done']).run(TASK)
        self.assertEqual(result['status'], 'completed', result)
        repository = self.task_files().parent
        self.assertEqual(git(repository, 'show', 'HEAD:files/file.txt'), 'initial')

    def test_cancel_acknowledged_at_tool_start_prevents_the_edit(self):
        cancelled = [False]
        def event_sink(event):
            if event['type'] == 'tool_start':
                cancelled[0] = True
        result = self.runner([call('edit_file', {'path': 'file.txt', 'old': 'initial', 'new': 'fixed'})],
                             event_sink=event_sink, cancel_requested=lambda: cancelled[0]).run(TASK)
        self.assertEqual(result['status'], 'cancelled', result)
        self.assertEqual((self.task_files() / 'file.txt').read_text(), 'initial')
        resumed = self.runner([call('edit_file', {'path': 'file.txt', 'old': 'initial', 'new': 'fixed'}), 'Done']).run(TASK)
        self.assertEqual(resumed['status'], 'completed', resumed)
        self.assertEqual((self.task_files() / 'file.txt').read_text(), 'fixed')

    def test_many_large_read_receipts_keep_resume_context_bounded(self):
        (self.project / 'large.txt').write_text('important line\n' * 400)
        replies = [call('read_file', {'path': 'large.txt'}, 'read' + str(i)) for i in range(6)]
        result = self.runner([*replies, 'Read the available evidence.']).run(TASK)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['usage']['requests'], 7)

    def test_prompt_limit_counts_utf8_bytes_before_model_use(self):
        result = self.runner(['Must not run']).run({**TASK, 'prompt': '\u20ac' * 800})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.model.requests, [])

    @unittest.skipUnless(os.environ.get('STATETREE_TEST_DOCKER') == '1', 'Explicit local Docker boundary probe')
    def test_real_docker_has_only_task_files_and_no_network_or_credentials(self):
        code = ('import os,socket,pathlib; '
                'assert not pathlib.Path("/workspace/.git").exists(); '
                'assert not pathlib.Path("/var/run/docker.sock").exists(); '
                'assert not os.environ.get("AWS_SECRET_ACCESS_KEY"); '
                's=socket.socket();s.settimeout(0.5); '
                'assert s.connect_ex(("1.1.1.1",443)) != 0; '
                'pathlib.Path("sandbox-result.txt").write_text("sandboxed"); '
                'print("ISOLATION_CHECK_PASSED")')
        command = "python -c '" + code + "'"
        result = self.runner([call('run_command', {'command': command}), 'Checked container boundaries.']).run(TASK)
        self.assertEqual(result['status'], 'completed', result)
        self.assertIn('ISOLATION_CHECK_PASSED', json.dumps(self.model.requests))
        self.assertEqual((self.task_files() / 'sandbox-result.txt').read_text(), 'sandboxed')
        self.assertFalse((self.project / 'sandbox-result.txt').exists())

    @unittest.skipUnless(os.environ.get('STATETREE_TEST_DOCKER') == '1', 'Explicit Docker crash-recovery probe')
    def test_killed_command_worker_fences_project_and_stops_container_without_replay(self):
        from statetree.agent.journal import Journal
        script = r'''
import json,sys,os,pathlib
from statetree.agent import LocalTaskRunner
from tests.helpers import ScriptedModel
pathlib.Path(sys.argv[4]).write_text(str(os.getpid()))
m=ScriptedModel([{'id':'sleep','tool':'run_command','input':{'command':'sleep 30'}},'done'])
LocalTaskRunner(sys.argv[1],sys.argv[2],lambda:m).run(json.loads(sys.argv[3]))
'''
        pid_path = self.root / 'worker.pid'
        child = subprocess.Popen([sys.executable, '-B', '-c', script, str(self.project), str(self.state), json.dumps(TASK), str(pid_path)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        runner = self.runner([RuntimeError('must not invoke')])
        container = None
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                journal_path = self.state / 'tasks' / TASK['id'] / 'journal.sqlite3'
                if journal_path.exists():
                    commands = [step for step in Journal(journal_path).steps() if step['kind'] == 'command']
                    if commands:
                        container = commands[-1]['intent']['container']
                        check = subprocess.run([runner.docker, 'inspect', '--format', '{{.State.Running}}', container], capture_output=True, timeout=5)
                        if check.returncode == 0 and check.stdout.strip() == b'true':
                            break
                time.sleep(0.1)
            else:
                self.fail('The isolated command did not start within the probe deadline.')
            os.kill(int(pid_path.read_text()), signal.SIGTERM)
            child.wait(timeout=10)
            fresh = runner.run({**TASK, 'id': 'e' * 32})
            self.assertEqual(fresh['status'], 'waiting_for_input', fresh)
            resumed = runner.run(TASK)
            self.assertEqual(resumed['status'], 'waiting_for_input', resumed)
            self.assertEqual(self.model.requests, [])
            self.assertIn('uncertain outcome', resumed['result'])
            inspect = subprocess.run([runner.docker, 'inspect', container], capture_output=True, timeout=5)
            self.assertNotEqual(inspect.returncode, 0)
            commands = [step for step in Journal(journal_path).steps() if step['kind'] == 'command']
            self.assertIsNone(commands[-1]['receipt'])
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            if container:
                subprocess.run([runner.docker, 'rm', '-f', container], capture_output=True, timeout=10)

    def test_archive_tool_evidence_survives_compaction(self):
        first = self.runner([call('read_file', {'path': 'file.txt'}), RuntimeError('offline')]).run(TASK)
        archive_id = first['checkpoint']['evidence_id']
        result = self.runner([call('statetree_read_archive', {'archive_id': archive_id, 'limit': 1000}), 'Used the retained evidence.']).run(TASK)
        self.assertEqual(result['status'], 'completed', result)
        self.assertIn('initial', json.dumps(self.model.requests[-1]))
        self.assertIn('statetree_read_archive', json.dumps(self.model.requests[-1]))

    def test_project_fence_blocks_a_fresh_task_and_apply(self):
        first = self.runner(['Finished']).run(TASK)
        runner = self.runner([RuntimeError('must not run')])
        runner._fence().set('blocked_task', 'f' * 32)
        second = runner.run({**TASK, 'id': 'e' * 32})
        self.assertEqual(second['status'], 'waiting_for_input', second)
        self.assertEqual(self.model.requests, [])
        self.assertFalse((self.state / 'tasks' / ('e' * 32)).exists())
        self.assertEqual(runner.apply(TASK['id'])['status'], 'conflict')
        self.assertEqual((self.project / 'file.txt').read_text(), 'initial')

    def test_safe_delete_requires_expected_content_and_removes_only_target(self):
        import hashlib
        from statetree.agent.files import SafeFiles
        files = SafeFiles(self.project)
        with self.assertRaises(ValueError):
            files.delete('file.txt', '0' * 64)
        self.assertEqual((self.project / 'file.txt').read_text(), 'initial')
        files.delete('file.txt', hashlib.sha256(b'initial').hexdigest())
        self.assertFalse((self.project / 'file.txt').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows handle sharing guarantee')
    def test_safe_write_holds_a_handle_that_excludes_other_writers(self):
        import hashlib
        from statetree.agent.files import SafeFiles
        real_sync = os.fsync
        blocked = []
        def competing_write(descriptor):
            try:
                (self.project / 'file.txt').write_text('other writer')
            except PermissionError:
                blocked.append(True)
            real_sync(descriptor)
        with patch('statetree.agent.files.os.fsync', side_effect=competing_write):
            SafeFiles(self.project).write('file.txt', b'fixed', hashlib.sha256(b'initial').hexdigest())
        self.assertEqual(blocked, [True])
        self.assertEqual((self.project / 'file.txt').read_text(), 'fixed')


if __name__ == '__main__':
    unittest.main()
