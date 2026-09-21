"""Durable, local execution for explicitly classified tool steps.

The SQLite journal records an intent before each call and its JSON result
afterward.  A process lock serializes all workers for one run, including the
time spent inside handlers.  That makes a live concurrent worker wait for a
receipt instead of mistaking an in-flight at-most-once step for a crash.

This module does not promise exactly-once effects.  After a crash or handler
error, an ``at_most_once`` step is uncertain and requires explicit resolution
or reconciliation.  ``read`` and ``idempotent`` steps may be retried; every
retry receives the same idempotency key.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path


_MODES = frozenset({"read", "idempotent", "at_most_once"})
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class StepConflictError(ValueError):
    """A persisted step ID was reused with a different definition."""


class UncertainStepError(RuntimeError):
    """An at-most-once step may have taken effect and cannot be retried."""

    def __init__(self, step_id: str, idempotency_key: str):
        self.step_id = step_id
        self.idempotency_key = idempotency_key
        super().__init__(
            f"Step {step_id!r} has an uncertain at-most-once outcome; "
            "reconcile it or resolve it explicitly"
        )


def _validate_json(value, path="$"):
    """Reject values that Python's permissive encoder would coerce."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string object key")
            _validate_json(item, f"{path}.{key}")
        return
    raise TypeError(f"{path} contains non-JSON value {type(value).__name__}")


