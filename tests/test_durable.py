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

from statetree.runtime.durable import (
    DurableRunner,
    Step,
    StepConflictError,
    UncertainStepError,
)


ROOT = Path(__file__).resolve().parents[1]


class DurableRunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.journal = self.root / "durable.sqlite3"

    def runner(self, tools, run_id="run-1"):
        return DurableRunner(self.journal, tools, run_id)

    def test_completed_steps_replay_json_result_without_repeating_tool(self):
        calls = []

        def lookup(arguments, idempotency_key):
            calls.append((arguments, idempotency_key))
            return {"value": arguments["value"], "items": [1, None, True]}

        step = Step("lookup-1", "lookup", {"value": "answer"}, mode="read")
        first = self.runner({"lookup": lookup}).run([step])
        second = self.runner({"lookup": lookup}).run([step])

        self.assertEqual(first, [{"items": [1, None, True], "value": "answer"}])
        self.assertEqual(second, first)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], {"value": "answer"})
        self.assertTrue(calls[0][1].startswith("statetree:"))

    def test_intent_is_committed_before_handler_runs(self):
        observed = []

        def inspect_journal(arguments, idempotency_key):
            with closing(sqlite3.connect(self.journal)) as connection:
                observed.append(connection.execute(
                    "SELECT status, idempotency_key FROM durable_steps "
                    "WHERE run_id = ? AND step_id = ?",
                    ("run-1", "inspect"),
                ).fetchone())
            return "seen"

        result = self.runner({"inspect": inspect_journal}).run([
            Step("inspect", "inspect", {}, mode="read")
        ])
        self.assertEqual(result, ["seen"])
        self.assertEqual(observed[0][0], "intent")
        self.assertEqual(observed[0][1].startswith("statetree:"), True)

    def test_changed_step_definition_is_rejected_without_new_effect(self):
        calls = []

        def tool(arguments, idempotency_key):
            calls.append(arguments)
            return "done"

        runner = self.runner({"tool": tool, "other": tool})
        runner.run([Step("same", "tool", {"n": 1}, mode="read")])
        changed = [
            Step("same", "tool", {"n": 2}, mode="read"),
            Step("same", "other", {"n": 1}, mode="read"),
            Step("same", "tool", {"n": 1}, mode="idempotent"),
        ]
        for step in changed:
            with self.subTest(step=step), self.assertRaises(StepConflictError):
                runner.run([step])
        self.assertEqual(calls, [{"n": 1}])

    def test_later_definition_conflict_is_found_before_earlier_new_effect(self):
        calls = []

        def tool(arguments, idempotency_key):
            calls.append(arguments)
            return "done"

        runner = self.runner({"tool": tool})
        runner.run([Step("existing", "tool", {"n": 1})])
        calls.clear()
        with self.assertRaises(StepConflictError):
            runner.run([
                Step("new", "tool", {"n": 0}),
                Step("existing", "tool", {"n": 2}),
            ])
        self.assertEqual(calls, [])

    def test_idempotent_failure_retries_after_restart_with_same_key(self):
        keys = []

        def flaky(arguments, idempotency_key):
            keys.append(idempotency_key)
            if len(keys) == 1:
                raise RuntimeError("temporary")
            return {"attempt": len(keys)}

        step = Step("send", "send", {"message": "hello"}, mode="idempotent")
        with self.assertRaisesRegex(RuntimeError, "temporary"):
            self.runner({"send": flaky}).run([step])
        result = self.runner({"send": flaky}).run([step])
        self.assertEqual(result, [{"attempt": 2}])
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], keys[1])

    def test_at_most_once_failure_is_uncertain_and_never_retried(self):
        calls = []

        def charge(arguments, idempotency_key):
            calls.append(idempotency_key)
            raise RuntimeError("connection lost")

        step = Step("charge", "charge", {"amount": 9}, mode="at_most_once")
        with self.assertRaisesRegex(RuntimeError, "connection lost"):
            self.runner({"charge": charge}).run([step])
        with self.assertRaises(UncertainStepError) as caught:
            self.runner({"charge": charge}).run([step])
        self.assertEqual(len(calls), 1)
        self.assertEqual(caught.exception.step_id, "charge")
        self.assertEqual(caught.exception.idempotency_key, calls[0])

    def test_manual_resolution_and_reconciliation_complete_uncertain_steps(self):
        def fail(arguments, idempotency_key):
            raise OSError("outcome unavailable")

        resolve_step = Step("manual", "unsafe", {"x": 1}, mode="at_most_once")
        reconcile_step = Step("query", "unsafe", {"x": 2}, mode="at_most_once")
        runner = self.runner({"unsafe": fail})
        for step in (resolve_step, reconcile_step):
            with self.assertRaises(OSError):
                runner.run([step])

        resolved = runner.resolve(resolve_step, {"receipt": "external-1"})
        seen = []

        def reconcile(arguments, idempotency_key):
            seen.append((arguments, idempotency_key))
            return {"receipt": "external-2"}

        reconciled = runner.reconcile(reconcile_step, reconcile)
        self.assertEqual(resolved, {"receipt": "external-1"})
        self.assertEqual(reconciled, {"receipt": "external-2"})
        self.assertEqual(runner.run([resolve_step, reconcile_step]), [resolved, reconciled])
        self.assertEqual(seen[0][0], {"x": 2})
        self.assertTrue(seen[0][1].startswith("statetree:"))

    def test_json_contract_and_plan_are_validated_before_any_effect(self):
        for arguments in ({"bad": float("nan")}, {1: "non-string key"}, {"bad": (1, 2)}):
            with self.subTest(arguments=arguments), self.assertRaises((TypeError, ValueError)):
                Step("bad", "tool", arguments)
        with self.assertRaises(ValueError):
            Step("bad", "tool", {}, mode="exactly_once")

        calls = []

        def tool(arguments, idempotency_key):
            calls.append(arguments)
            return {"not": {"json"}}

        runner = self.runner({"tool": tool})
        with self.assertRaises(KeyError):
            runner.run([
                Step("known", "tool", {}),
                Step("unknown", "missing", {}),
            ])
        self.assertEqual(calls, [])
        with self.assertRaises(TypeError):
            runner.run([Step("known", "tool", {})])
        self.assertEqual(calls, [{}])

    def test_duplicate_step_ids_are_rejected_before_any_effect(self):
        calls = []

        def tool(arguments, idempotency_key):
            calls.append(arguments)
            return None

        with self.assertRaises(ValueError):
            self.runner({"tool": tool}).run([
                Step("duplicate", "tool", {"n": 1}),
                Step("duplicate", "tool", {"n": 1}),
            ])
        self.assertEqual(calls, [])

    def test_entire_plan_is_detached_and_revalidated_before_first_effect(self):
        calls = []

        def tool(arguments, idempotency_key):
            calls.append(arguments)
            return arguments

        invalid_later = Step("later", "tool", {"value": 2})
        invalid_later.arguments["invalid"] = float("nan")
        with self.assertRaises(ValueError):
            self.runner({"tool": tool}).run([
                Step("first", "tool", {"value": 1}),
                invalid_later,
            ])
        self.assertEqual(calls, [])

        later = Step("later", "tool", {"value": 2})

        def mutating_tool(arguments, idempotency_key):
            calls.append(arguments)
            if arguments["value"] == 1:
                later.arguments["value"] = 99
            return arguments

        result = self.runner({"tool": mutating_tool}, run_id="mutation-run").run([
            Step("first", "tool", {"value": 1}),
            later,
        ])
        self.assertEqual(result, [{"value": 1}, {"value": 2}])

    def test_process_death_after_unsafe_effect_fails_closed_on_restart(self):
        effect = self.root / "unsafe-effect.txt"
        script = r'''
import os
import sys
from pathlib import Path
from statetree.runtime.durable import DurableRunner, Step

def charge(arguments, idempotency_key):
    Path(arguments["effect"]).write_text(idempotency_key, encoding="utf-8")
    os._exit(73)

DurableRunner(sys.argv[1], {"charge": charge}, "crash-run").run([
    Step("charge-1", "charge", {"effect": sys.argv[2]}, mode="at_most_once")
])
'''
        crashed = subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.journal), str(effect)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(crashed.returncode, 73, crashed.stderr)
        original_key = effect.read_text(encoding="utf-8")
        calls = []

        def must_not_repeat(arguments, idempotency_key):
            calls.append(idempotency_key)
            return "duplicate"

        with self.assertRaises(UncertainStepError) as caught:
            DurableRunner(self.journal, {"charge": must_not_repeat}, "crash-run").run([
                Step("charge-1", "charge", {"effect": str(effect)}, mode="at_most_once")
            ])
        self.assertEqual(calls, [])
        self.assertEqual(caught.exception.idempotency_key, original_key)

    def test_process_death_retries_idempotent_effect_with_original_key(self):
        effect = self.root / "idempotent-effect.json"
        script = r'''
import json
import os
import sys
from pathlib import Path
from statetree.runtime.durable import DurableRunner, Step

def send(arguments, idempotency_key):
    Path(arguments["effect"]).write_text(
        json.dumps({"count": 1, "key": idempotency_key}), encoding="utf-8"
    )
    os._exit(74)

DurableRunner(sys.argv[1], {"send": send}, "retry-run").run([
    Step("send-1", "send", {"effect": sys.argv[2]}, mode="idempotent")
])
'''
        crashed = subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.journal), str(effect)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(crashed.returncode, 74, crashed.stderr)
        first = json.loads(effect.read_text(encoding="utf-8"))
        retry_keys = []

        def retry(arguments, idempotency_key):
            retry_keys.append(idempotency_key)
            previous = json.loads(Path(arguments["effect"]).read_text(encoding="utf-8"))
            return {"count": previous["count"], "key": idempotency_key}

        result = DurableRunner(self.journal, {"send": retry}, "retry-run").run([
            Step("send-1", "send", {"effect": str(effect)}, mode="idempotent")
        ])
        self.assertEqual(result, [{"count": 1, "key": first["key"]}])
        self.assertEqual(retry_keys, [first["key"]])

    @unittest.skipUnless(os.name in {"nt", "posix"}, "requires supported file locking")
    def test_concurrent_processes_share_run_lock_and_execute_effect_once(self):
        effect = self.root / "concurrent-effects.txt"
        script = r'''
import json
import sys
import time
from pathlib import Path
from statetree.runtime.durable import DurableRunner, Step

def write_once(arguments, idempotency_key):
    with Path(arguments["effect"]).open("a", encoding="utf-8") as stream:
        stream.write(idempotency_key + "\n")
        stream.flush()
    time.sleep(0.4)
    return {"key": idempotency_key}

result = DurableRunner(sys.argv[1], {"write": write_once}, "shared-run").run([
    Step("write-1", "write", {"effect": sys.argv[2]}, mode="at_most_once")
])
print(json.dumps(result))
'''
        command = [sys.executable, "-B", "-c", script, str(self.journal), str(effect)]
        first = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(0.05)
        second = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        first_out, first_err = first.communicate(timeout=15)
        second_out, second_err = second.communicate(timeout=15)
        self.assertEqual(first.returncode, 0, first_err)
        self.assertEqual(second.returncode, 0, second_err)
        self.assertEqual(json.loads(first_out), json.loads(second_out))
        self.assertEqual(len(effect.read_text(encoding="utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
