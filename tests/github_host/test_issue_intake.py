from __future__ import annotations

import argparse
import json

import pytest

from argus.github_host.cli import intake
from argus.github_host.protocol import ContractError, Project, digest


class Store:
    def __init__(self):
        self.files = {'host/project.json': Project(1, 'owner/repo', 'goal', 'a' * 40, 'b' * 40).json()}

    def read(self, path, default=None):
        return self.files.get(path, default)

    def write(self, path, value):
        self.files[path] = value

    def publish(self, message):
        return 'c' * 40


class API:
    def __init__(self):
        self.issue = {'number': 1, 'title': 'Request', 'body': 'Untrusted text', 'updated_at': '2026-11-17'}
        self.actors = []

    def request(self, method, path):
        assert path == '/issues/1'
        return self.issue

    def require_maintainer(self, actor):
        self.actors.append(actor)


def test_issue_is_only_exposed_after_exact_content_approval(tmp_path, monkeypatch):
    store, api = Store(), API()
    event_path = tmp_path / 'event.json'
    monkeypatch.setenv('GITHUB_EVENT_PATH', str(event_path))
    monkeypatch.setenv('GITHUB_ACTOR', 'maintainer')
    monkeypatch.setenv('GITHUB_TRIGGERING_ACTOR', 'maintainer')
    monkeypatch.setenv('GITHUB_RUN_ID', '123')
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'issues')
    event_path.write_text(json.dumps({'repository': {'id': 1}, 'issue': api.issue}))
    intake(argparse.Namespace(), api, store)
    assert not any(path.startswith('inbox/') for path in store.files)
    fingerprint = store.files['unapproved/index.json']['1']['content_hash']
    assert fingerprint == digest({key: api.issue[key] for key in ('number', 'title', 'body')})
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'workflow_dispatch')
    event_path.write_text(json.dumps({'repository': {'id': 1}, 'inputs': {
        'command': 'approve-issue', 'payload': json.dumps({'number': 1, 'content_hash': fingerprint})}}))
    api.issue['body'] = 'Changed after review'
    with pytest.raises(ContractError, match='changed since review'):
        intake(argparse.Namespace(), api, store)
    assert not any(path.startswith('inbox/') for path in store.files)
    api.issue['body'] = 'Untrusted text'
    intake(argparse.Namespace(), api, store)
    assert store.files['inbox/dispatch-123.json']['snapshot']['body'] == 'Untrusted text'
    assert store.files['unapproved/index.json'] == {}
    assert api.actors == ['maintainer', 'maintainer']
