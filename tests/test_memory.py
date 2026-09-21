import copy
import json
import unittest

from statetree.memory import FactBudgetError, FactMemory, FactValidationError


class FactMemoryTests(unittest.TestCase):
    def test_observe_projects_explicit_versioned_fact_with_evidence(self):
        memory = FactMemory()

        record = memory.observe("runtime", {"language": "python"}, evidence=["archive:readme"])

        self.assertEqual(record, {
            "key": "runtime",
            "version": 1,
            "value": {"language": "python"},
            "evidence": ["archive:readme"],
            "dependencies": {},
            "status": "active",
        })
        self.assertEqual(memory.active_facts(), {
            "runtime": {
                "version": 1,
                "value": {"language": "python"},
                "evidence": ["archive:readme"],
                "dependencies": {},
            }
        })

    def test_duplicate_observation_is_deduplicated_without_new_version(self):
        memory = FactMemory()
        first = memory.observe("runtime", "python", evidence=["archive:1"])

        second = memory.observe("runtime", "python", evidence=["archive:1"])

        self.assertEqual(second, first)
        self.assertEqual(len(memory.to_dict()["records"]), 1)

    def test_boolean_to_number_is_a_new_version_and_invalidates_dependents(self):
        memory = FactMemory()
        memory.observe("enabled", True, evidence=["config:1"])
        memory.observe(
            "mode", "active", evidence=["decision:1"], dependencies={"enabled": 1}
        )

        replacement = memory.observe("enabled", 1, evidence=["config:1"])

        self.assertEqual(replacement["version"], 2)
        self.assertEqual(replacement["value"], 1)
        self.assertNotIsInstance(replacement["value"], bool)
        self.assertNotIn("mode", memory.active_facts())
        mode = next(record for record in memory.to_dict()["records"]
                    if record["key"] == "mode")
        self.assertEqual(mode["status"], "invalid")

    def test_nested_booleans_and_numbers_are_not_deduplicated(self):
        memory = FactMemory()
        memory.observe(
            "settings", {"enabled": True, "limits": [1, False]}, evidence=["config:1"]
        )

        replacement = memory.observe(
            "settings", {"enabled": 1, "limits": [True, 0]}, evidence=["config:1"]
        )

        self.assertEqual(replacement["version"], 2)
        self.assertEqual(len(memory.to_dict()["records"]), 2)
        self.assertEqual(memory.to_dict()["records"][0]["status"], "archived")

    def test_replacement_archives_old_version_and_invalidates_dependents_transitively(self):
        memory = FactMemory()
        memory.observe("runtime", "python", evidence=["archive:1"])
        memory.observe(
            "command", "python -m unittest", evidence=["archive:2"],
            dependencies={"runtime": 1},
        )
        memory.observe(
            "result", "passed", evidence=["archive:3"], dependencies={"command": 1}
        )

        replacement = memory.observe("runtime", "pypy", evidence=["archive:4"])

        self.assertEqual(replacement["version"], 2)
        self.assertEqual(memory.active_facts(), {
            "runtime": {
                "version": 2,
                "value": "pypy",
                "evidence": ["archive:4"],
                "dependencies": {},
            }
        })
        statuses = {(item["key"], item["version"]): item["status"]
                    for item in memory.to_dict()["records"]}
        self.assertEqual(statuses, {
            ("command", 1): "invalid",
            ("result", 1): "invalid",
            ("runtime", 1): "archived",
            ("runtime", 2): "active",
        })

    def test_different_keys_are_not_semantically_declared_contradictory(self):
        memory = FactMemory()
        memory.observe("claim_a", "the service is ready", evidence=["log:a"])
        memory.observe("claim_b", "the service is not ready", evidence=["log:b"])

        self.assertEqual(set(memory.active_facts()), {"claim_a", "claim_b"})

    def test_rejects_stale_dependency_version(self):
        memory = FactMemory()
        memory.observe("runtime", "python", evidence=["archive:1"])
        memory.observe("runtime", "pypy", evidence=["archive:2"])

        with self.assertRaisesRegex(FactValidationError, "active version 2"):
            memory.observe(
                "command", "run", evidence=["archive:3"], dependencies={"runtime": 1}
            )

    def test_rejects_dependency_cycle_transactionally(self):
        memory = FactMemory()
        memory.observe("a", 1, evidence=["a:1"])
        memory.observe("b", 1, evidence=["b:1"], dependencies={"a": 1})
        before = memory.to_dict()

        with self.assertRaisesRegex(FactValidationError, "cycle"):
            memory.observe("a", 2, evidence=["a:2"], dependencies={"b": 1})

        self.assertEqual(memory.to_dict(), before)

    def test_invalidate_archive_and_delete_remove_active_facts_and_dependents(self):
        for operation, source_status in (
            ("invalidate", "invalid"), ("archive", "archived"), ("delete", "archived")
        ):
            with self.subTest(operation=operation):
                memory = FactMemory()
                memory.observe("source", "v", evidence=["source:1"])
                memory.observe(
                    "dependent", "d", evidence=["dependent:1"], dependencies={"source": 1}
                )

                result = getattr(memory, operation)("source")

                self.assertEqual(memory.active_facts(), {})
                records = memory.to_dict()["records"]
                source = next(item for item in records
                              if item["key"] == "source" and item["version"] == 1)
                dependent = next(item for item in records if item["key"] == "dependent")
                self.assertEqual(source["status"], source_status)
                self.assertEqual(dependent["status"], "invalid")
                if operation == "delete":
                    self.assertEqual(result["status"], "deleted")
                    self.assertEqual(result["version"], 2)

    def test_delete_leaves_tombstone_and_reobservation_advances_version(self):
        memory = FactMemory()
        memory.observe("phase", "build", evidence=["event:1"])
        tombstone = memory.delete("phase")
        restored = memory.observe("phase", "test", evidence=["event:2"])

        self.assertEqual(tombstone, {
            "key": "phase", "version": 2, "value": None, "evidence": [],
            "dependencies": {}, "status": "deleted",
        })
        self.assertEqual(restored["version"], 3)
        self.assertEqual([item["status"] for item in memory.to_dict()["records"]],
                         ["archived", "deleted", "active"])

    def test_inputs_and_returned_values_do_not_alias_internal_state(self):
        memory = FactMemory()
        value = {"items": ["first"]}
        evidence = ["archive:1"]
        dependencies = {}

        returned = memory.observe(
            "payload", value, evidence=evidence, dependencies=dependencies
        )
        value["items"].append("mutated")
        evidence.append("mutated")
        dependencies["other"] = 9
        returned["value"]["items"].append("also mutated")
        projection = memory.active_facts()
        projection["payload"]["value"]["items"].append("projection mutation")

        self.assertEqual(memory.active_facts()["payload"], {
            "version": 1,
            "value": {"items": ["first"]},
            "evidence": ["archive:1"],
            "dependencies": {},
        })

    def test_long_lifecycle_roundtrip_retains_history_evidence_and_tombstones(self):
        memory = FactMemory()
        for version in range(1, 31):
            memory.observe("counter", version, evidence=[f"event:{version}"])
        memory.observe("derived", 60, evidence=["calc:1"], dependencies={"counter": 30})
        memory.delete("counter")
        serialized = json.loads(json.dumps(memory.to_dict()))

        restored = FactMemory.from_dict(serialized)

        self.assertEqual(restored.to_dict(), serialized)
        records = restored.to_dict()["records"]
        self.assertEqual(len(records), 32)
        self.assertEqual(records[0]["evidence"], ["event:1"])
        self.assertEqual(records[29]["evidence"], ["event:30"])
        self.assertEqual(records[-1]["status"], "invalid")
        self.assertEqual(restored.active_facts(), {})

    def test_from_dict_rejects_malformed_or_incoherent_records(self):
        valid = FactMemory()
        valid.observe("a", 1, evidence=["event:1"])
        baseline = valid.to_dict()
        malformed = [
            None,
            {},
            {**baseline, "schema_version": True},
            {**baseline, "schema_version": 2},
            {**baseline, "extra": True},
            {**baseline, "records": "not a list"},
            {**baseline, "records": [baseline["records"][0], baseline["records"][0]]},
            {**baseline, "records": [{**baseline["records"][0], "version": 0}]},
            {**baseline, "records": [{**baseline["records"][0], "status": "unknown"}]},
            {**baseline, "records": [{**baseline["records"][0], "evidence": []}]},
            {**baseline, "records": [{**baseline["records"][0], "unexpected": 1}]},
            {"schema_version": 1, "records": [{
                "key": "dependent", "version": 1, "value": 2,
                "evidence": ["event:2"], "dependencies": {"missing": 1},
                "status": "active",
            }]},
            {"schema_version": 1, "records": [{
                "key": "a", "version": 1, "value": 1,
                "evidence": ["event:1"], "dependencies": {},
                "status": "invalid",
                "invalidated_by": {"key": "missing", "version": 1,
                                   "action": "dependency_invalidated"},
            }]},
            {"schema_version": 1, "records": [{
                "key": "a", "version": 1, "value": None,
                "evidence": [], "dependencies": {}, "status": "deleted",
            }]},
            {"schema_version": 1, "records": [
                {"key": "a", "version": 1, "value": 1,
                 "evidence": ["event:1"], "dependencies": {}, "status": "archived"},
                {"key": "b", "version": 1, "value": 2,
                 "evidence": ["event:2"], "dependencies": {}, "status": "invalid",
                 "invalidated_by": {"key": "a", "version": 1,
                                    "action": "superseded"}},
            ]},
        ]

        for payload in malformed:
            with self.subTest(payload=payload):
                with self.assertRaises(FactValidationError):
                    FactMemory.from_dict(payload)

    def test_invalid_keys_values_evidence_and_dependencies_are_rejected(self):
        bad_calls = [
            ("", 1, ["event:1"], None),
            (1, 1, ["event:1"], None),
            ("a", float("nan"), ["event:1"], None),
            ("a", {1: "bad key"}, ["event:1"], None),
            ("a", 1, [], None),
            ("a", 1, "event:1", None),
            ("a", 1, [""], None),
            ("a", 1, [1], None),
            ("a", 1, ["same", "same"], None),
            ("a", 1, ["event:1"], {"a": 1}),
            ("a", 1, ["event:1"], {"missing": 1}),
            ("a", 1, ["event:1"], {"missing": True}),
        ]

        for key, value, evidence, dependencies in bad_calls:
            with self.subTest(key=key, value=value, evidence=evidence, dependencies=dependencies):
                with self.assertRaises(FactValidationError):
                    FactMemory().observe(
                        key, value, evidence=evidence, dependencies=dependencies
                    )

    def test_missing_or_inactive_key_cannot_be_mutated_again(self):
        memory = FactMemory()
        memory.observe("a", 1, evidence=["event:1"])
        memory.archive("a")

        for operation, key in (("archive", "a"), ("invalidate", "a"),
                               ("delete", "missing")):
            with self.subTest(operation=operation, key=key):
                with self.assertRaises(KeyError):
                    getattr(memory, operation)(key)

    def test_materialize_selects_deterministically_with_dependency_closures(self):
        memory = FactMemory()
        memory.observe("a", "A", evidence=["event:a"])
        memory.observe("b", "B", evidence=["event:b"], dependencies={"a": 1})
        memory.observe("c", "C", evidence=["event:c"])
        calls = []

        def counter(serialized):
            facts = json.loads(serialized)
            calls.append(list(facts))
            return len(facts) * 10

        result = memory.materialize(20, counter=counter)

        self.assertEqual(list(result.facts), ["a", "b"])
        self.assertEqual(result.estimated_count, 20)
        self.assertEqual(result.counting_method, "custom_counter")
        self.assertEqual(result.omitted_keys, ("c",))
        self.assertEqual(calls[-1], ["a", "b"])

    def test_materialize_required_closure_fails_explicitly_when_it_cannot_fit(self):
        memory = FactMemory()
        memory.observe("a", "A", evidence=["event:a"])
        memory.observe("b", "B", evidence=["event:b"], dependencies={"a": 1})

        with self.assertRaisesRegex(FactBudgetError, "Required facts"):
            memory.materialize(19, counter=lambda serialized: len(json.loads(serialized)) * 10,
                               required=["b"])

    def test_materialize_default_counts_canonical_serialized_utf8_bytes(self):
        memory = FactMemory()
        memory.observe("language", "日本語", evidence=["doc:🌲"])

        result = memory.materialize(1000)
        serialized = json.dumps(
            result.facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )

        self.assertEqual(result.estimated_count, len(serialized.encode("utf-8")))
        self.assertEqual(result.counting_method, "serialized_json_utf8_bytes_estimate")

    def test_materialize_validates_budget_counter_and_required_keys(self):
        memory = FactMemory()
        memory.observe("a", 1, evidence=["event:1"])

        for budget in (0, -1, True, 1.5):
            with self.subTest(budget=budget):
                with self.assertRaises(ValueError):
                    memory.materialize(budget)
        for bad_count in (-1, True, 1.5):
            with self.subTest(bad_count=bad_count):
                with self.assertRaises(FactBudgetError):
                    memory.materialize(10, counter=lambda serialized, value=bad_count: value)
        for required in (["missing"], ["a", "a"], "a"):
            with self.subTest(required=required):
                with self.assertRaises(FactValidationError):
                    memory.materialize(1000, required=required)

    def test_serialized_input_is_copied_before_validation(self):
        memory = FactMemory()
        memory.observe("a", {"items": [1]}, evidence=["event:1"])
        payload = memory.to_dict()
        original = copy.deepcopy(payload)

        restored = FactMemory.from_dict(payload)
        payload["records"][0]["value"]["items"].append(2)

        self.assertEqual(restored.to_dict(), original)


if __name__ == "__main__":
    unittest.main()
