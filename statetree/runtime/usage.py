"""Durable request-level provider usage, independent of checkpoint rollback.

Normalized ``input_tokens`` means the complete prompt, including cache reads
and writes. Providers differ on whether ``inputTokens`` includes those counts.
Some also exclude cache from ``totalTokens`` (Strands' Anthropic adapter).
Thus totalTokens == inputTokens + outputTokens does not prove cache inclusion.
With cache present, automatic normalization only resolves an excluded-input
convention when totalTokens == inputTokens + cache + outputTokens. Otherwise
an explicit input cache convention is required. Raw fields are retained.
"""

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path


_COUNTS = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "totalTokens": "total_tokens",
    "cacheReadInputTokens": "cache_read_input_tokens",
    "cacheWriteInputTokens": "cache_write_input_tokens",
}
_CONVENTIONS = ("auto", "included", "excluded")


def _normalize(usage, convention):
    if usage is None or "inputTokens" not in usage or "outputTokens" not in usage:
        return {"status": "unknown", "reason": "Provider input/output usage is missing."}

    prompt = usage["inputTokens"]
    output = usage["outputTokens"]
    read = usage.get("cacheReadInputTokens", 0)
    write = usage.get("cacheWriteInputTokens", 0)
    cached = read + write
    total = usage.get("totalTokens")
    resolved = convention
    if resolved == "auto":
        if cached == 0:
            resolved = "included"
        elif total is not None and total == prompt + cached + output:
            resolved = "excluded"
        else:
            return {
                "status": "ambiguous",
                "reason": "Cache inclusion cannot be reconciled with reported totalTokens.",
            }

    full_prompt = prompt + cached if resolved == "excluded" else prompt
    allowed_totals = {full_prompt + output}
    if resolved == "excluded":
        allowed_totals.add(prompt + output)
    if (resolved == "included" and cached > prompt) or (
        total is not None and total not in allowed_totals
    ):
        return {
            "status": "ambiguous",
            "reason": "Reported counts contradict the input cache convention.",
        }
    return {
        "status": "known",
        "input_cache_convention": resolved,
        "total_cache_convention": (
            "not_reported" if total is None else
            "included" if total == full_prompt + output else "excluded"
        ),
        "input_tokens": full_prompt,
        "output_tokens": output,
        "total_tokens": full_prompt + output,
        "cache_read_input_tokens": read,
        "cache_write_input_tokens": write,
    }


class UsageLedger:
    """SQLite ledger with atomic, globally unique request IDs.

    ``record`` returns True on insertion, False on exact replay, and raises
    ValueError on conflicting reuse. Retries are distinct requests and need
    distinct IDs; replaying a delivery uses its original ID. Status (including
    errors/discarded branches) does not exclude a request from accounting.

    Each operation opens and closes its own connection, so independent
    processes and threads can safely share the same filesystem database.
    """

    def __init__(self, path):
        if str(path) == ":memory:":
            raise ValueError("A durable ledger requires a filesystem path")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS usage_requests ("
                "request_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
                "branch TEXT NOT NULL, phase TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS usage_attribution "
                "ON usage_requests (run_id, branch, phase)"
            )

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def record(
        self,
        request_id,
        *,
        run_id,
        branch,
        phase="agent",
        model="",
        usage=None,
        status="ok",
        input_cache_convention="auto",
    ):
        """Store provider usage without estimating absent counts.

        ``usage`` is a JSON-serializable mapping with Strands camelCase count
        names (see module documentation). Supplied count values must be
        nonnegative integers, excluding booleans. Extra provider fields are
        preserved. Missing input/output counts are flagged as unknown.

        ``input_cache_convention`` is ``auto``, ``included``, or ``excluded``;
        Explicit ``excluded`` accepts totals with or without cache, since both
        occur in provider adapters; other contradictory totals remain flagged.
        Convention ambiguity is retained as data, not raised and lost.
        """
        for field, value in (
            ("request_id", request_id), ("run_id", run_id), ("branch", branch),
            ("phase", phase), ("status", status),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field} must be a nonempty string")
        if not isinstance(model, str):
            raise TypeError("model must be a string")
        if input_cache_convention not in _CONVENTIONS:
            raise ValueError("input_cache_convention must be auto, included, or excluded")
        if usage is not None:
            if not isinstance(usage, Mapping):
                raise TypeError("usage must be a mapping or None")
            usage = dict(usage)
            for field in _COUNTS:
                if field in usage:
                    value = usage[field]
                    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                        raise ValueError(f"{field} must be a nonnegative integer")

        payload = json.dumps(
            {
                "request_id": request_id,
                "run_id": run_id,
                "branch": branch,
                "phase": phase,
                "model": model,
                "status": status,
                "usage": usage,
                "input_cache_convention": input_cache_convention,
                "normalization": _normalize(usage, input_cache_convention),
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with closing(self._connect()) as connection, connection:
            inserted = connection.execute(
                "INSERT INTO usage_requests (request_id, run_id, branch, phase, payload) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(request_id) DO NOTHING",
                (request_id, run_id, branch, phase, payload),
            ).rowcount
            if not inserted:
                existing = connection.execute(
                    "SELECT payload FROM usage_requests WHERE request_id = ?", (request_id,)
                ).fetchone()[0]
                if existing != payload:
                    raise ValueError(f"Conflicting usage for request_id {request_id!r}")
        return bool(inserted)

    def records(self, run_id=None, branch=None, phase=None):
        """Return independent JSON-compatible records in insertion order."""
        conditions = []
        parameters = []
        for field, value in (("run_id", run_id), ("branch", branch), ("phase", phase)):
            if value is not None:
                conditions.append(f"{field} = ?")
                parameters.append(value)
        query = "SELECT payload FROM usage_requests"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY rowid"
        with closing(self._connect()) as connection:
            return [json.loads(row[0]) for row in connection.execute(query, parameters)]

    def summary(self, run_id=None, branch=None, phase=None):
        """Return counters, optionally filtered by run, branch and/or phase.

        ``requests`` is partitioned into ``known_usage_requests``,
        ``unknown_usage_requests`` and ``ambiguous_usage_requests``.

        ``input_tokens``, ``output_tokens``, ``total_tokens``,
        ``cache_read_input_tokens`` and ``cache_write_input_tokens`` sum only
        known, consistently normalized requests. Their ``reported_`` versions
        sum the actual supplied provider fields from *all* requests. They are
        subtotals when unknown/ambiguous requests are present, not estimates
        or assertions that missing usage is zero. Missing cache fields count
        as zero cache reported; total_tokens may be the arithmetic sum of
        reported input/output whereas reported_total_tokens never is inferred.
        """
        summary = {
            "requests": 0,
            "known_usage_requests": 0,
            "unknown_usage_requests": 0,
            "ambiguous_usage_requests": 0,
            **{field: 0 for field in _COUNTS.values()},
            **{f"reported_{field}": 0 for field in _COUNTS.values()},
        }
        for record in self.records(run_id=run_id, branch=branch, phase=phase):
            summary["requests"] += 1
            normalized = record["normalization"]
            summary[f"{normalized['status']}_usage_requests"] += 1
            usage = record["usage"] or {}
            for raw_field, field in _COUNTS.items():
                summary[f"reported_{field}"] += usage.get(raw_field, 0)
                if normalized["status"] == "known":
                    summary[field] += normalized[field]
        return summary
