import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
import unittest

from strands import Agent

from statetree.adapters.portable import StrandsStateAdapter
from statetree.core.state import AgentState
from statetree.runtime.durable import Step, UncertainStepError
from statetree.runtime.runtime import StateTreeRuntime
from tests.helpers import ScriptedModel, init_repo


ROOT = Path(__file__).resolve().parents[1]


class CheckpointedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = init_repo(self.root / "repo")

    def runtime(self):
        agent = Agent(model=ScriptedModel(), callback_handler=None)
        StrandsStateAdapter(agent).import_state(AgentState(goal="finish workflow"))
        return StateTreeRuntime(agent, goal="finish workflow", repo_path=self.repo)

    def test_completed_workflow_replays_without_effects_and_rejects_changed_plan(self):
        runtime = self.runtime()
        effects = []

        def record(arguments, idempotency_key):
            effects.append((arguments["value"], idempotency_key))
            return {"value": arguments["value"]}

        steps = [
            Step("one", "record", {"value": "one"}, mode="idempotent"),
            Step("two", "record", {"value": "two"}, mode="read"),
        ]
        first = runtime.run_steps(steps, {"record": record}, run_id="workflow-1")
        second = runtime.run_steps(steps, {"record": record}, run_id="workflow-1")
        self.assertEqual(first, [{"value": "one"}, {"value": "two"}])
        self.assertEqual(second, first)
        self.assertEqual([value for value, _ in effects], ["one", "two"])
        state = StrandsStateAdapter(runtime.agent).export_state()
        marker = state.extensions["statetree.workflow"]["workflow-1"]
        self.assertEqual([item["step_id"] for item in marker["completed"]], ["one", "two"])
        self.assertEqual(len(state.archive_ids), 2)

        before = (self.repo / "file.txt").read_text(encoding="utf-8")
        with self.assertRaises(ValueError):
            runtime.run_steps(
                [Step("one", "record", {"value": "changed"}, mode="idempotent")],
                {"record": record},
                run_id="workflow-1",
            )
        self.assertEqual((self.repo / "file.txt").read_text(encoding="utf-8"), before)
        self.assertEqual([value for value, _ in effects], ["one", "two"])
        with self.assertRaises(TypeError):
            runtime.run_steps(
                steps,
                {"record": record, "unused-invalid": None},
                run_id="workflow-1",
            )
        self.assertEqual([value for value, _ in effects], ["one", "two"])

    def test_fresh_process_resumes_checkpointed_first_step_without_repeating_it(self):
        effects = self.root / "effects.txt"
        script = r'''
import os
import sys
from pathlib import Path
from strands import Agent
from statetree.adapters.portable import StrandsStateAdapter
from statetree.core.state import AgentState
from statetree.runtime.durable import Step
from statetree.runtime.runtime import StateTreeRuntime
from statetree.runtime import workflow
from tests.helpers import ScriptedModel

repo, effects = Path(sys.argv[1]), Path(sys.argv[2])
agent = Agent(model=ScriptedModel(), callback_handler=None)
StrandsStateAdapter(agent).import_state(AgentState(goal="finish workflow"))
runtime = StateTreeRuntime(agent, goal="finish workflow", repo_path=repo)

def record(arguments, idempotency_key):
    with effects.open("a", encoding="utf-8") as stream:
        stream.write(arguments["value"] + "\n")
    return {"value": arguments["value"]}

original_append = workflow._WorkflowStore.append
def crash_before_progress(self, run_id, index, marker, receipt, checkpoint_id, expected_parent):
    if index == 0:
        os._exit(75)
    return original_append(
        self, run_id, index, marker, receipt, checkpoint_id, expected_parent
    )
workflow._WorkflowStore.append = crash_before_progress

runtime.run_steps([
    Step("one", "record", {"value": "one"}, mode="at_most_once"),
    Step("two", "record", {"value": "two"}, mode="idempotent"),
], {"record": record}, run_id="crash-workflow")
'''
        crashed = subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.repo), str(effects)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(crashed.returncode, 75, crashed.stderr)
        self.assertEqual(effects.read_text(encoding="utf-8").splitlines(), ["one"])

        runtime = self.runtime()

        def record(arguments, idempotency_key):
            with effects.open("a", encoding="utf-8") as stream:
                stream.write(arguments["value"] + "\n")
            return {"value": arguments["value"]}

        steps = [
            Step("one", "record", {"value": "one"}, mode="at_most_once"),
            Step("two", "record", {"value": "two"}, mode="idempotent"),
        ]
        result = runtime.run_steps(steps, {"record": record}, run_id="crash-workflow")
        self.assertEqual(result, [{"value": "one"}, {"value": "two"}])
        self.assertEqual(effects.read_text(encoding="utf-8").splitlines(), ["one", "two"])
        state = StrandsStateAdapter(runtime.agent).export_state()
        marker = state.extensions["statetree.workflow"]["crash-workflow"]
        self.assertEqual([item["step_id"] for item in marker["completed"]], ["one", "two"])

    def test_fresh_process_recovers_baseline_published_before_activation(self):
        script = r'''
import os
import sys
from pathlib import Path
from strands import Agent
from statetree.adapters.portable import StrandsStateAdapter
from statetree.core.state import AgentState
from statetree.runtime.durable import Step
from statetree.runtime.runtime import StateTreeRuntime
from statetree.runtime import workflow
from tests.helpers import ScriptedModel

repo = Path(sys.argv[1])
agent = Agent(model=ScriptedModel(), callback_handler=None)
StrandsStateAdapter(agent).import_state(AgentState(goal="finish workflow"))
runtime = StateTreeRuntime(agent, goal="finish workflow", repo_path=repo)
workflow._WorkflowStore.activate = lambda *args, **kwargs: os._exit(77)
runtime.run_steps(
    [Step("one", "tool", {}, mode="read")],
    {"tool": lambda arguments, key: "done"},
    run_id="baseline-crash",
)
'''
        crashed = subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.repo)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(crashed.returncode, 77, crashed.stderr)
        runtime = self.runtime()
        calls = []

        def tool(arguments, idempotency_key):
            calls.append(idempotency_key)
            return "done"

        result = runtime.run_steps(
            [Step("one", "tool", {}, mode="read")],
            {"tool": tool},
            run_id="baseline-crash",
        )
        self.assertEqual(result, ["done"])
        self.assertEqual(len(calls), 1)

    def test_manually_rewound_branch_is_rejected_before_handler(self):
        runtime = self.runtime()
        calls = []

        def tool(arguments, idempotency_key):
            calls.append(idempotency_key)
            return "done"

        step = Step("one", "tool", {}, mode="read")
        runtime.run_steps([step], {"tool": tool}, run_id="rewind-workflow")
        workflow_db = runtime.store.root / "workflows.sqlite3"
        with closing(sqlite3.connect(workflow_db)) as connection:
            baseline = connection.execute(
                "SELECT baseline_checkpoint FROM workflows WHERE run_id = ?",
                ("rewind-workflow",),
            ).fetchone()[0]
        runtime.restore(baseline)
        with self.assertRaisesRegex(RuntimeError, "diverged|rewound"):
            runtime.run_steps([step], {"tool": tool}, run_id="rewind-workflow")
        self.assertEqual(len(calls), 1)

    def test_uncertain_at_most_once_crash_pauses_without_restoring_away_effect(self):
        effect = self.repo / "unsafe-effect.txt"
        script = r'''
import os
import sys
from pathlib import Path
from strands import Agent
from statetree.adapters.portable import StrandsStateAdapter
from statetree.core.state import AgentState
from statetree.runtime.durable import Step
from statetree.runtime.runtime import StateTreeRuntime
from tests.helpers import ScriptedModel

repo, effect = Path(sys.argv[1]), Path(sys.argv[2])
agent = Agent(model=ScriptedModel(), callback_handler=None)
StrandsStateAdapter(agent).import_state(AgentState(goal="finish workflow"))
runtime = StateTreeRuntime(agent, goal="finish workflow", repo_path=repo)

def unsafe(arguments, idempotency_key):
    effect.write_text(idempotency_key, encoding="utf-8")
    os._exit(76)

runtime.run_steps(
    [Step("unsafe", "unsafe", {}, mode="at_most_once")],
    {"unsafe": unsafe}, run_id="unsafe-workflow",
)
'''
        crashed = subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.repo), str(effect)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(crashed.returncode, 76, crashed.stderr)
        key = effect.read_text(encoding="utf-8")
        runtime = self.runtime()
        calls = []

        def must_not_run(arguments, idempotency_key):
            calls.append(idempotency_key)
            return "duplicate"

        with self.assertRaises(UncertainStepError) as caught:
            runtime.run_steps(
                [Step("unsafe", "unsafe", {}, mode="at_most_once")],
                {"unsafe": must_not_run}, run_id="unsafe-workflow",
            )
        self.assertEqual(caught.exception.idempotency_key, key)
        self.assertEqual(calls, [])
        self.assertEqual(effect.read_text(encoding="utf-8"), key)

    @unittest.skipUnless(os.name in {"nt", "posix"}, "requires supported file locking")
    def test_concurrent_fresh_processes_serialize_workspace_recovery(self):
        effects = self.root / "concurrent.txt"
        script = r'''
import json
import sys
import time
from pathlib import Path
from strands import Agent
from statetree.adapters.portable import StrandsStateAdapter
from statetree.core.state import AgentState
from statetree.runtime.durable import Step
from statetree.runtime.runtime import StateTreeRuntime
from tests.helpers import ScriptedModel

repo, effects = Path(sys.argv[1]), Path(sys.argv[2])
agent = Agent(model=ScriptedModel(), callback_handler=None)
StrandsStateAdapter(agent).import_state(AgentState(goal="finish workflow"))
runtime = StateTreeRuntime(agent, goal="finish workflow", repo_path=repo)
def tool(arguments, idempotency_key):
    with effects.open("a", encoding="utf-8") as stream:
        stream.write(idempotency_key + "\n")
        stream.flush()
    time.sleep(0.4)
    return {"key": idempotency_key}
result = runtime.run_steps(
    [Step("one", "tool", {}, mode="at_most_once")],
    {"tool": tool}, run_id="concurrent-workflow",
)
print(json.dumps(result))
'''
        command = [sys.executable, "-B", "-c", script, str(self.repo), str(effects)]
        first = subprocess.Popen(
            command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        time.sleep(0.05)
        second = subprocess.Popen(
            command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        first_out, first_err = first.communicate(timeout=20)
        second_out, second_err = second.communicate(timeout=20)
        self.assertEqual(first.returncode, 0, first_err)
        self.assertEqual(second.returncode, 0, second_err)
        self.assertEqual(json.loads(first_out), json.loads(second_out))
        self.assertEqual(len(effects.read_text(encoding="utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
