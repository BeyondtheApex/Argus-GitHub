"""Portable snapshots and conflict-aware three-way state merging.

Native Argus files remain native files. A worker never replaces an entire live
state tree with its older snapshot. Content hashes and keyed JSON/append-only
logs permit independent work to merge without discarding intervening updates.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

from .protocol import ContractError, canonical
from .sqlite_state import MAGIC, SUFFIXES, is_logical
from .sqlite_state import snapshot as snapshot_sqlite

MAX_FILE_BYTES = 40 * 1024 * 1024
_TRANSIENT = {'.git', '__pycache__', 'node_modules', '.venv', '.cache',
              'credentials', 'vault', 'auth.json', '.env', 'daemon.pid'}


def safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or '\\' in value or ':' in value or path.is_absolute():
        raise ContractError('Snapshot path must be relative POSIX')
    if any(part in {'', '.', '..', '.git'} for part in path.parts):
        raise ContractError('Unsafe snapshot path')
    if any(part.rstrip(' .') != part for part in path.parts):
        raise ContractError('Nonportable snapshot path')
    return path.as_posix()


def sha(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def capture(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    if not root.exists():
        return result
    for directory, subdirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        subdirs[:] = sorted(name for name in subdirs
                            if name not in _TRANSIENT and not (base / name).is_symlink())
        for name in sorted(files):
            path = base / name
            if name in _TRANSIENT or name.endswith(('.lock', '.pid', '.sock', '.tmp', '.pyc')):
                continue
            if name.endswith(('-wal', '-shm', '-journal')) and (base / name.rsplit('-', 1)[0]).suffix in SUFFIXES:
                continue
            if path.is_symlink():
                raise ContractError('State snapshots cannot contain symlinks')
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ContractError(f'State artifact requires an explicit large-file policy: {name}')
            data = path.read_bytes()
            if data.startswith(MAGIC):
                data = snapshot_sqlite(path)
            result[safe_path(path.relative_to(root).as_posix())] = data
    return result


def restore(root: Path, files: dict[str, bytes]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve()
    for name, data in files.items():
        relative = safe_path(name)
        target = root / relative
        if not target.resolve().is_relative_to(resolved):
            raise ContractError('Snapshot escapes destination')
        if any(parent.is_symlink() for parent in [target, *target.parents] if parent != resolved.parent):
            raise ContractError('Snapshot destination contains a symlink')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def make_delta(before: dict[str, bytes], after: dict[str, bytes]) -> dict[str, dict[str, Any]]:
    return {name: {'base_sha256': sha(before.get(name)),
                   'content_b64': (base64.b64encode(after[name]).decode() if name in after else None),
                   'sha256': sha(after.get(name))}
            for name in sorted(before.keys() | after.keys()) if before.get(name) != after.get(name)}


_MISSING = object()


def _merge_json(base: Any, current: Any, proposed: Any) -> Any:
    if current == base:
        return proposed
    if proposed == base or proposed == current:
        return current
    if base is _MISSING and isinstance(current, dict) and isinstance(proposed, dict):
        base = {}
    if isinstance(base, dict) and isinstance(current, dict) and isinstance(proposed, dict):
        result = {}
        for key in base.keys() | current.keys() | proposed.keys():
            value = _merge_json(base.get(key, _MISSING), current.get(key, _MISSING),
                                proposed.get(key, _MISSING))
            if value is not _MISSING:
                result[key] = value
        return result
    raise ContractError('Concurrent state edits conflict')


def _merge_jsonl(base: bytes, current: bytes, proposed: bytes) -> bytes:
    # Native event logs retain the original serialized bytes and ordering.
    if current.startswith(base) and proposed.startswith(base):
        existing = set(current.splitlines())
        additions = [line for line in proposed[len(base):].splitlines() if line and line not in existing]
        return current + (b'\n' if current and not current.endswith(b'\n') else b'') + b''.join(line + b'\n' for line in additions)
    rows = [list(map(json.loads, data.splitlines())) for data in (base, current, proposed)]
    if not all(isinstance(row, dict) and isinstance(row.get('id'), str) for block in rows for row in block):
        raise ContractError('Concurrent non-keyed log changes conflict')
    maps = [{row['id']: row for row in block} for block in rows]
    if any(len(mapping) != len(block) for mapping, block in zip(maps, rows)):
        raise ContractError('Duplicate row identities in snapshot')
    merged = _merge_json(*maps)
    order = list(dict.fromkeys([row['id'] for row in rows[1] + rows[2]]))
    return b''.join(canonical(merged[key]) + b'\n' for key in order if key in merged)


def _merge_cost_control(base: dict[str, Any], current: dict[str, Any], proposed: dict[str, Any]) -> dict[str, Any]:
    # Native accounting is daily, keyed by call identity, and keeps explicit
    # unknown-cost liabilities. Timestamp changes are not semantic conflicts.
    if current.get('day') != proposed.get('day'):
        return max((current, proposed), key=lambda row: row.get('day', ''))
    result = _merge_json({key: val for key, val in base.items() if key not in {'updated_at', 'project_roots', 'reservations', 'unresolved'}},
                         {key: val for key, val in current.items() if key not in {'updated_at', 'project_roots', 'reservations', 'unresolved'}},
                         {key: val for key, val in proposed.items() if key not in {'updated_at', 'project_roots', 'reservations', 'unresolved'}})
    result['updated_at'] = max(current.get('updated_at', 0), proposed.get('updated_at', 0))
    result['project_roots'] = sorted(set(current.get('project_roots', [])) | set(proposed.get('project_roots', [])))
    for key in ('reservations', 'unresolved'):
        blocks = [document.get(key, []) for document in (base, current, proposed)]
        if any(any(not row.get('call_id') for row in block) for block in blocks):
            raise ContractError('Native cost-control row has no call identity')
        maps = [{row['call_id']: row for row in block} for block in blocks]
        if any(len(mapping) != len(block) for mapping, block in zip(maps, blocks)):
            raise ContractError('Native cost-control row identity is duplicated')
        result[key] = list(_merge_json(*maps).values())
    return result


def merge_delta(base: dict[str, bytes], current: dict[str, bytes],
                delta: dict[str, dict[str, Any]]) -> dict[str, bytes]:
    """Merge atomically in memory; callers write nothing on any conflict."""
    merged = dict(current)
    for raw_name, change in delta.items():
        name = safe_path(raw_name)
        if any(part in _TRANSIENT for part in PurePosixPath(name).parts):
            raise ContractError('Credential or transient path in result')
        if change.get('base_sha256') != sha(base.get(name)):
            raise ContractError('Worker delta has the wrong base hash')
        encoded = change.get('content_b64')
        try:
            proposed = None if encoded is None else base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ContractError('Invalid snapshot content encoding') from exc
        if proposed is not None and len(proposed) > MAX_FILE_BYTES:
            raise ContractError('Snapshot file exceeds the allowed size')
        if change.get('sha256') != sha(proposed):
            raise ContractError('Worker delta content hash mismatch')
        original, live = base.get(name), current.get(name)
        if live == original or live == proposed:
            value = proposed
        elif proposed == original:
            value = live
        elif Path(name).name in {'knowledge-recall.sqlite3', 'failure-experience.sqlite3', 'mission-view.json'} or name.startswith('map-history-cache/'):
            # These indexes are derived from canonical knowledge documents.
            # Native source_digest validation rebuilds mismatched indexes on
            # first use. Keep one coherent cache, never mix rows from two indexes.
            value = live
        elif live is not None and proposed is not None and (original is not None or name.endswith(('.json', '.jsonl')) or is_logical(proposed)):
            try:
                if name.endswith('.json') or is_logical(proposed):
                    base_json, live_json, proposed_json = json.loads(original or b'{}'), json.loads(live), json.loads(proposed)
                    if name == 'cost-control.json':
                        value = canonical(_merge_cost_control(base_json, live_json, proposed_json)) + b'\n'
                    elif name == 'verticals/catalog.json':
                        value = canonical(max((live_json, proposed_json), key=lambda row: row.get('fetched_at', 0))) + b'\n'
                    elif name == 'letters.json' and set(live_json) == set(proposed_json) == {'started_at'}:
                        value = canonical({'started_at': min(live_json['started_at'], proposed_json['started_at'])}) + b'\n'
                    else:
                        value = canonical(_merge_json(base_json, live_json, proposed_json)) + b'\n'
                elif name.endswith('.jsonl'):
                    value = _merge_jsonl(original or b'', live, proposed)
                else:
                    raise ContractError(f'Concurrent edits conflict: {name}')
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise ContractError(f'Invalid structured state: {name}') from exc
            except ContractError as exc:
                raise ContractError(f'{name}: {exc}') from exc
        else:
            raise ContractError(f'Concurrent deletion/replacement conflicts: {name}')
        if value is None:
            merged.pop(name, None)
        else:
            merged[name] = value
    return merged


def replace_tree(root: Path, before: dict[str, bytes], after: dict[str, bytes]) -> None:
    """Delete only tracked snapshot members; never recursively remove the root."""
    for name in before.keys() - after.keys():
        target = root / safe_path(name)
        if not target.resolve().is_relative_to(root.resolve()) or target.is_symlink():
            raise ContractError('Unsafe deletion path')
        target.unlink(missing_ok=True)
    restore(root, after)