def _dump_json(value) -> str:
    _validate_json(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _copy_json(value):
    return json.loads(_dump_json(value))


@dataclass(frozen=True)
class Step:
    """One durable tool invocation.

    ``arguments`` must be a JSON object.  It is copied during construction so
    later mutation of the caller's input cannot silently change the step.
    """

    step_id: str
    tool: str
    arguments: dict
    mode: str = "read"

    def __post_init__(self):
        for name, value in (("step_id", self.step_id), ("tool", self.tool)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a nonempty string")
        if self.mode not in _MODES:
            raise ValueError("mode must be read, idempotent, or at_most_once")
        if not isinstance(self.arguments, dict):
            raise TypeError("arguments must be a JSON object")
        object.__setattr__(self, "arguments", _copy_json(self.arguments))


class DurableRunner:
    """Execute and replay journaled local tool steps for one run ID.

    Tool handlers have signature ``handler(arguments, idempotency_key)`` and
    must return a JSON value.  The mapping is checked before any step in a
    supplied plan can execute.
    """

    def __init__(self, path, tools: Mapping[str, Callable], run_id: str):
        if str(path) == ":memory:":
            raise ValueError("A durable runner requires a filesystem path")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a nonempty string")
        if not isinstance(tools, Mapping):
            raise TypeError("tools must be a mapping")
        copied_tools = dict(tools)
        for name, handler in copied_tools.items():
            if not isinstance(name, str) or not name:
                raise ValueError("tool names must be nonempty strings")
            if not callable(handler):
                raise TypeError(f"tool handler {name!r} must be callable")

        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.tools = copied_tools
        self.run_id = run_id
        lock_digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
        self._lock_path = self.path.parent / f".{self.path.name}.locks" / f"{lock_digest}.lock"
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self):
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS durable_steps ("
                "run_id TEXT NOT NULL, step_id TEXT NOT NULL, "
                "tool TEXT NOT NULL, mode TEXT NOT NULL, arguments_json TEXT NOT NULL, "
                "idempotency_key TEXT NOT NULL, status TEXT NOT NULL, "
                "result_json TEXT, error_json TEXT, attempts INTEGER NOT NULL, "
                "PRIMARY KEY (run_id, step_id))"
            )

    @contextmanager
    def _run_lock(self):
        lock_key = str(self._lock_path)
        with _THREAD_LOCKS_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(lock_key, threading.Lock())
        with thread_lock:
            with self._lock_path.open("a+b") as lock_file:
                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                self._acquire_file_lock(lock_file)
                try:
                    yield
                finally:
                    self._release_file_lock(lock_file)

    @staticmethod
    def _acquire_file_lock(lock_file):
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    return
                except OSError:
                    time.sleep(0.05)
        elif os.name == "posix":
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        else:
            raise RuntimeError(f"Unsupported process-lock platform: {os.name}")

    @staticmethod
    def _release_file_lock(lock_file):
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        elif os.name == "posix":
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _key_for(self, step_id: str) -> str:
        identity = _dump_json([self.run_id, step_id]).encode("utf-8")
        return "statetree:" + hashlib.sha256(identity).hexdigest()

    @staticmethod
    def _definition(step: Step):
        return step.tool, step.mode, _dump_json(step.arguments)

    @staticmethod
    def _check_definition(step: Step, row):
        tool, mode, arguments_json = DurableRunner._definition(step)
        if (row[0], row[1], row[2]) != (tool, mode, arguments_json):
            raise StepConflictError(
                f"Step {step.step_id!r} conflicts with its persisted definition"
            )

    def _get_row(self, connection, step_id):
        return connection.execute(
            "SELECT tool, mode, arguments_json, idempotency_key, status, "
            "result_json, error_json, attempts FROM durable_steps "
            "WHERE run_id = ? AND step_id = ?",
            (self.run_id, step_id),
        ).fetchone()

    def _claim(self, step: Step):
        tool, mode, arguments_json = self._definition(step)
        idempotency_key = self._key_for(step.step_id)
        with closing(self._connect()) as connection, connection:
            row = self._get_row(connection, step.step_id)
            if row is None:
                connection.execute(
                    "INSERT INTO durable_steps "
                    "(run_id, step_id, tool, mode, arguments_json, idempotency_key, "
                    "status, result_json, error_json, attempts) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'intent', NULL, NULL, 1)",
                    (
                        self.run_id,
                        step.step_id,
                        tool,
                        mode,
                        arguments_json,
                        idempotency_key,
                    ),
                )
                return "call", idempotency_key, None

            self._check_definition(step, row)
            idempotency_key = row[3]
            status = row[4]
            if status == "complete":
                return "replay", idempotency_key, json.loads(row[5])
            if mode == "at_most_once":
                raise UncertainStepError(step.step_id, idempotency_key)
            connection.execute(
                "UPDATE durable_steps SET status = 'intent', error_json = NULL, "
                "attempts = attempts + 1 WHERE run_id = ? AND step_id = ?",
                (self.run_id, step.step_id),
            )
            return "call", idempotency_key, None

    def _complete(self, step: Step, result_json: str):
        with closing(self._connect()) as connection, connection:
            updated = connection.execute(
                "UPDATE durable_steps SET status = 'complete', result_json = ?, "
                "error_json = NULL WHERE run_id = ? AND step_id = ? AND status = 'intent'",
                (result_json, self.run_id, step.step_id),
            ).rowcount
            if updated != 1:
                raise RuntimeError(f"Lost durable claim for step {step.step_id!r}")

    def _record_failure(self, step: Step, error: Exception):
        status = "uncertain" if step.mode == "at_most_once" else "retryable"
        error_json = _dump_json(
            {"type": type(error).__name__, "message": str(error)}
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE durable_steps SET status = ?, error_json = ? "
                "WHERE run_id = ? AND step_id = ? AND status = 'intent'",
                (status, error_json, self.run_id, step.step_id),
            )

    def _validate_plan(self, steps) -> list[Step]:
        if isinstance(steps, (str, bytes)):
            raise TypeError("steps must be an iterable of Step objects")
        supplied = list(steps)
        materialized = []
        seen = set()
        for step in supplied:
            if not isinstance(step, Step):
                raise TypeError("steps must contain only Step objects")
            step = Step(step.step_id, step.tool, step.arguments, mode=step.mode)
            if step.step_id in seen:
                raise ValueError(f"Duplicate step_id {step.step_id!r} in one run call")
            seen.add(step.step_id)
            if step.tool not in self.tools:
                raise KeyError(f"Unknown durable tool {step.tool!r}")
            materialized.append(step)
        return materialized

    def _validate_persisted_definitions(self, steps: Sequence[Step]):
        with closing(self._connect()) as connection:
            for step in steps:
                row = self._get_row(connection, step.step_id)
                if row is not None:
                    self._check_definition(step, row)

    def run(self, steps: Sequence[Step]) -> list:
        """Execute steps in order and return their JSON results.

        Completed steps are replayed from the journal.  The whole input plan is
        validated before the first effect, and one process lock serializes all
        runners sharing this journal path and run ID.
        """
        materialized = self._validate_plan(steps)
        with self._run_lock():
            self._validate_persisted_definitions(materialized)
            results = []
            for step in materialized:
                action, idempotency_key, cached = self._claim(step)
                if action == "replay":
                    results.append(cached)
                    continue
                try:
                    result = self.tools[step.tool](
                        _copy_json(step.arguments), idempotency_key
                    )
                    result_json = _dump_json(result)
                    self._complete(step, result_json)
                except Exception as error:
                    self._record_failure(step, error)
                    raise
                results.append(json.loads(result_json))
            return results

    def resolve(self, step: Step, result):
        """Manually record the verified JSON outcome of an uncertain step."""
        if not isinstance(step, Step):
            raise TypeError("step must be a Step")
        result_json = _dump_json(result)
        with self._run_lock(), closing(self._connect()) as connection, connection:
            row = self._get_row(connection, step.step_id)
            if row is None:
                raise ValueError(f"Step {step.step_id!r} has no persisted intent")
            self._check_definition(step, row)
            if step.mode != "at_most_once":
                raise ValueError("resolve is only for uncertain at_most_once steps")
            if row[4] == "complete":
                if row[5] != result_json:
                    raise StepConflictError(
                        f"Step {step.step_id!r} already has a different result"
                    )
                return json.loads(row[5])
            connection.execute(
                "UPDATE durable_steps SET status = 'complete', result_json = ?, "
                "error_json = NULL WHERE run_id = ? AND step_id = ?",
                (result_json, self.run_id, step.step_id),
            )
        return json.loads(result_json)

    def reconcile(self, step: Step, reconciler: Callable):
        """Query an external source for an uncertain at-most-once outcome.

        ``reconciler(arguments, idempotency_key)`` must only inspect the prior
        operation.  Its JSON return value becomes the durable receipt.  If it
        raises or returns invalid JSON, the step remains uncertain.
        """
        if not isinstance(step, Step):
            raise TypeError("step must be a Step")
        if not callable(reconciler):
            raise TypeError("reconciler must be callable")
        with self._run_lock():
            with closing(self._connect()) as connection:
                row = self._get_row(connection, step.step_id)
            if row is None:
                raise ValueError(f"Step {step.step_id!r} has no persisted intent")
            self._check_definition(step, row)
            if step.mode != "at_most_once":
                raise ValueError("reconcile is only for uncertain at_most_once steps")
            if row[4] == "complete":
                return json.loads(row[5])

            result = reconciler(_copy_json(step.arguments), row[3])
            result_json = _dump_json(result)
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "UPDATE durable_steps SET status = 'complete', result_json = ?, "
                    "error_json = NULL WHERE run_id = ? AND step_id = ?",
                    (result_json, self.run_id, step.step_id),
                )
            return json.loads(result_json)
