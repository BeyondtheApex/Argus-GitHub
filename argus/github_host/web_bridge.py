"""Reuse native HTTP handlers inside finite jobs; no HTTP server is deployed."""
from __future__ import annotations

import base64
import hashlib
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from .protocol import ContractError, Project, canonical


def normalize_path(path: str) -> str:
    url = urlsplit(path)
    if url.scheme or url.netloc or not url.path.startswith('/api/') or '..' in url.path or '\\' in path:
        raise ContractError('Only relative native API requests are allowed')
    query = urlencode(sorted((key, value) for key, value in parse_qsl(url.query) if key != 'prewarm'))
    return url.path + ('?' + query if query else '')


def view_key(path: str) -> str:
    return hashlib.sha256(normalize_path(path).encode()).hexdigest()


def make_app(root: Path, project: Project):
    from ..daemon.protocol import daemon_protocol_metadata
    from ..daemon.state import DaemonStatus
    from ..webapi.daemon_services import DaemonServices
    from ..webapi.server import create_app

    def status(life_dir: Path) -> DaemonStatus:
        metadata = daemon_protocol_metadata()
        protocol = metadata['protocol']
        return DaemonStatus(alive=project.status == 'active', pid=None, started_at_iso=None,
            uptime_seconds=None, life_dir=life_dir, backend='github-actions',
            health_state='running' if any(row['state'] == 'running' for row in project.tasks.values()) else 'idle',
            protocol_name=protocol['name'], protocol_major=protocol['major'],
            protocol_minor=protocol['minor'], capabilities=tuple(metadata.get('capabilities', [])), runtime=metadata['runtime'])

    def start(sid: str, **kwargs: Any) -> dict[str, Any]:
        return {'sid': sid, 'rc': 0, 'command_status': 'queued', 'transport': 'github-actions'}

    return create_app(global_root=root, session_roots=[root], auth_token='finite-action',
                      daemon_services=DaemonServices(read_status=status, start=start))


def serialize(response: Any) -> dict[str, Any]:
    return {'status': response.status_code, 'content_type': response.headers.get('content-type', 'application/json'),
            'body_b64': base64.b64encode(response.content).decode(), 'generated_at': time.time()}


def collect_views(root: Path, project: Project) -> dict[str, Any]:
    """Precompute ordinary UI reads; parameterized reads use the same finite RPC."""
    from fastapi.testclient import TestClient
    app = make_app(root, project)
    sid = project.id
    paths = ['/api/meta', '/api/projects', '/api/projects/costs', '/api/metrics', '/api/skill-library',
             '/api/wiki', '/api/knowledge/feed', '/api/verticals', '/api/map-datasets', '/api/plugins']
    paths.extend(['/api/skill-library?sid=' + sid, '/api/wiki?sid=' + sid, '/api/knowledge/feed?limit=20'])
    paths.extend('/api/map-copy/project/' + sid + '?locale=' + locale + '&session_id=' + sid
                 for locale in ('en-US', 'zh-CN'))
    tails = ['/snapshot?compact=true&events_limit=1', '/status', '/journal?n=20', '/config', '/identity',
             '/transcript?n=30', '/transcript?n=120', '/snapshot?compact=false&events_limit=40',
             '/events?limit=80&view=ui', '/events?limit=120&view=ui', '/events?limit=500&view=ui', '/artifacts',
             '/artifacts?include_reading=true', '/map', '/map-info', '/map-history', '/map-notes', '/wiki', '/learning',
             '/advisor/config', '/advisor/consultations', '/research/method', '/cost-control']
    paths.extend('/api/projects/' + sid + tail for tail in tails)
    views = {}
    with TestClient(app, headers={'Authorization': 'Bearer finite-action'}) as client:
        for path in paths:
            try:
                response = client.get(path)
                views[view_key(path)] = {'path': normalize_path(path), **serialize(response)}
            except Exception as exc:
                # A failing optional projection cannot erase native state.
                body = canonical({'detail': f'Projection unavailable: {type(exc).__name__}'})
                views[view_key(path)] = {'path': normalize_path(path), 'status': 503,
                    'content_type': 'application/json', 'body_b64': base64.b64encode(body).decode()}
    return views


def execute_request(root: Path, project: Project, request: dict[str, Any]) -> dict[str, Any]:
    from fastapi.testclient import TestClient
    path = normalize_path(str(request['path']))
    method = str(request.get('method', 'GET')).upper()
    if method not in {'GET', 'POST', 'PATCH', 'DELETE'}:
        raise ContractError('Unsupported API method')
    if method == 'DELETE' and path.split('?')[0] == '/api/projects/' + project.id:
        raise ContractError('Project cancellation must pass through authenticated repository intake')
    if method == 'POST' and path.split('?')[0] == '/api/projects':
        raise ContractError('Each repository has one durable identity; initialize another repository for another project')
    if any(word in path.split('?')[0] for word in ('/pairing', '/runtime/source-update', '/daemon/',
            '/continuous', '/launch-cwd', '/workdir', '/reset', '/trash', '/plugins/')) or path == '/api/daemons':
        raise ContractError('This machine operation is managed by repository controls')
    if path.startswith('/api/projects/') and path.split('/')[3] != project.id:
        raise ContractError('API command targets another project identity')
    body = request.get('body')
    # Native Manager still classifies and handles the request. Work is queued
    # into the project DAG instead of taking the local immediate-execution route.
    if '/message' in path and isinstance(body, dict):
        body = {**body, 'route_override': 'chat' if body.get('route_override') == 'chat' else 'task'}
    with TestClient(make_app(root, project), headers={'Authorization': 'Bearer finite-action'}) as client:
        kwargs = {'json': body} if body is not None else {}
        multipart = request.get('multipart')
        if multipart:
            from .protocol import identifier
            from .snapshots import safe_path
            uploads = root / 'repository-uploads' / identifier(request['upload_id'])
            kwargs = {'files': [(row['name'], (Path(row['filename']).name,
                (uploads / safe_path(row['path'])).read_bytes(), row.get('type', 'application/octet-stream')))
                for row in multipart]}
        response = client.request(method, path, **kwargs)
        return {'path': path, 'method': method, **serialize(response)}


def persist_views(store: Any, views: dict[str, Any]) -> None:
    if not isinstance(views, dict) or len(views) > 300:
        raise ContractError('Invalid view projection bundle')
    index = store.read('views/index.json', {'schema': 1, 'views': {}})
    for key, value in views.items():
        if key != view_key(value['path']):
            raise ContractError('View key does not match its native API path')
        store.write('views/' + key + '.json', value)
        index['views'][key] = {'path': value['path'], 'generated_at': value.get('generated_at', time.time())}
    index['updated_at'] = time.time()
    store.write('views/index.json', index)
