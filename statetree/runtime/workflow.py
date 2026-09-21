"""Checkpoint-aligned execution of registered durable tool workflows."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Mapping
from contextlib import closing, contextmanager
from pathlib import Path

from statetree.adapters.portable import StrandsStateAdapter, _apply_strands_state
from statetree.core.commit import canonical_json, digest
from statetree.core.state import AgentState, json_copy
from statetree.runtime.durable import DurableRunner, Step, UncertainStepError


_WORKFLOW_EXTENSION = "statetree.workflow"
_RESULT_KIND = "statetree-workflow-result-v1"
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@contextmanager
def _workspace_lock(root: Path):
    """Serialize workspace restore, tool execution, and checkpoint publication."""
    path = root / ".workflow.lock"
    key = str(path)
    with _LOCKS_GUARD:
        thread_lock = _LOCKS.setdefault(key, threading.Lock())
    with thread_lock:
        with path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.05)
            elif os.name == "posix":
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            else:
                raise RuntimeError(f"Unsupported process-lock platform: {os.name}")
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class _WorkflowStore:
    def __init__(self, path):
        self.path = Path(path)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS workflows ("
                "run_id TEXT PRIMARY KEY, workspace TEXT NOT NULL, "
                "branch TEXT NOT NULL, plan_hash TEXT NOT NULL, "
                "plan_json TEXT NOT NULL, start_parent TEXT, "
                "baseline_checkpoint TEXT, latest_checkpoint TEXT, "
                "completed_count INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS workflow_steps ("
                "run_id TEXT NOT NULL, step_index INTEGER NOT NULL, "
                "step_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, "
                "result_ref TEXT NOT NULL, checkpoint_id TEXT NOT NULL, "
                "marker_json TEXT NOT NULL, receipt_json TEXT NOT NULL, "
                "PRIMARY KEY (run_id, step_index))"
            )

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def _row(row):
        if row is None:
            return None
        fields = (
            "run_id", "workspace", "branch", "plan_hash", "plan_json",
            "start_parent", "baseline_checkpoint", "latest_checkpoint",
            "completed_count",
        )
        return dict(zip(fields, row))

    def get(self, run_id):
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT run_id, workspace, branch, plan_hash, plan_json, "
                "start_parent, baseline_checkpoint, latest_checkpoint, "
                "completed_count FROM workflows WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._row(row)

    def begin(self, run_id, workspace, branch, plan_hash, plan_json, start_parent):
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO workflows VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 0)",
                (run_id, workspace, branch, plan_hash, plan_json, start_parent),
            )

    def activate(self, run_id, baseline):
        with closing(self._connect()) as connection, connection:
            updated = connection.execute(
                "UPDATE workflows SET baseline_checkpoint = ?, latest_checkpoint = ? "
                "WHERE run_id = ? AND baseline_checkpoint IS NULL",
                (baseline, baseline, run_id),
            ).rowcount
            if updated != 1:
                raise RuntimeError("Workflow baseline was concurrently changed")

    def steps(self, run_id):
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT step_index, step_id, idempotency_key, result_ref, "
                "checkpoint_id, marker_json, receipt_json FROM workflow_steps "
                "WHERE run_id = ? ORDER BY step_index",
                (run_id,),
            ).fetchall()
        fields = (
            "step_index", "step_id", "idempotency_key", "result_ref",
            "checkpoint_id", "marker_json", "receipt_json",
        )
        return [dict(zip(fields, row)) for row in rows]

    def append(self, run_id, index, marker, receipt, checkpoint_id, expected_parent):
        marker_json = canonical_json(marker).decode("utf-8")
        receipt_json = canonical_json(receipt).decode("utf-8")
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT completed_count, latest_checkpoint FROM workflows "
                "WHERE run_id = ?", (run_id,),
            ).fetchone()
            if row != (index, expected_parent):
                raise RuntimeError("Workflow progress changed while publishing a checkpoint")
            connection.execute(
                "INSERT INTO workflow_steps VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, index, marker["step_id"], marker["idempotency_key"],
                    marker["result_ref"], checkpoint_id, marker_json, receipt_json,
                ),
            )
            connection.execute(
                "UPDATE workflows SET completed_count = ?, latest_checkpoint = ? "
                "WHERE run_id = ?",
                (index + 1, checkpoint_id, run_id),
            )


def _validate_inputs(runtime, steps, tools, run_id):
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a nonempty string")
    if not isinstance(tools, Mapping):
        raise TypeError("tools must be a mapping")
    for name, handler in tools.items():
        if not isinstance(name, str) or not name:
            raise ValueError("tool names must be nonempty strings")
        if not callable(handler):
            raise TypeError(f"Workflow tool {name!r} must be callable")
    materialized = list(steps)
    seen = set()
    for step in materialized:
        if not isinstance(step, Step):
            raise TypeError("steps must contain only Step objects")
        if step.step_id in seen:
            raise ValueError(f"Duplicate step_id {step.step_id!r}")
        seen.add(step.step_id)
        if step.tool not in tools:
            raise KeyError(f"Unknown workflow tool {step.tool!r}")
    workspace = str(runtime.workspace.path.resolve())
    branch = runtime.branch
    plan = [
        {
            "step_id": step.step_id,
            "tool": step.tool,
            "arguments": step.arguments,
            "mode": step.mode,
        }
        for step in materialized
    ]
    plan_json = canonical_json(plan).decode("utf-8")
    return materialized, workspace, branch, plan_json, digest(plan)


def _marker(run_id, branch, plan_hash, completed):
    return {
        "version": 1,
        "run_id": run_id,
        "branch": branch,
        "plan_hash": plan_hash,
        "completed": completed,
    }


def _state_marker(state, run_id):
    workflows = state.extensions.get(_WORKFLOW_EXTENSION, {})
    if not isinstance(workflows, dict):
        raise RuntimeError("Portable workflow state is malformed")
    return workflows.get(run_id)


def _import_marker(adapter, state, run_id, marker, result_ref=None):
    data = state.to_dict()
    workflows = data["extensions"].get(_WORKFLOW_EXTENSION, {})
    if not isinstance(workflows, dict):
        raise RuntimeError("Portable workflow state is malformed")
    workflows = dict(workflows)
    workflows[run_id] = marker
    data["extensions"][_WORKFLOW_EXTENSION] = workflows
    if result_ref is not None and result_ref not in data["archive_ids"]:
        data["archive_ids"].append(result_ref)
    updated = AgentState.from_dict(data)
    _apply_strands_state(adapter.agent, updated)
    return updated


def _completed_rows(workflow_store, runtime, record, state, run_id):
    rows = workflow_store.steps(run_id)
    if len(rows) != record["completed_count"]:
        raise RuntimeError("Workflow progress rows do not match the persisted count")
    marker = _state_marker(state, run_id)
    expected_entries = [json.loads(row["marker_json"]) for row in rows]
    expected_marker = _marker(run_id, record["branch"], record["plan_hash"], expected_entries)
    if marker != expected_marker:
        raise RuntimeError("Portable state is not aligned with workflow progress")
    if rows and rows[-1]["checkpoint_id"] != record["latest_checkpoint"]:
        raise RuntimeError("Latest workflow checkpoint is not aligned with progress")
    for row in rows:
        payload = runtime.store.read_archive(row["result_ref"])
        if payload.get("kind") != _RESULT_KIND:
            raise RuntimeError("Workflow result reference has the wrong kind")
        receipt = json.loads(row["receipt_json"])
        if receipt.get("result") != payload.get("result"):
            raise RuntimeError("Workflow receipt does not match its result reference")
    return rows


def _journal_rows(path, run_id):
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path)) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='durable_steps'"
        ).fetchone()
        if not exists:
            return {}
        rows = connection.execute(
            "SELECT step_id, status, idempotency_key, tool, mode, arguments_json "
            "FROM durable_steps WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    return {
        row[0]: {
            "status": row[1], "idempotency_key": row[2], "tool": row[3],
            "mode": row[4], "arguments_json": row[5],
        }
        for row in rows
    }


def _check_uncompleted_journal(journal_path, run_id, steps, completed_count):
    rows = _journal_rows(journal_path, run_id)
    for step in steps[completed_count:]:
        row = rows.get(step.step_id)
        if row is None:
            continue
        if row["status"] == "complete":
            raise RuntimeError("Durable receipt is ahead of workflow checkpoint state")
        if step.mode == "at_most_once":
            raise UncertainStepError(step.step_id, row["idempotency_key"])


def _check_record(record, workspace, branch, plan_hash, plan_json):
    if record["workspace"] != workspace or record["branch"] != branch:
        raise ValueError("Workflow run_id is bound to a different workspace or branch")
    if record["plan_hash"] != plan_hash or record["plan_json"] != plan_json:
        raise ValueError("Workflow run_id is bound to a different ordered plan")


def _checkpoint_state(runtime, commit_id):
    """Read and hash-check a workflow checkpoint without mutating the workspace."""
    manifest = runtime.store.load_commit(commit_id)
    runtime.workspace.validate(manifest["workspace_sha"])
    snapshot = runtime.store.read_snapshot(manifest["snapshot_digest"])
    try:
        state = AgentState.from_dict(snapshot["data"]["state"]["statetree"])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Workflow checkpoint has no valid portable state") from error
    return manifest, state


def _recover_published_baseline(runtime, workflow_store, record, actual_head, run_id):
    manifest, state = _checkpoint_state(runtime, actual_head)
    expected = _marker(run_id, record["branch"], record["plan_hash"], [])
    if (
        manifest["parent"] != record["start_parent"]
        or manifest["branch"] != record["branch"]
        or manifest["current_subgoal"] != f"workflow {run_id}: baseline"
        or _state_marker(state, run_id) != expected
    ):
        return False
    workflow_store.activate(run_id, actual_head)
    return True


def _recover_published_step(
    runtime, workflow_store, record, actual_head, run_id, steps, journal_path
):
    index = record["completed_count"]
    if index >= len(steps):
        return False
    manifest, state = _checkpoint_state(runtime, actual_head)
    previous_rows = workflow_store.steps(run_id)
    previous = [json.loads(row["marker_json"]) for row in previous_rows]
    marker = _state_marker(state, run_id)
    if not isinstance(marker, dict):
        return False
    completed = marker.get("completed")
    if not isinstance(completed, list) or len(completed) != index + 1:
        return False
    entry = completed[-1]
    step = steps[index]
    journal = _journal_rows(journal_path, run_id).get(step.step_id)
    arguments_json = json.dumps(
        step.arguments, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    expected_entry_keys = {"index", "step_id", "idempotency_key", "result_ref"}
    if (
        manifest["parent"] != record["latest_checkpoint"]
        or manifest["branch"] != record["branch"]
        or manifest["current_subgoal"]
        != f"workflow {run_id}: completed {step.step_id}"
        or not isinstance(entry, dict)
        or set(entry) != expected_entry_keys
        or entry.get("index") != index
        or entry.get("step_id") != step.step_id
        or completed[:-1] != previous
        or marker != _marker(run_id, record["branch"], record["plan_hash"], completed)
        or journal is None
        or journal["tool"] != f"workflow-step-{index}"
        or journal["mode"] != step.mode
        or journal["arguments_json"] != arguments_json
        or journal["idempotency_key"] != entry.get("idempotency_key")
        or entry.get("result_ref") not in state.archive_ids
    ):
        return False
    payload = runtime.store.read_archive(entry["result_ref"])
    if payload.get("kind") != _RESULT_KIND or "result" not in payload:
        return False
    row = {
        **entry,
        "step_index": index,
        "checkpoint_id": actual_head,
    }
    receipt = _receipt(record, row, payload["result"])
    workflow_store.append(
        run_id,
        index,
        entry,
        receipt,
        actual_head,
        record["latest_checkpoint"],
    )
    return True


def _receipt(record, row, result):
    return {
        "workflow_version": 1,
        "run_id": record["run_id"],
        "branch": record["branch"],
        "plan_hash": record["plan_hash"],
        "step_index": row["step_index"],
        "step_id": row["step_id"],
        "idempotency_key": row["idempotency_key"],
        "result_ref": row["result_ref"],
        "checkpoint_id": row["checkpoint_id"],
        "result": result,
    }


def run_checkpointed(runtime, steps, tools, *, run_id):
    """Run declared tools with workspace, portable-state, and receipt alignment.

    The caller must hold ``runtime._operation_lock``. This function adds a
    cross-process workspace lock for the complete restore/run/checkpoint span.
    """
    steps, workspace, branch, plan_json, plan_hash = _validate_inputs(
        runtime, steps, tools, run_id
    )
    adapter = StrandsStateAdapter(runtime.agent)
    adapter.export_state()  # Validate the caller initialized portable state.
    root = runtime.store.root
    journal_path = root / "durable.sqlite3"

    with _workspace_lock(root):
        workflow_store = _WorkflowStore(root / "workflows.sqlite3")
        record = workflow_store.get(run_id)
        actual_head = runtime.store.get_head(branch)

        if record is None:
            if actual_head != runtime._parent:
                raise RuntimeError("Runtime branch changed before workflow initialization")
            workflow_store.begin(
                run_id, workspace, branch, plan_hash, plan_json, actual_head
            )
            record = workflow_store.get(run_id)
        else:
            _check_record(record, workspace, branch, plan_hash, plan_json)

        if record["baseline_checkpoint"] is None:
            if actual_head != record["start_parent"]:
                if not _recover_published_baseline(
                    runtime, workflow_store, record, actual_head, run_id
                ):
                    raise RuntimeError("Workflow baseline initialization diverged")
            else:
                runtime._parent = actual_head
                state = adapter.export_state()
                baseline_marker = _marker(run_id, branch, plan_hash, [])
                _import_marker(adapter, state, run_id, baseline_marker)
                baseline = runtime._commit(
                    current_subgoal=f"workflow {run_id}: baseline"
                )
                workflow_store.activate(run_id, baseline.id)
            record = workflow_store.get(run_id)
            actual_head = runtime.store.get_head(branch)

        if actual_head != record["latest_checkpoint"]:
            if not _recover_published_step(
                runtime,
                workflow_store,
                record,
                actual_head,
                run_id,
                steps,
                journal_path,
            ):
                raise RuntimeError("Workflow branch was manually rewound or diverged")
            record = workflow_store.get(run_id)
        _check_uncompleted_journal(
            journal_path, run_id, steps, record["completed_count"]
        )
        runtime._parent = actual_head
        runtime._restore(record["latest_checkpoint"])
        state = adapter.export_state()

        rows = _completed_rows(workflow_store, runtime, record, state, run_id)
        row_by_index = {row["step_index"]: row for row in rows}

        def make_handler(index, step):
            def checkpointed(arguments, idempotency_key):
                current = workflow_store.get(run_id)
                if index < current["completed_count"]:
                    row = workflow_store.steps(run_id)[index]
                    if row["step_id"] != step.step_id or row["idempotency_key"] != idempotency_key:
                        raise RuntimeError("Cached workflow step is not aligned with durable intent")
                    payload = runtime.store.read_archive(row["result_ref"])
                    return _receipt(current, row, payload["result"])
                if index != current["completed_count"]:
                    raise RuntimeError("Workflow steps must complete in plan order")

                before_state = adapter.export_state()
                expected_entries = [
                    json.loads(row["marker_json"])
                    for row in workflow_store.steps(run_id)
                ]
                if _state_marker(before_state, run_id) != _marker(
                    run_id, branch, plan_hash, expected_entries
                ):
                    raise RuntimeError("Portable workflow state changed before tool execution")

                result = json_copy(tools[step.tool](arguments, idempotency_key))
                result_ref = runtime.store.put_archive(
                    {"kind": _RESULT_KIND, "result": result}
                )
                marker_entry = {
                    "index": index,
                    "step_id": step.step_id,
                    "idempotency_key": idempotency_key,
                    "result_ref": result_ref,
                }
                after_tool = adapter.export_state()
                completed = expected_entries + [marker_entry]
                _import_marker(
                    adapter,
                    after_tool,
                    run_id,
                    _marker(run_id, branch, plan_hash, completed),
                    result_ref=result_ref,
                )
                checkpoint = runtime._commit(
                    current_subgoal=f"workflow {run_id}: completed {step.step_id}"
                )
                row = {
                    **marker_entry,
                    "step_index": index,
                    "checkpoint_id": checkpoint.id,
                }
                updated_record = {**current, "latest_checkpoint": checkpoint.id}
                receipt = _receipt(updated_record, row, result)
                workflow_store.append(
                    run_id,
                    index,
                    marker_entry,
                    receipt,
                    checkpoint.id,
                    current["latest_checkpoint"],
                )
                return receipt

            return checkpointed

        # Different steps may share a tool name, so dispatch by a private name
        # while preserving the original definition in the persisted plan.
        durable_steps = []
        durable_tools = {}
        for index, step in enumerate(steps):
            durable_name = f"workflow-step-{index}"
            durable_tools[durable_name] = make_handler(index, step)
            durable_steps.append(
                Step(step.step_id, durable_name, step.arguments, mode=step.mode)
            )

        runner = DurableRunner(journal_path, durable_tools, run_id)
        expected_receipts = []
        for index, row in row_by_index.items():
            payload = runtime.store.read_archive(row["result_ref"])
            receipt = _receipt(record, row, payload["result"])
            expected_receipts.append(receipt)
            if steps[index].mode == "at_most_once":
                runner.resolve(durable_steps[index], receipt)

        completed_count = record["completed_count"]
        cached = runner.run(durable_steps[:completed_count])
        if cached != expected_receipts:
            raise RuntimeError("Durable receipts are not aligned with workflow checkpoints")
        new = runner.run(durable_steps[completed_count:])
        receipts = cached + new
        return [receipt["result"] for receipt in receipts]
