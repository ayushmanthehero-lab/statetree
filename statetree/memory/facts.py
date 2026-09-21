"""Versioned structured facts with deterministic lifecycle policy.

The module deliberately does not infer facts or contradictions from prose.
Callers choose explicit keys, values, evidence references, and dependency
versions.  A new observation for the same key is the only automatic signal
that the prior version has been superseded.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence


class FactValidationError(ValueError):
    """Raised when a fact or serialized memory violates the schema."""


class FactBudgetError(ValueError):
    """Raised when a bounded fact projection cannot be measured or fit."""


@dataclass(frozen=True)
class FactMaterialization:
    """A bounded active-fact projection and its deterministic accounting."""

    facts: dict[str, dict[str, Any]]
    estimated_count: int
    counting_method: str
    omitted_keys: tuple[str, ...]


_STATUSES = frozenset({"active", "archived", "invalid", "deleted"})
_INVALIDATION_ACTIONS = frozenset(
    {"superseded", "archive", "invalidate", "delete", "dependency_invalidated"}
)
_BASE_RECORD_FIELDS = frozenset(
    {"key", "version", "value", "evidence", "dependencies", "status"}
)


def _validate_key(key: Any, *, label: str = "Fact key") -> str:
    if not isinstance(key, str) or not key or key != key.strip():
        raise FactValidationError(f"{label} must be a nonempty trimmed string")
    return key


def _validate_json_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise FactValidationError("JSON object keys must be strings")
            _validate_json_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _validate_json_keys(child)


def _json_copy(value: Any, *, label: str) -> Any:
    _validate_json_keys(value)
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return json.loads(serialized)
    except (TypeError, ValueError) as error:
        raise FactValidationError(f"{label} must be finite JSON data") from error


def _json_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _validate_evidence(evidence: Any, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(evidence, list):
        raise FactValidationError("Evidence must be a list of strings")
    if not evidence and not allow_empty:
        raise FactValidationError("Evidence must contain at least one reference")
    if any(not isinstance(item, str) or not item or item != item.strip() for item in evidence):
        raise FactValidationError("Evidence references must be nonempty trimmed strings")
    if len(set(evidence)) != len(evidence):
        raise FactValidationError("Evidence references must be unique")
    return list(evidence)


def _validate_dependencies(dependencies: Any) -> dict[str, int]:
    if dependencies is None:
        return {}
    if not isinstance(dependencies, dict):
        raise FactValidationError("Dependencies must be an object of key-to-version entries")
    validated: dict[str, int] = {}
    for key, version in dependencies.items():
        key = _validate_key(key, label="Dependency key")
        if type(version) is not int or version < 1:
            raise FactValidationError("Dependency versions must be positive integers")
        validated[key] = version
    return dict(sorted(validated.items()))


def _public_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(record))


def _projection_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy({
        "version": record["version"],
        "value": record["value"],
        "evidence": record["evidence"],
        "dependencies": record["dependencies"],
    })


class FactMemory:
    """Maintain explicit, versioned facts and their evidence history."""

    SCHEMA_VERSION = 1

    def __init__(self) -> None:
        self._records: dict[str, list[dict[str, Any]]] = {}

    def _active_record(self, key: str) -> dict[str, Any] | None:
        records = self._records.get(key)
        if records and records[-1]["status"] == "active":
            return records[-1]
        return None

    def _latest_version(self, key: str) -> int:
        records = self._records.get(key)
        return records[-1]["version"] if records else 0

    def _require_active(self, key: Any) -> dict[str, Any]:
        key = _validate_key(key)
        active = self._active_record(key)
        if active is None:
            raise KeyError(f"No active fact exists for {key!r}")
        return active

    def _validate_live_dependencies(
        self,
        key: str,
        dependencies: Mapping[str, int],
        replaced: Mapping[str, Any] | None,
    ) -> None:
        for dependency_key, version in dependencies.items():
            if dependency_key == key:
                raise FactValidationError(f"Dependency cycle for fact {key!r}")
            active = self._active_record(dependency_key)
            if active is None:
                raise FactValidationError(
                    f"Dependency {dependency_key!r} has no active version"
                )
            if active["version"] != version:
                raise FactValidationError(
                    f"Dependency {dependency_key!r} requires active version "
                    f"{active['version']}, not {version}"
                )
            if replaced is not None and self._depends_on(
                active, key, replaced["version"], seen=set()
            ):
                raise FactValidationError(
                    f"Dependency cycle would invalidate replacement for fact {key!r}"
                )

    def _depends_on(
        self,
        record: Mapping[str, Any],
        target_key: str,
        target_version: int,
        *,
        seen: set[tuple[str, int]],
    ) -> bool:
        identity = (record["key"], record["version"])
        if identity in seen:
            return False
        seen.add(identity)
        for key, version in record["dependencies"].items():
            if (key, version) == (target_key, target_version):
                return True
            dependency = self._active_record(key)
            if dependency is not None and dependency["version"] == version:
                if self._depends_on(
                    dependency, target_key, target_version, seen=seen
                ):
                    return True
        return False

    def _invalidate_dependents(
        self,
        source_key: str,
        source_version: int,
        *,
        action: str,
    ) -> None:
        queue: list[tuple[str, int, str]] = [(source_key, source_version, action)]
        while queue:
            dependency_key, dependency_version, dependency_action = queue.pop(0)
            for key in sorted(self._records):
                record = self._active_record(key)
                if record is None:
                    continue
                if record["dependencies"].get(dependency_key) != dependency_version:
                    continue
                record["status"] = "invalid"
                record["invalidated_by"] = {
                    "key": dependency_key,
                    "version": dependency_version,
                    "action": dependency_action,
                }
                queue.append((record["key"], record["version"], "dependency_invalidated"))

    def observe(
        self,
        key: str,
        value: Any,
        *,
        evidence: list[str],
        dependencies: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Record an observation, superseding the active version of its key."""

        key = _validate_key(key)
        copied_value = _json_copy(value, label="Fact value")
        copied_evidence = _validate_evidence(evidence)
        copied_dependencies = _validate_dependencies(dependencies)
        current = self._active_record(key)

        if current is not None and (
            _json_text(current["value"]) == _json_text(copied_value)
            and current["evidence"] == copied_evidence
            and current["dependencies"] == copied_dependencies
        ):
            return _public_record(current)

        self._validate_live_dependencies(key, copied_dependencies, current)
        version = self._latest_version(key) + 1

        if current is not None:
            current["status"] = "archived"
            self._invalidate_dependents(
                key, current["version"], action="superseded"
            )

        record = {
            "key": key,
            "version": version,
            "value": copied_value,
            "evidence": copied_evidence,
            "dependencies": copied_dependencies,
            "status": "active",
        }
        self._records.setdefault(key, []).append(record)
        return _public_record(record)

    def invalidate(self, key: str) -> dict[str, Any]:
        """Invalidate an active fact and all active dependents."""

        record = self._require_active(key)
        record["status"] = "invalid"
        record["invalidated_by"] = {
            "key": record["key"],
            "version": record["version"],
            "action": "invalidate",
        }
        self._invalidate_dependents(
            record["key"], record["version"], action="invalidate"
        )
        return _public_record(record)

    def archive(self, key: str) -> dict[str, Any]:
        """Archive an active fact and invalidate facts that depend on it."""

        record = self._require_active(key)
        record["status"] = "archived"
        self._invalidate_dependents(
            record["key"], record["version"], action="archive"
        )
        return _public_record(record)

    def delete(self, key: str) -> dict[str, Any]:
        """Archive an active fact and append a versioned deletion tombstone."""

        record = self._require_active(key)
        record["status"] = "archived"
        self._invalidate_dependents(
            record["key"], record["version"], action="delete"
        )
        tombstone = {
            "key": record["key"],
            "version": record["version"] + 1,
            "value": None,
            "evidence": [],
            "dependencies": {},
            "status": "deleted",
        }
        self._records[record["key"]].append(tombstone)
        return _public_record(tombstone)

    def active_facts(self) -> dict[str, dict[str, Any]]:
        """Return the JSON-ready active projection for ``AgentState.facts``."""

        return {
            key: _projection_record(record)
            for key in sorted(self._records)
            if (record := self._active_record(key)) is not None
        }

    def to_dict(self) -> dict[str, Any]:
        """Return the complete JSON-ready lifecycle for ``AgentState.memory``."""

        records = [
            _public_record(record)
            for key in sorted(self._records)
            for record in self._records[key]
        ]
        return {"schema_version": self.SCHEMA_VERSION, "records": records}

    @classmethod
    def from_dict(cls, payload: Any) -> FactMemory:
        """Load and strictly validate a complete serialized lifecycle."""

        if not isinstance(payload, dict):
            raise FactValidationError("Serialized fact memory must be an object")
        if set(payload) != {"schema_version", "records"}:
            raise FactValidationError(
                "Serialized fact memory requires only schema_version and records"
            )
        if (type(payload["schema_version"]) is not int
                or payload["schema_version"] != cls.SCHEMA_VERSION):
            raise FactValidationError("Unsupported fact memory schema version")
        if not isinstance(payload["records"], list):
            raise FactValidationError("Serialized fact records must be a list")

        memory = cls()
        identities: set[tuple[str, int]] = set()
        copied_records: list[dict[str, Any]] = []
        for raw in payload["records"]:
            record = cls._validated_serialized_record(raw)
            identity = (record["key"], record["version"])
            if identity in identities:
                raise FactValidationError(f"Duplicate fact record {identity!r}")
            identities.add(identity)
            copied_records.append(record)

        for record in copied_records:
            memory._records.setdefault(record["key"], []).append(record)
        for records in memory._records.values():
            records.sort(key=lambda item: item["version"])
        memory._validate_serialized_lifecycle(identities)
        return memory

    @classmethod
    def _validated_serialized_record(cls, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise FactValidationError("Each fact record must be an object")
        status = raw.get("status")
        if status not in _STATUSES:
            raise FactValidationError("Fact record status is invalid")
        expected = set(_BASE_RECORD_FIELDS)
        if status == "invalid":
            expected.add("invalidated_by")
        if set(raw) != expected:
            raise FactValidationError("Fact record contains missing or unknown fields")

        key = _validate_key(raw["key"])
        version = raw["version"]
        if type(version) is not int or version < 1:
            raise FactValidationError("Fact record version must be a positive integer")
        value = _json_copy(raw["value"], label="Fact value")
        evidence = _validate_evidence(raw["evidence"], allow_empty=status == "deleted")
        dependencies = _validate_dependencies(raw["dependencies"])
        record: dict[str, Any] = {
            "key": key,
            "version": version,
            "value": value,
            "evidence": evidence,
            "dependencies": dependencies,
            "status": status,
        }

        if status == "deleted":
            if value is not None or evidence or dependencies:
                raise FactValidationError("Deletion tombstones cannot carry fact data")
        elif not evidence:
            raise FactValidationError("Non-deleted records require evidence")

        if status == "invalid":
            invalidated_by = raw["invalidated_by"]
            if not isinstance(invalidated_by, dict) or set(invalidated_by) != {
                "key", "version", "action"
            }:
                raise FactValidationError("Invalidation provenance is malformed")
            source_key = _validate_key(
                invalidated_by["key"], label="Invalidation source key"
            )
            source_version = invalidated_by["version"]
            if type(source_version) is not int or source_version < 1:
                raise FactValidationError(
                    "Invalidation source version must be a positive integer"
                )
            action = invalidated_by["action"]
            if action not in _INVALIDATION_ACTIONS:
                raise FactValidationError("Invalidation action is invalid")
            record["invalidated_by"] = {
                "key": source_key, "version": source_version, "action": action
            }
        return record

    def _validate_serialized_lifecycle(
        self, identities: set[tuple[str, int]]
    ) -> None:
        for key, records in self._records.items():
            versions = [record["version"] for record in records]
            if versions != list(range(1, len(records) + 1)):
                raise FactValidationError(
                    f"Fact {key!r} versions must be contiguous from one"
                )
            active = [record for record in records if record["status"] == "active"]
            if len(active) > 1 or (active and active[0] is not records[-1]):
                raise FactValidationError(
                    f"Fact {key!r} can only have its latest record active"
                )
            for index, record in enumerate(records):
                if record["status"] == "deleted" and (
                    index == 0 or records[index - 1]["status"] != "archived"
                ):
                    raise FactValidationError(
                        f"Fact {key!r} deletion tombstone requires an archived predecessor"
                    )

        for records in self._records.values():
            for record in records:
                for dependency_key, dependency_version in record["dependencies"].items():
                    if (dependency_key, dependency_version) not in identities:
                        raise FactValidationError(
                            f"Fact dependency {(dependency_key, dependency_version)!r} is missing"
                        )
                    if record["status"] == "active":
                        active = self._active_record(dependency_key)
                        if active is None or active["version"] != dependency_version:
                            raise FactValidationError(
                                "Active fact has a stale or inactive dependency"
                            )
                if record["status"] == "invalid":
                    source = record["invalidated_by"]
                    source_identity = (source["key"], source["version"])
                    record_identity = (record["key"], record["version"])
                    if source_identity not in identities:
                        raise FactValidationError(
                            "Invalidation provenance references a missing fact version"
                        )
                    if source_identity == record_identity:
                        if source["action"] != "invalidate":
                            raise FactValidationError(
                                "Self-invalidation provenance requires invalidate action"
                            )
                    elif record["dependencies"].get(source["key"]) != source["version"]:
                        raise FactValidationError(
                            "Invalidation provenance must name an exact dependency"
                        )

        for key in sorted(self._records):
            record = self._active_record(key)
            if record is not None:
                self._assert_no_active_cycle(record, path=set(), complete=set())

    def _assert_no_active_cycle(
        self,
        record: Mapping[str, Any],
        *,
        path: set[tuple[str, int]],
        complete: set[tuple[str, int]],
    ) -> None:
        identity = (record["key"], record["version"])
        if identity in path:
            raise FactValidationError("Active fact dependency cycle is invalid")
        if identity in complete:
            return
        next_path = set(path)
        next_path.add(identity)
        for key, version in record["dependencies"].items():
            dependency = self._active_record(key)
            if dependency is not None and dependency["version"] == version:
                self._assert_no_active_cycle(
                    dependency, path=next_path, complete=complete
                )
        complete.add(identity)

    def materialize(
        self,
        budget: int,
        counter: Callable[[str], int] | None = None,
        *,
        required: Sequence[str] = (),
    ) -> FactMaterialization:
        """Select active facts deterministically within a serialized budget.

        ``required`` facts include their transitive dependency closure and fail
        explicitly when that closure does not fit.  Remaining facts are tried
        in lexical key order and included only with their complete closure.
        """

        if type(budget) is not int or budget < 1:
            raise ValueError("Fact materialization budget must be a positive integer")
        if counter is not None and not callable(counter):
            raise ValueError("Fact materialization counter must be callable")
        if isinstance(required, (str, bytes)) or not isinstance(required, Sequence):
            raise FactValidationError("Required facts must be a sequence of unique keys")
        required_keys = list(required)
        if len(set(required_keys)) != len(required_keys):
            raise FactValidationError("Required fact keys must be unique")
        for key in required_keys:
            _validate_key(key, label="Required fact key")
            if self._active_record(key) is None:
                raise FactValidationError(f"Required fact {key!r} is not active")

        active = self.active_facts()

        def closure(keys: Sequence[str]) -> set[str]:
            selected: set[str] = set()
            pending = list(keys)
            while pending:
                key = pending.pop()
                if key in selected:
                    continue
                record = active[key]
                selected.add(key)
                pending.extend(record["dependencies"])
            return selected

        def projection(keys: set[str]) -> dict[str, dict[str, Any]]:
            return {key: copy.deepcopy(active[key]) for key in sorted(keys)}

        def count(facts: Mapping[str, Any]) -> int:
            serialized = _json_text(facts)
            measured = (
                counter(serialized) if counter is not None
                else len(serialized.encode("utf-8"))
            )
            if type(measured) is not int or measured < 0:
                raise FactBudgetError("Fact counter must return a nonnegative integer")
            return measured

        selected = closure(required_keys)
        required_projection = projection(selected)
        required_count = count(required_projection)
        if required_count > budget:
            raise FactBudgetError(
                f"Required facts need {required_count}; budget is {budget}"
            )

        for key in sorted(active):
            candidate = selected | closure([key])
            if count(projection(candidate)) <= budget:
                selected = candidate

        facts = projection(selected)
        final_count = count(facts)
        omitted = tuple(key for key in sorted(active) if key not in selected)
        return FactMaterialization(
            facts=facts,
            estimated_count=final_count,
            counting_method=(
                "custom_counter"
                if counter is not None
                else "serialized_json_utf8_bytes_estimate"
            ),
            omitted_keys=omitted,
        )
