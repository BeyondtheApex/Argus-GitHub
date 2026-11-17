from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from argus.github_host.protocol import ContractError, Project
from argus.github_host.web_bridge import (
    collect_history_views,
    execute_request,
    normalize_path,
    persist_views,
    view_key,
)


def test_view_keys_ignore_prewarm_and_order_but_not_meaning():
    assert view_key('/api/x?b=2&a=1&prewarm=true') == view_key('/api/x?a=1&b=2')
    assert view_key('/api/x?a=2') != view_key('/api/x?a=1')
    with pytest.raises(ContractError):
        normalize_path('https://attacker.invalid/api/x')


def test_native_api_is_available_without_listening_server(tmp_path: Path):
    project = Project(1, 'owner/repo', 'Persistent objective', 'a' * 40, 'b' * 40)
    receipt = execute_request(tmp_path, project, {'path': '/api/meta'})
    assert receipt['status'] == 200
    meta = json.loads(base64.b64decode(receipt['body_b64']))
    assert meta['authentication']['authenticated']
    with pytest.raises(ContractError):
        execute_request(tmp_path, project, {'path': '/api/projects/other/identity', 'method': 'POST'})
    with pytest.raises(ContractError):
        execute_request(tmp_path, project, {'path': '/api/projects/' + project.id + '/daemon/start', 'method': 'POST'})


def test_history_projections_preserve_every_native_page_and_retire_old_cursors(tmp_path: Path, monkeypatch):
    from urllib.parse import urlencode

    from fastapi.testclient import TestClient

    from argus.core.session import SessionMeta, write_session_meta
    from argus.github_host.git_store import GitStore
    from argus.life.memory import BacklogItem, LifeMemory
    from argus.webapi import map_history
    from argus.webapi.server import create_app

    sid = 'history-test'
    write_session_meta(tmp_path, SessionMeta(id=sid, created=1, last_active=1))
    life = tmp_path / 'projects' / sid
    memory = LifeMemory.open(life)
    memory.backlog.add(BacklogItem(id='a', ts=1, title='Research', objective='Compare methods', status='running'))
    rows = [{'type': 'life.mission.started', 'item_id': 'a', 'ts': 2}]
    rows += [{'type': 'round.review.completed', 'event_id': f'review-{n}', 'item_id': 'a', 'ts': n + 3}
             for n in range(5)]
    (life / 'events.jsonl').write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')
    monkeypatch.setattr(map_history, 'PAGE_EVENTS', 1)
    monkeypatch.setattr(map_history, 'PAGE_BYTES', 200)
    path = f'/api/projects/{sid}/map-history'
    with TestClient(create_app(global_root=tmp_path)) as client:
        views = collect_history_views(client, path)
    seen, current = [], path
    for _ in range(20):
        page = json.loads(base64.b64decode(views[view_key(current)]['body_b64']))
        seen.extend(event['id'] for event in page['events'])
        next_path = path + '?' + urlencode({'after': page['history_cursor'], 'task_after': page['cursor']})
        if normalize_path(current) == normalize_path(next_path):
            assert page['events'] == page['tasks'] == []
            assert not page['history_loading']
            break
        current = next_path
    else:
        pytest.fail('Persisted history never reached its terminal page')
    assert {f'review-{n}' for n in range(5)} <= set(seen)
    assert len(seen) == len(set(seen))
    with GitStore('owner/repo') as store:
        stale = path + '?after=previous-epoch'
        persist_views(store, {view_key(stale): {'path': stale, 'status': 200}})
        persist_views(store, views)
        assert view_key(stale) not in store.read('views/index.json')['views']
        assert not (store.root / 'views' / (view_key(stale) + '.json')).exists()
