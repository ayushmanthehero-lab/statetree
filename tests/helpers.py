import copy
import subprocess
from pathlib import Path



def git(path, *args):
    return subprocess.check_output(
        ['git', '-C', str(path), *args], text=True, stderr=subprocess.PIPE
    ).strip()


def init_repo(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    git(path, 'init', '-q')
    git(path, 'config', 'user.name', 'StateTree tests')
    git(path, 'config', 'user.email', 'tests@statetree.invalid')
    (path / 'file.txt').write_text('initial', encoding='utf-8')
    git(path, 'add', 'file.txt')
    git(path, 'commit', '-qm', 'initial')
    return path


class OfflineAgent:
    """Small snapshot adapter for persistence tests; never invokes a model."""

    def __init__(self):
        self.messages = []
        self.values = {'task': 'original'}
        self.system_prompt = 'offline test'

    def take_snapshot(self, **kwargs):
        from strands import Snapshot
        return Snapshot(
            scope='agent', schema_version='1.0', app_data={},
            data=copy.deepcopy({'state': self.values, 'messages': self.messages}),
        )

    def load_snapshot(self, snapshot):
        self.values = copy.deepcopy(snapshot.data['state'])
        self.messages = copy.deepcopy(snapshot.data['messages'])


def __getattr__(name):
    if name == 'ScriptedModel':
        from tests.model_helpers import ScriptedModel
        return ScriptedModel
    raise AttributeError(name)
