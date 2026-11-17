from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from argus.github_host.snapshots import capture, make_delta, merge_delta, restore
from argus.github_host.sqlite_state import materialize


def test_sqlite_wal_blob_and_disjoint_rows_survive_parallel_snapshots(tmp_path: Path):
    base = tmp_path / 'base'
    base.mkdir()
    with closing(sqlite3.connect(base / 'memory.sqlite3')) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY, body BLOB, revision INTEGER)')
        db.execute('INSERT INTO memories VALUES (?,?,?)', ('initial', b'\x00\xff', 1))
        db.commit()
        initial = capture(base)
        assert set(initial) == {'memory.sqlite3'}
    versions = []
    for name in ('a', 'b'):
        root = tmp_path / name
        restore(root, initial)
        materialize(root)
        with closing(sqlite3.connect(root / 'memory.sqlite3')) as db:
            db.execute('INSERT INTO memories VALUES (?,?,?)', (name, name.encode(), 2))
            db.commit()
        versions.append(capture(root))
    combined = merge_delta(initial, versions[0], make_delta(initial, versions[1]))
    restored = tmp_path / 'restored'
    restore(restored, combined)
    materialize(restored)
    with closing(sqlite3.connect(restored / 'memory.sqlite3')) as db:
        rows = db.execute('SELECT * FROM memories ORDER BY id').fetchall()
    assert rows == [('a', b'a', 2), ('b', b'b', 2), ('initial', b'\x00\xff', 1)]
