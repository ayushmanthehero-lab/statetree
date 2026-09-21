import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.test_project import repo_at

ROOT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = repo_at(Path(self.temp.name) / 'repo')

    def command(self, *args, okay=True):
        result = subprocess.run([sys.executable, '-m', 'statetree', '--repo', str(self.repo), *args],
                                cwd=ROOT, capture_output=True, text=True)
        if okay:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result

    def test_init_remember_recall_and_reopen_through_cli(self):
        self.command('init', '--goal', 'Test CLI project')
        self.command('remember', 'retention', '7', '--evidence', 'caller:decision')
        self.command('checkpoint', 'Export retention is 7 days')
        self.assertTrue(self.command('recall', 'export retention')['notes'])
        self.assertEqual(self.command('status')['goal'], 'Test CLI project')
        self.assertEqual(self.command('facts')['active']['retention']['value'], 7)

    def test_restore_requires_explicit_confirmation(self):
        head = self.command('init', '--goal', 'Test CLI project')['head']
        (self.repo / 'example.txt').write_text('modified')
        result = self.command('restore', head, okay=False)
        self.assertIn('--yes', result.stderr)
        self.assertEqual((self.repo / 'example.txt').read_text(), 'modified')
        self.command('restore', head, '--yes')
        self.assertEqual((self.repo / 'example.txt').read_text(), 'initial')

    def test_export_import_use_validated_files(self):
        self.command('init', '--goal', 'Portable CLI project')
        target = Path(self.temp.name) / 'bundle.json'
        self.command('export', str(target))
        self.assertEqual(json.loads(target.read_text())['format'], 'statetree.bundle')
        self.command('import', str(target))

    def test_doctor_does_not_require_project_or_sdk(self):
        result = self.command('doctor')
        self.assertTrue(result['git_available'])
        self.assertIn('strands_available', result)

    def test_init_git_handles_fresh_download_without_committing_source(self):
        self.repo = Path(self.temp.name) / 'download'
        self.repo.mkdir()
        (self.repo / 'app.py').write_text('print(42)')
        self.command('init', '--git', '--goal', 'Fresh ZIP project')
        self.assertEqual(self.command('status')['goal'], 'Fresh ZIP project')
        self.assertEqual((self.repo / 'app.py').read_text(), 'print(42)')

    def test_missing_sdk_is_reported_without_fabricated_usage(self):
        if importlib.util.find_spec('strands'):
            self.skipTest('The SDK is installed; genuine bridge tests cover inference')
        self.command('init', '--goal', 'No fake models')
        result = self.command('ask', 'What is the project goal?', okay=False)
        self.assertIn('Strands', result.stderr)
        self.assertEqual(self.command('usage')['requests'], 0)

    def test_demo_runs_real_local_components_and_labels_estimates(self):
        output = Path(self.temp.name) / 'demo'
        report = self.command('demo', '--directory', str(output))
        self.assertEqual(report['provider_requests'], 0)
        self.assertEqual(report['context_comparison']['counting_method'], 'utf8_bytes_estimate')
        self.assertIsNone(report['context_comparison']['provider_tokens'])
        self.assertTrue(report['checks']['recovery'])
        self.assertTrue(report['checks']['verified_merge'])
        self.assertTrue((output / 'demo-report.json').is_file())
