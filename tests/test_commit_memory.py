import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from statetree.core.commit import StateTreeCommit
from statetree.memory.commits import CommitMemory, CommitNote
from statetree.storage.local import LocalStateStore


class CommitMemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = LocalStateStore(self.root)
        self.memory = CommitMemory(self.store)

    def commit(self, parent=None, branch="main", verified=False):
        commit = StateTreeCommit.create(
            parent=parent, branch=branch, goal="test", current_subgoal=None,
            strands_snapshot_path="snapshot", workspace_sha="git-sha",
            verification_status="verified" if verified else "unverified",
            verification={"passed": True} if verified else None,
        )
        self.store.save_commit(commit)
        return commit.id

    def noted(self, summary, parent=None, **fields):
        commit_id = self.commit(parent)
        self.memory.write(commit_id, CommitNote(summary, **fields))
        return commit_id

    def search(self, query, head, **options):
        return self.memory.search(query, head, budget=options.pop("budget", 10000), **options)

    def ids(self, result):
        return [note["commit_id"] for note in result.notes]

    def test_roundtrip_detaches_inputs_and_serialized_values(self):
        payload = {"summary": "Cache fix", "changes": ["worker.py"],
                   "dependencies": {"config": "v1"}, "outcome": "partial"}
        note = CommitNote.from_dict(payload)
        payload["changes"].append("mutated")
        payload["dependencies"]["config"] = "v2"
        serialized = note.to_dict()
        serialized["changes"].append("mutated again")
        self.assertEqual(note.to_dict(), {
            "summary": "Cache fix", "changes": ["worker.py"], "decisions": [],
            "pending": [], "evidence_ids": [], "dependencies": {"config": "v1"},
            "supersedes": [], "outcome": "partial",
        })

    def test_note_schema_rejects_unknown_fields_and_non_json_shapes(self):
        invalid = [None, {}, {"summary": ""}, {"summary": "  "},
                   {"summary": 1}, {"summary": "fix", "extra": True}]
        for field, values in {
            "changes": ["file", ("file",), [None], [""]],
            "decisions": [[True]], "pending": [[{}]],
            "evidence_ids": [["not-a-hash"]], "supersedes": [["../escape"]],
            "dependencies": [[], {1: "v1"}, {"version": 1}, {"version": ""}],
            "outcome": ["unknown", None, []],
        }.items():
            invalid.extend({"summary": "fix", field: value} for value in values)
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                CommitNote.from_dict(payload)

    def test_persistence_is_immutable_and_does_not_change_commit_manifest(self):
        commit_id = self.commit(verified=True)
        path = self.store.commits_dir / (commit_id + ".json")
        before = path.read_bytes()
        evidence = self.store.put_archive({"output": "passed"})
        note = CommitNote("Cache fixed", evidence_ids=[evidence])
        self.memory.write(commit_id, note)
        self.memory.write(commit_id, note)
        restored = CommitMemory(LocalStateStore(self.root)).read(commit_id)
        self.assertEqual(restored, note)
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(ValueError):
            self.memory.write(commit_id, CommitNote("Changed later"))
        self.assertEqual(self.memory.read(commit_id), note)

    def test_missing_note_is_optional_but_missing_commit_is_not(self):
        self.assertIsNone(self.memory.read(self.commit()))
        for operation in (self.memory.read,
                          lambda commit_id: self.memory.write(commit_id, CommitNote("fix"))):
            with self.assertRaises(ValueError):
                operation("f" * 64)

    def test_sidecar_hash_and_commit_binding_are_checked(self):
        first = self.noted("Cache fixed")
        second = self.commit(first)
        path = self.root / "notes" / (first + ".json")
        raw = path.read_bytes()
        (path.parent / (second + ".json")).write_bytes(raw)
        with self.assertRaises(ValueError):
            self.memory.read(second)
        payload = json.loads(raw)
        payload["note"]["summary"] = "Tampered"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.memory.read(first)
        path.write_text("not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.memory.read(first)

    def test_evidence_must_resolve_on_write_and_when_selected_again(self):
        commit_id = self.commit()
        with self.assertRaises(ValueError):
            self.memory.write(commit_id, CommitNote("cache", evidence_ids=["f" * 64]))
        evidence = self.store.put_archive({"output": "cache passed"})
        self.memory.write(commit_id, CommitNote("Cache fixed", evidence_ids=[evidence]))
        (self.store.archive_dir / (evidence + ".json")).unlink()
        self.assertEqual(self.search("unrelated", commit_id).notes, [])
        with self.assertRaises(ValueError):
            self.search("cache", commit_id)

    def test_validate_detaches_and_checks_evidence_before_publication(self):
        evidence = self.store.put_archive({"passed": True})
        source = CommitNote("Cache fixed", evidence_ids=[evidence], changes=["worker.py"])
        validated = self.memory.validate(source)
        source.changes.append("later")
        self.assertEqual(validated.changes, ["worker.py"])
        with self.assertRaises(ValueError):
            self.memory.validate({"summary": "Cache", "evidence_ids": ["f" * 64]})

    def test_relevance_splits_identifiers_and_files_and_omits_unrelated_notes(self):
        cache = self.noted("Repair OAuthToken cache", changes=["src/cache_store.py"])
        unrelated = self.noted("Document database migration", cache)
        result = self.search("debug oauth token in cache_store.py", unrelated)
        self.assertEqual(self.ids(result), [cache])
        self.assertEqual(result.notes[0]["workspace_sha"], "git-sha")
        self.assertEqual(result.notes[0]["verification_status"], "unverified")
        self.assertEqual(self.search("the and with a", unrelated).notes, [])
        self.assertEqual(self.search("unknown", unrelated).notes, [])

    def test_relevance_wins_then_ancestry_recency_breaks_ties(self):
        strong = self.noted("Cache expiration", changes=["cache invalidation"])
        older = self.noted("Cache maintenance", strong)
        newest = self.noted("Cache maintenance", older)
        self.assertEqual(self.ids(self.search("cache", newest)), [strong, newest, older])
        self.assertEqual(self.ids(self.search("cache", newest, limit=1)), [strong])

    def test_identifier_search_preserves_acronyms_and_whole_identifier_casefolding(self):
        oauth = self.noted("OAuthToken renewal")
        http = self.noted("HTTPServer transport", oauth)
        for query in ("oauth", "token", "OAUTHTOKEN"):
            with self.subTest(query=query):
                self.assertEqual(self.ids(self.search(query, http)), [oauth])
        for query in ("http", "server", "httpserver"):
            with self.subTest(query=query):
                self.assertEqual(self.ids(self.search(query, http)), [http])

    def test_whole_notes_fit_canonical_json_list_budget(self):
        first = self.noted("Cache fixed 日本語")
        second = self.noted("Cache fixed", first)
        one = self.search("cache", second, limit=1)
        encoded = json.dumps(one.notes, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.assertEqual(one.estimated_count, len(encoded))
        self.assertEqual(one.counting_method, "utf8_bytes_estimate")
        self.assertEqual(self.search("cache", second, budget=len(encoded)).notes, one.notes)
        self.assertEqual(self.search("cache", second, budget=len(encoded) - 1).notes, [])
        custom = self.search("cache", second, budget=10,
                             counter=lambda text: len(json.loads(text)) * 10)
        self.assertEqual(self.ids(custom), [second])
        self.assertEqual(custom.estimated_count, 10)
        self.assertEqual(custom.counting_method, "custom_counter")

    def test_zero_budget_and_empty_queries_return_no_notes(self):
        head = self.noted("Cache fixed")
        for query, head_id, budget in [("cache", head, 0), ("", head, 100),
                                       ("cache", None, 100)]:
            with self.subTest(query=query, head=head_id, budget=budget):
                result = self.search(query, head_id, budget=budget)
                self.assertEqual(result.notes, [])
                self.assertEqual(result.estimated_count, 2)

    def test_budget_limit_and_counter_are_validated_even_without_candidates(self):
        for budget in (-1, True, 1.5):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                self.search("", None, budget=budget)
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.search("", None, limit=limit)
        for value in (-1, True, 1.5, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.search("", None, counter=lambda text, value=value: value)
        with self.assertRaises(ValueError):
            self.search("", None, counter="not callable")

    def test_fork_includes_shared_ancestors_but_excludes_siblings_and_restored_future(self):
        base = self.noted("Cache base")
        abandoned = self.noted("Cache abandoned future", base)
        fork = self.commit(base, branch="experiment")
        self.memory.write(fork, CommitNote("Cache experiment"))
        self.assertEqual(self.ids(self.search("cache", fork)), [fork, base])
        self.assertEqual(self.ids(self.search("cache", base)), [base])
        self.assertNotIn(abandoned, self.ids(self.search("cache", fork)))

    def test_supersession_applies_before_relevance_and_preserves_history_when_restored(self):
        original = self.noted("Cache implementation")
        replacement = self.noted("Replacement architecture", original, supersedes=[original])
        self.assertEqual(self.search("cache", replacement).notes, [])
        self.assertEqual(self.ids(self.search("cache", original)), [original])
        self.assertEqual(self.ids(self.search("architecture", replacement)), [replacement])

    def test_invalid_dependency_versions_do_not_select_or_supersede(self):
        original = self.noted("Cache old implementation")
        replacement = self.noted("Cache new implementation", original,
                                dependencies={"config": "v2"}, supersedes=[original])
        for resources in (None, {}, {"config": "v1"}):
            with self.subTest(resources=resources):
                self.assertEqual(self.ids(self.search("cache", replacement,
                                                      resources=resources)), [original])
        self.assertEqual(self.ids(self.search("cache", replacement,
                                              resources={"config": "v2"})), [replacement])

    def test_corrupt_ancestor_manifests_and_cycles_fail_closed(self):
        base = self.noted("Cache base")
        child = self.noted("Cache child", base)
        path = self.store.commits_dir / (base + ".json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["goal"] = "tampered"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.search("cache", child)
        cycle = self.store.load_commit(child)
        cycle["parent"] = child
        with patch.object(self.store, "load_commit", return_value=cycle):
            with self.assertRaisesRegex(ValueError, "cycle"):
                self.search("cache", child)


if __name__ == "__main__":
    unittest.main()
