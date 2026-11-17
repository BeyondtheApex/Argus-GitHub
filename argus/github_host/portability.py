"""Rebase explicit runtime paths without summarizing or rewriting model memory."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .protocol import canonical
from .sqlite_state import is_logical

STATE = '@ARGUS_STATE@'
SOURCE = '@ARGUS_SOURCE@'


def rebase(files: dict[str, bytes], state_root: Path, source: Path, *, portable: bool) -> dict[str, bytes]:
    pairs = [(str(state_root.resolve()), STATE), (str(source.resolve()), SOURCE)]
    pairs += [(str(state_root.resolve()).replace('\\', '/'), STATE),
              (str(source.resolve()).replace('\\', '/'), SOURCE)]
    if not portable:
        pairs = [(STATE, str(state_root.resolve())), (SOURCE, str(source.resolve()))]
    pairs.sort(key=lambda row: len(row[0]), reverse=True)

    def strings(value: Any) -> Any:
        if isinstance(value, str):
            for before, after in pairs:
                value = value.replace(before, after)
            return value
        if isinstance(value, list):
            return [strings(row) for row in value]
        if isinstance(value, dict):
            return {strings(key): strings(row) for key, row in value.items()}
        return value

    result = {}
    for name, data in files.items():
        try:
            if name.endswith('.json') or is_logical(data):
                result[name] = canonical(strings(json.loads(data))) + b'\n'
            elif name.endswith('.jsonl'):
                result[name] = b''.join(canonical(strings(json.loads(row))) + b'\n' for row in data.splitlines() if row)
            elif name.endswith(('.md', '.txt', '.toml', '.yaml', '.yml')):
                result[name] = strings(data.decode('utf-8')).encode('utf-8')
            else:
                result[name] = data
        except (UnicodeError, ValueError):
            result[name] = data  # preserve opaque evidence byte-for-byte
    return result


def configure_provider_state(root: Path, *, owner: str) -> None:
    """Use a private persisted provider home, never the host user's login files."""
    from ..core.portable_filename import portable_filename_component
    home = root / 'provider-sessions' / portable_filename_component(owner, windows=True)
    for name, subdir in [('CODEX_HOME', 'codex'), ('CLAUDE_CONFIG_DIR', 'claude')]:
        path = home / subdir
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    if os.environ.get('OPENAI_API_KEY'):
        os.environ['CODEX_API_KEY'] = os.environ['OPENAI_API_KEY']
