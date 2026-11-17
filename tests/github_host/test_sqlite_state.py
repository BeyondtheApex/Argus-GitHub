from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from argus.github_host.snapshots import capture, make_delta, merge_delta, restore
from argus.github_host.sqlite_state import materialize


def test_path_keys_rebase_without_becoming_duplicate_rows(tmp_path: Path):
    from argus.github_host.portability import rebase
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    with closing(sqlite3.connect(first / 'paths.db')) as db:
        db.execute('CREATE TABLE paths(path TEXT PRIMARY KEY, body TEXT)')
        db.execute('INSERT INTO paths VALUES (?,?)', (str(first / 'notes'), 'before'))
        db.commit()
    initial = rebase(capture(first), first, tmp_path / 'source', portable=True)
    restore(second, rebase(initial, second, tmp_path / 'source-2', portable=False))
    materialize(second)
    with closing(sqlite3.connect(second / 'paths.db')) as db:
        db.execute('UPDATE paths SET body=? WHERE path=?', ('after', str(second / 'notes')))
        db.commit()
    changed = rebase(capture(second), second, tmp_path / 'source-2', portable=True)
    import json
    before_rows = json.loads(initial['paths.db'])['tables']['paths']['rows']
    after_rows = json.loads(changed['paths.db'])['tables']['paths']['rows']
    assert before_rows.keys() == after_rows.keys()
    assert len(after_rows) == 1
    assert next(iter(after_rows.values()))['values']['body'] == 'after'


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
