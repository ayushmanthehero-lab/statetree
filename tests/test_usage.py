import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from statetree.runtime.usage import UsageLedger


class UsageLedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "usage.sqlite3"
        self.ledger = UsageLedger(self.path)

    def record(self, request_id, usage, **kwargs):
        options = {"run_id": "run-1", "branch": "main", **kwargs}
        return self.ledger.record(request_id, usage=usage, **options)

    def test_plain_usage_and_actual_zero_usage_have_complete_totals(self):
        self.record("one", {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120})
        self.record("zero", {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0})
        summary = self.ledger.summary()
        self.assertEqual(summary["requests"], 2)
        self.assertEqual(summary["known_usage_requests"], 2)
        self.assertEqual(summary["input_tokens"], 100)
        self.assertEqual(summary["output_tokens"], 20)
        self.assertEqual(summary["total_tokens"], 120)
        self.assertEqual(summary["reported_total_tokens"], 120)

    def test_included_and_excluded_cache_conventions_normalize_equally(self):
        self.record("included", {
            "inputTokens": 100, "outputTokens": 20, "totalTokens": 120,
            "cacheReadInputTokens": 30, "cacheWriteInputTokens": 10,
        }, input_cache_convention="included")
        self.record("excluded", {
            "inputTokens": 60, "outputTokens": 20, "totalTokens": 120,
            "cacheReadInputTokens": 30, "cacheWriteInputTokens": 10,
        })
        summary = self.ledger.summary()
        self.assertEqual(summary["known_usage_requests"], 2)
        self.assertEqual(summary["input_tokens"], 200)
        self.assertEqual(summary["output_tokens"], 40)
        self.assertEqual(summary["total_tokens"], 240)
        self.assertEqual(summary["cache_read_input_tokens"], 60)
        self.assertEqual(summary["cache_write_input_tokens"], 20)
        self.assertEqual(summary["reported_input_tokens"], 160)

    def test_ambiguous_or_inconsistent_usage_retains_raw_counts_without_normalized_totals(self):
        ambiguous = {"inputTokens": 100, "outputTokens": 20, "cacheReadInputTokens": 30}
        inconsistent = {"inputTokens": 100, "outputTokens": 20, "totalTokens": 999}
        impossible_cache = {
            "inputTokens": 10, "outputTokens": 20, "totalTokens": 30,
            "cacheReadInputTokens": 40,
        }
        for request_id, usage in [("missing-total", ambiguous), ("bad-total", inconsistent), ("bad-cache", impossible_cache)]:
            self.record(request_id, usage)
        summary = self.ledger.summary()
        self.assertEqual(summary["ambiguous_usage_requests"], 3)
        self.assertEqual(summary["known_usage_requests"], 0)
        self.assertEqual(summary["total_tokens"], 0)
        self.assertEqual(summary["reported_total_tokens"], 1029)
        self.assertEqual(summary["reported_input_tokens"], 210)
        records = {row["request_id"]: row for row in self.ledger.records()}
        self.assertEqual(records["missing-total"]["usage"], ambiguous)
        self.assertEqual(records["missing-total"]["normalization"]["status"], "ambiguous")

    def test_explicit_cache_convention_supports_missing_total_but_cannot_override_contradiction(self):
        usage = {"inputTokens": 100, "outputTokens": 20, "cacheReadInputTokens": 30}
        self.record("included", usage, input_cache_convention="included")
        self.record("excluded", usage, input_cache_convention="excluded")
        self.record("contradiction", {**usage, "totalTokens": 999}, input_cache_convention="excluded")
        summary = self.ledger.summary()
        self.assertEqual(summary["input_tokens"], 230)
        self.assertEqual(summary["output_tokens"], 40)
        self.assertEqual(summary["total_tokens"], 270)
        self.assertEqual(summary["reported_total_tokens"], 999)
        self.assertEqual(summary["known_usage_requests"], 2)
        self.assertEqual(summary["ambiguous_usage_requests"], 1)

    def test_equal_input_output_total_does_not_prove_cache_is_included(self):
        # Strands Anthropic reports a subtotal excluding cache; OpenAI includes
        # cache in both inputTokens and totalTokens. Equal sums alone cannot tell.
        self.record("unknown-provider", {
            "inputTokens": 100, "outputTokens": 20, "totalTokens": 120,
            "cacheReadInputTokens": 30,
        })
        summary = self.ledger.summary()
        self.assertEqual(summary["ambiguous_usage_requests"], 1)
        self.assertEqual(summary["total_tokens"], 0)
        self.assertEqual(summary["reported_total_tokens"], 120)

    def test_explicit_excluded_cache_supports_provider_total_excluding_cache(self):
        self.record("anthropic", {
            "inputTokens": 100, "outputTokens": 20, "totalTokens": 120,
            "cacheReadInputTokens": 30, "cacheWriteInputTokens": 10,
        }, input_cache_convention="excluded")
        summary = self.ledger.summary()
        self.assertEqual(summary["known_usage_requests"], 1)
        self.assertEqual(summary["input_tokens"], 140)
        self.assertEqual(summary["total_tokens"], 160)
        self.assertEqual(summary["reported_input_tokens"], 100)
        self.assertEqual(summary["reported_total_tokens"], 120)
        self.assertEqual(self.ledger.records()[0]["normalization"]["total_cache_convention"], "excluded")

    def test_unknown_and_partial_usage_are_not_treated_as_known_zero(self):
        self.record("unknown", None, status="error")
        self.record("empty", {})
        self.record("partial", {"inputTokens": 17, "totalTokens": 20})
        summary = self.ledger.summary()
        self.assertEqual(summary["requests"], 3)
        self.assertEqual(summary["unknown_usage_requests"], 3)
        self.assertEqual(summary["known_usage_requests"], 0)
        self.assertEqual(summary["reported_input_tokens"], 17)
        self.assertEqual(summary["reported_total_tokens"], 20)
        self.assertEqual(summary["total_tokens"], 0)
        self.assertEqual(self.ledger.records()[0]["status"], "error")

    def test_duplicate_delivery_is_idempotent_across_instances_and_restart(self):
        usage = {"inputTokens": 7, "outputTokens": 3, "providerExtra": {"nested": [1, 2]}}
        self.assertTrue(self.record("request", usage))
        replay = {"providerExtra": {"nested": [1, 2]}, "outputTokens": 3, "inputTokens": 7}
        self.assertFalse(self.record("request", replay))
        reopened = UsageLedger(self.path)
        self.assertFalse(reopened.record("request", run_id="run-1", branch="main", usage=usage))
        self.assertEqual(reopened.summary()["requests"], 1)
        self.assertEqual(reopened.summary()["total_tokens"], 10)
        usage["providerExtra"]["nested"].append(3)
        self.assertEqual(reopened.records()[0]["usage"]["providerExtra"]["nested"], [1, 2])

    def test_conflicting_request_id_raises_without_mutating_original(self):
        usage = {"inputTokens": 7, "outputTokens": 3}
        self.record("request", usage)
        for changed in [{"usage": {"inputTokens": 8, "outputTokens": 3}}, {"branch": "other"}, {"run_id": "other"}, {"phase": "summary"}, {"model": "different"}, {"status": "discarded"}, {"input_cache_convention": "included"}]:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.ledger.record("request", **{"run_id": "run-1", "branch": "main", "usage": usage, **changed})
        self.assertEqual(self.ledger.summary()["requests"], 1)
        self.assertEqual(self.ledger.summary()["total_tokens"], 10)

    def test_filters_attribute_discarded_work_and_phases(self):
        self.record("main", {"inputTokens": 10, "outputTokens": 1})
        self.record("discarded", {"inputTokens": 20, "outputTokens": 2}, branch="candidate", status="discarded")
        self.record("summary", {"inputTokens": 30, "outputTokens": 3}, phase="summary")
        self.record("second", {"inputTokens": 40, "outputTokens": 4}, run_id="run-2")
        self.assertEqual(self.ledger.summary()["total_tokens"], 110)
        self.assertEqual(self.ledger.summary(run_id="run-1")["total_tokens"], 66)
        self.assertEqual(self.ledger.summary(branch="main")["total_tokens"], 88)
        self.assertEqual(self.ledger.summary(phase="summary")["total_tokens"], 33)
        self.assertEqual(self.ledger.summary(run_id="run-1", branch="main", phase="agent")["total_tokens"], 11)
        self.assertEqual(self.ledger.summary(run_id="missing")["requests"], 0)
        self.assertEqual(len(self.ledger.records(branch="candidate")), 1)

    def test_malformed_counts_are_rejected_without_writes(self):
        for field in ["inputTokens", "outputTokens", "totalTokens", "cacheReadInputTokens", "cacheWriteInputTokens"]:
            for value in [-1, True, False, 1.5, "1", None]:
                with self.subTest(field=field, value=value), self.assertRaises((TypeError, ValueError)):
                    self.record("bad", {"inputTokens": 1, "outputTokens": 1, field: value})
        for usage in [[], "usage", 10, {"inputTokens": 1, "extra": float("nan")}, {"inputTokens": 1, "extra": object()}]:
            with self.subTest(usage=usage), self.assertRaises((TypeError, ValueError)):
                self.record("bad", usage)
        with self.assertRaises(ValueError):
            self.record("bad", {}, input_cache_convention="guess")
        self.assertEqual(self.ledger.summary()["requests"], 0)

    def test_concurrent_duplicate_delivery_counts_once(self):
        def deliver(_):
            ledger = UsageLedger(self.path)
            return ledger.record("same", run_id="run-1", branch="main", usage={"inputTokens": 9, "outputTokens": 1})

        with ThreadPoolExecutor(max_workers=4) as executor:
            inserted = list(executor.map(deliver, range(12)))
        self.assertEqual(sum(inserted), 1)
        self.assertEqual(self.ledger.summary()["total_tokens"], 10)


if __name__ == "__main__":
    unittest.main()
