"""Versioned, content-addressed checkpoint manifests."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def validate_id(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('Expected a full 64-character StateTree content hash')


def validate_branch(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,127}', value):
        raise ValueError('Invalid branch name')


@dataclass(frozen=True)
class StateTreeCommit:
    id: str
    parent: str | None
    branch: str
    goal: str
    current_subgoal: str | None
    strands_snapshot_path: str
    workspace_sha: str
    verification_status: str
    created_at: str
    snapshot_digest: str = ''
    verification: dict[str, Any] | None = None
    schema_version: int = 1

    @classmethod
    def create(cls, *, parent, branch, goal, current_subgoal,
               strands_snapshot_path, workspace_sha,
               verification_status='unverified', snapshot_digest='',
               verification=None):
        payload = dict(
            parent=parent, branch=branch, goal=goal, current_subgoal=current_subgoal,
            strands_snapshot_path=strands_snapshot_path, workspace_sha=workspace_sha,
            verification_status=verification_status,
            created_at=datetime.now(timezone.utc).isoformat(),
            snapshot_digest=snapshot_digest, verification=verification, schema_version=1,
        )
        payload = json.loads(canonical_json(payload))
        commit = cls(id=digest(payload), **payload)
        commit.validate()
        return commit

    def validate(self):
        validate_id(self.id)
        validate_branch(self.branch)
        if self.parent is not None:
            validate_id(self.parent)
        if self.schema_version != 1:
            raise ValueError('Unsupported checkpoint schema')
        if self.verification_status not in ('verified', 'unverified'):
            raise ValueError('Invalid verification status')
        if self.verification_status == 'verified':
            if not self.verification or self.verification.get('passed') is not True:
                raise ValueError('Verified checkpoints require passed verification evidence')
        data = self.to_dict()
        data.pop('id')
        if digest(data) != self.id:
            raise ValueError('Checkpoint manifest failed its content hash check')

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
