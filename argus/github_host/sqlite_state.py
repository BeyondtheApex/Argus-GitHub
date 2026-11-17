"""Consistent, mergeable SQLite snapshots without executing SQL in publishers.

Only execution jobs materialize logical snapshots. Tables retain their keys,
schema, BLOBs and values; conflicting updates still require reconciliation.
"""
from __future__ import annotations

import base64
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .protocol import ContractError, canonical, digest

MAGIC = b'SQLite format 3\x00'
SUFFIXES = {'.sqlite', '.sqlite3', '.db'}


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _encode(value: Any) -> Any:
    return {'$blob': base64.b64encode(value).decode()} if isinstance(value, bytes) else value


def _decode(value: Any) -> Any:
    return base64.b64decode(value['$blob'], validate=True) if isinstance(value, dict) and set(value) == {'$blob'} else value


def snapshot(path: Path) -> bytes:
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as disk, closing(sqlite3.connect(':memory:')) as db:
        disk.backup(db)
        schema = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name").fetchall()
        # Extension-owned virtual tables must keep their exact engine format.
        # The connection backup includes WAL changes even for these databases.
        if any('CREATE VIRTUAL TABLE' in row[3].upper() for row in schema):
            return db.serialize()
        tables = {}
        for kind, name, _, sql in schema:
            if kind != 'table' or name.startswith('sqlite_'):
                continue
            info = db.execute('PRAGMA table_info(' + quote(name) + ')').fetchall()
            columns = [row[1] for row in info]
            primary = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
            rows = {}
            for values in db.execute('SELECT ' + ','.join(quote(col) for col in columns) + ' FROM ' + quote(name)):
                row = {col: _encode(value) for col, value in zip(columns, values)}
                key = digest([row[col] for col in primary] if primary else row)
                if key in rows:
                    if primary:
                        raise ContractError('SQLite snapshot contains duplicate primary keys')
                    rows[key]['count'] += 1
                else:
                    rows[key] = {'values': row, 'count': 1}
            tables[name] = {'columns': columns, 'primary_key': primary, 'rows': rows}
        sequences = {}
        if any(row[1] == 'sqlite_sequence' for row in schema):
            sequences = dict(db.execute('SELECT name,seq FROM sqlite_sequence'))
        return canonical({'$argus_sqlite': 1, 'schema': {row[1]: {'type': row[0], 'table': row[2], 'sql': row[3]}
            for row in schema if not row[1].startswith('sqlite_')}, 'tables': tables, 'sequences': sequences}) + b'\n'


def is_logical(data: bytes) -> bool:
    return data.startswith(b'{"$argus_sqlite":1,')


def materialize(root: Path) -> None:
    """Run only in the isolated native execution context, never a publisher."""
    for path in root.rglob('*'):
        if not path.is_file() or path.suffix not in SUFFIXES:
            continue
        data = path.read_bytes()
        if not is_logical(data):
            continue
        document = json.loads(data)
        temporary = path.with_name(path.name + '.restoring')
        temporary.unlink(missing_ok=True)
        try:
            with closing(sqlite3.connect(temporary)) as db:
                db.execute('PRAGMA foreign_keys=OFF')
                for row in document['schema'].values():
                    if row['type'] == 'table':
                        db.execute(row['sql'])
                for name, table in document['tables'].items():
                    columns = table['columns']
                    sql = 'INSERT INTO ' + quote(name) + '(' + ','.join(quote(col) for col in columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')'
                    for record in table['rows'].values():
                        for _ in range(record['count']):
                            db.execute(sql, [_decode(record['values'][col]) for col in columns])
                for name, sequence in document.get('sequences', {}).items():
                    db.execute('DELETE FROM sqlite_sequence WHERE name=?', (name,))
                    db.execute('INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)', (name, sequence))
                for row in document['schema'].values():
                    if row['type'] != 'table':
                        db.execute(row['sql'])
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ContractError('Restored database failed integrity check')
                db.commit()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
