from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from argus.github_host.protocol import ContractError, Project
from argus.github_host.web_bridge import execute_request, normalize_path, view_key


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
