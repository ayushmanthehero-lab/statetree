import copy
import hashlib
import importlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


def text_message(role, text):
    return {"role": role, "content": [{"text": text}]}


def tool_turn(identifier="call-1", observation="original evidence", status="success"):
    return [
        text_message("user", "Inspect the source"),
        {"role": "assistant", "content": [{"toolUse": {
            "toolUseId": identifier, "name": "read", "input": {"path": "source.py"}
        }}]},
        {"role": "user", "content": [{"toolResult": {
            "toolUseId": identifier, "content": [{"text": observation}], "status": status
        }}]},
        text_message("assistant", "The source was inspected."),
    ]


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            importlib.util.find_spec("statetree.context"),
            "The context builder package must exist before its contract can run.",
        )
        self.context = importlib.import_module("statetree.context")

    def test_preserves_pinned_goal_constraints_and_distinct_state_values(self):
        state = {"constraints": ["Work offline"], "files": {"a.py": "ready", "b.py": "ready"}}
        result = self.context.ContextBuilder(10000).build(
            [text_message("user", "Continue")], goal="Repair the parser", state=state,
            system_prompt="You are a careful engineer.",
        )
        self.assertTrue(result.system_prompt.startswith("You are a careful engineer."))
        self.assertEqual(result.system_prompt.count("Repair the parser"), 1)
        self.assertIn("Work offline", result.system_prompt)
        self.assertIn('"a.py":"ready"', result.system_prompt)
        self.assertIn('"b.py":"ready"', result.system_prompt)
        self.assertEqual(result.masked_observations, 0)
        self.assertEqual(result.dropped_messages, 0)

    def test_archives_original_result_before_masking_and_preserves_status_and_pair(self):
        messages = tool_turn(observation="error detail " * 200, status="error")
        original_result = copy.deepcopy(messages[2]["content"][0]["toolResult"])
        messages.append(text_message("user", "Next step"))
        with tempfile.TemporaryDirectory() as directory:
            def archive(value):
                serialized = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
                identifier = hashlib.sha256(serialized).hexdigest()
                Path(directory, identifier + ".json").write_bytes(serialized)
                return identifier

            result = self.context.ContextBuilder(10000, recent_turns=1, archive=archive).build(
                messages, goal="Repair"
            )
            files = list(Path(directory).glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text(encoding="utf-8")), original_result)
            masked = result.messages[2]["content"][0]["toolResult"]
            self.assertEqual(masked["toolUseId"], "call-1")
            self.assertEqual(masked["status"], "error")
            self.assertIn(files[0].stem, masked["content"][0]["text"])
            self.assertNotIn("error detail", masked["content"][0]["text"])
            self.assertEqual(result.masked_observations, 1)

    def test_no_archive_does_not_mask_observations(self):
        messages = tool_turn() + [text_message("user", "Next")]
        result = self.context.ContextBuilder(10000, recent_turns=1).build(messages, goal="Repair")
        self.assertEqual(result.messages, messages)
        self.assertEqual(result.masked_observations, 0)

    def test_recompaction_preserves_direct_archive_reference_after_restart(self):
        from statetree.storage.local import LocalStateStore
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStateStore(directory)
            messages = tool_turn(observation='original evidence ' * 50) + [text_message('user', 'Next')]
            first = self.context.ContextBuilder(10000, recent_turns=1, archive=store.put_archive).build(
                messages, goal='Repair')
            again = self.context.ContextBuilder(10000, recent_turns=1, archive=store.put_archive).build(
                first.messages, goal='Repair')
            self.assertEqual(first.messages, again.messages)
            self.assertEqual(len(list(store.archive_dir.glob('*.json'))), 1)
            archived = store.read_archive(next(store.archive_dir.glob('*.json')).stem)
            self.assertIn('original evidence', archived['content'][0]['text'])

    def test_long_single_user_task_can_compact_between_tool_exchanges(self):
        messages = [text_message('user', 'Keep all validation rules while repairing')]
        for number in range(8):
            messages.extend(tool_turn(str(number), 'log ' * 170)[1:])
        result = self.context.ContextBuilder(
            1800, recent_turns=1, archive=lambda value: value['toolUseId']
        ).build(messages, goal='Repair')
        self.assertLessEqual(result.estimated_input_tokens, 1800)
        self.assertEqual(result.messages[0], messages[0])
        self.assertEqual(result.messages[-3:], messages[-3:])
        self.assertGreater(result.dropped_messages, 0)

    def test_drops_whole_old_turn_with_tool_pairs_to_fit(self):
        messages = tool_turn(observation="evidence " * 300)
        recent = [text_message("user", "Next"), text_message("assistant", "Done")]
        messages += recent
        result = self.context.ContextBuilder(450, recent_turns=1, archive=lambda value: "saved").build(
            messages, goal="Repair"
        )
        self.assertEqual(result.messages, recent)
        self.assertEqual(result.dropped_messages, 4)
        self.assertLessEqual(result.estimated_input_tokens, 450)

    def test_recent_tool_turn_remains_unmasked_and_complete(self):
        messages = [text_message("user", "past" * 1000), text_message("assistant", "past")]
        recent = tool_turn(observation="Keep this evidence")
        messages += recent
        result = self.context.ContextBuilder(1000, recent_turns=1, archive=lambda value: "saved").build(
            messages, goal="Repair"
        )
        self.assertEqual(result.messages, recent)
        self.assertEqual(result.dropped_messages, 2)
        self.assertEqual(result.masked_observations, 0)

    def test_parallel_tool_calls_remain_together(self):
        messages = tool_turn()
        messages[1]["content"].append({"toolUse": {"toolUseId": "call-2", "name": "read", "input": {}}})
        messages[2]["content"].append({"toolResult": {
            "toolUseId": "call-2", "status": "success", "content": [{"text": "second"}]
        }})
        messages.append(text_message("user", "Now continue"))
        result = self.context.ContextBuilder(10000, recent_turns=1, archive=lambda value: value["toolUseId"]).build(
            messages, goal="Repair"
        )
        self.assertEqual(result.masked_observations, 2)
        self.assertEqual(
            [block["toolResult"]["toolUseId"] for block in result.messages[2]["content"]],
            ["call-1", "call-2"],
        )

    def test_rejects_orphan_missing_duplicate_and_wrong_role_tool_pairs(self):
        orphan = [tool_turn()[2]]
        unfinished = tool_turn()[:2]
        duplicate = tool_turn() + tool_turn()
        wrong_role = tool_turn()
        wrong_role[2]["role"] = "assistant"
        interrupted = tool_turn()[:2] + [text_message("user", "new task")] + tool_turn()[2:]
        for messages in (orphan, unfinished, duplicate, wrong_role, interrupted):
            with self.subTest(messages=messages):
                with self.assertRaises(self.context.ContextBudgetError):
                    self.context.ContextBuilder(10000).build(messages, goal="Repair")

    def test_oversized_pinned_content_tool_specs_and_recent_turn_raise(self):
        for arguments in (
            {"messages": [], "goal": "x" * 1000},
            {"messages": [], "goal": "Repair", "system_prompt": "x" * 1000},
            {"messages": [], "goal": "Repair", "state": {"constraint": "x" * 1000}},
            {"messages": [], "goal": "Repair", "tool_specs": [{"description": "x" * 1000}]},
            {"messages": [text_message("user", "x" * 1000)], "goal": "Repair"},
            {"messages": tool_turn(observation="x" * 1000), "goal": "Repair"},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(self.context.ContextBudgetError):
                    self.context.ContextBuilder(300, recent_turns=1, archive=lambda value: "saved").build(**arguments)

    def test_custom_counter_receives_system_messages_and_tool_specs_together(self):
        seen = []
        def counter(serialized):
            seen.append(json.loads(serialized))
            return 23

        tools = [{"name": "read", "inputSchema": {"type": "object"}}]
        messages = [text_message("user", "Continue")]
        result = self.context.ContextBuilder(23, counter=counter).build(
            messages, goal="Repair", state={"phase": "test"}, system_prompt="Engineer", tool_specs=tools
        )
        self.assertEqual(result.estimated_input_tokens, 23)
        self.assertEqual(seen[-1]["messages"], messages)
        self.assertEqual(seen[-1]["tool_specs"], tools)
        self.assertEqual(seen[-1]["system_prompt"], result.system_prompt)
        self.assertEqual(result.counting_method, "custom_counter")

    def test_utf8_estimate_counts_unicode_bytes_of_entire_serialized_request(self):
        messages = [text_message("user", "日本語 🌲")]
        result = self.context.ContextBuilder(10000).build(messages, goal="réparer")
        serialized = json.dumps(
            {"system_prompt": result.system_prompt, "messages": result.messages, "tool_specs": None},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        self.assertEqual(result.estimated_input_tokens, len(serialized.encode("utf-8")))
        self.assertGreater(result.estimated_input_tokens, len(serialized))
        self.assertEqual(result.counting_method, "utf8_bytes_estimate")

    def test_tight_budget_accepts_exact_count_but_rejects_one_less(self):
        for budget, accepted in ((7, True), (6, False)):
            builder = self.context.ContextBuilder(budget, counter=lambda value: 7)
            if accepted:
                self.assertEqual(builder.build([], goal="Repair").estimated_input_tokens, 7)
            else:
                with self.assertRaises(self.context.ContextBudgetError):
                    builder.build([], goal="Repair")

    def test_builds_are_deterministic_and_do_not_mutate_or_alias_inputs(self):
        messages = tool_turn(observation="Evidence " * 100) + [text_message("user", "Continue")]
        state = {"b": 2, "a": {"current": "ready"}}
        tools = [{"name": "read"}]
        before = copy.deepcopy((messages, state, tools))
        builder = self.context.ContextBuilder(10000, recent_turns=1, archive=lambda value: "saved")
        first = builder.build(messages, goal="Repair", state=state, tool_specs=tools)
        second = builder.build(messages, goal="Repair", state={"a": {"current": "ready"}, "b": 2}, tool_specs=tools)
        self.assertEqual(first, second)
        self.assertEqual((messages, state, tools), before)
        first.messages[-1]["content"][0]["text"] = "changed result"
        self.assertEqual(messages[-1]["content"][0]["text"], "Continue")

    def test_archive_failure_or_invalid_id_never_returns_lossy_result(self):
        messages = tool_turn() + [text_message("user", "Continue")]
        def broken_archive(value):
            raise OSError("storage unavailable")

        with self.assertRaises(OSError):
            self.context.ContextBuilder(10000, recent_turns=1, archive=broken_archive).build(messages, goal="Repair")
        for identifier in (None, "", 3):
            with self.subTest(identifier=identifier):
                with self.assertRaises(self.context.ContextBudgetError):
                    self.context.ContextBuilder(10000, recent_turns=1, archive=lambda value: identifier).build(messages, goal="Repair")
        self.assertEqual(messages[2]["content"][0]["toolResult"]["content"], [{"text": "original evidence"}])

    def test_invalid_configuration_counter_and_non_json_state_raise(self):
        for configuration in ({"max_input_tokens": 0}, {"max_input_tokens": True},
                              {"max_input_tokens": 10, "recent_turns": -1}):
            with self.subTest(configuration=configuration):
                with self.assertRaises(ValueError):
                    self.context.ContextBuilder(**configuration)
        for bad_count in (-1, 1.5, True):
            with self.subTest(bad_count=bad_count):
                with self.assertRaises(self.context.ContextBudgetError):
                    self.context.ContextBuilder(1000, counter=lambda value: bad_count).build([], goal="Repair")
        for state in (["not a mapping"], {"confidence": float("nan")}, {1: "non-string key"}):
            with self.subTest(state=state):
                with self.assertRaises(self.context.ContextBudgetError):
                    self.context.ContextBuilder(1000).build([], goal="Repair", state=state)


if __name__ == "__main__":
    unittest.main()
