"""Self-contained, hash-verified public-state bundles. No workspace mutation."""

import json
import os
from pathlib import Path
import tempfile

from statetree.core.commit import canonical_json, digest, validate_id
from statetree.core.state import AgentState, json_copy


def _validate_bundle(bundle):
    bundle = json_copy(bundle)
    if type(bundle) is not dict or set(bundle) != {'format', 'version', 'state', 'archives', 'digest'}:
        raise ValueError('Malformed portable bundle')
    if bundle['format'] != 'statetree.bundle' or type(bundle['version']) is not int or bundle['version'] != 1:
        raise ValueError('Unsupported portable bundle')
    identifier = bundle.pop('digest')
    validate_id(identifier)
    if digest(bundle) != identifier:
        raise ValueError('Portable bundle failed its content hash check')
    state = AgentState.from_dict(bundle['state'])
    archives = bundle['archives']
    if type(archives) is not dict or set(archives) != set(state.archive_ids):
        raise ValueError('Bundle must contain exactly its declared archive references')
    for archive_id, value in archives.items():
        validate_id(archive_id)
        if digest(value) != archive_id:
            raise ValueError('Portable archive failed its content hash check')
    return state, archives


def export_bundle(state: AgentState, store=None):
    state = AgentState.from_dict(state.to_dict())
    if state.archive_ids and store is None:
        raise ValueError('An archive store is required for referenced evidence')
    archives = {identifier: store.read_archive(identifier) for identifier in state.archive_ids}
    payload = {'format': 'statetree.bundle', 'version': 1,
               'state': state.to_dict(), 'archives': archives}
    return {**payload, 'digest': digest(payload)}


def import_bundle(bundle, store=None):
    state, archives = _validate_bundle(bundle)
    if archives and store is None:
        raise ValueError('A destination archive store is required')
    # Validate every object before publication. Interrupted writes leave only
    # immutable orphan blobs; no live agent or branch is changed by importing.
    for identifier, value in archives.items():
        if store.put_archive(value) != identifier:
            raise ValueError('Destination archive store returned an inconsistent hash')
    return state


def write_bundle(path, bundle):
    _validate_bundle(bundle)
    path = Path(path)
    fd, pending = tempfile.mkstemp(prefix='.statetree-bundle-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(canonical_json(bundle))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)


def read_bundle(path, *, max_bytes=16 * 1024 * 1024):
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError('max_bytes must be positive')
    with Path(path).open('rb') as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError('Portable bundle exceeds the size limit')
    try:
        bundle = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise ValueError('Invalid portable bundle JSON') from error
    _validate_bundle(bundle)
    return bundle
