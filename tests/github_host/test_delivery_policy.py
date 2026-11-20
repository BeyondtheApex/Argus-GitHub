from argus.github_host.delivery import requires_manual_review


def test_self_modification_and_incomplete_file_inventories_cannot_automerge():
    assert requires_manual_review({'changed_files': 2}, [{'filename': 'research/report.md'}])
    assert requires_manual_review({'changed_files': 1}, [{'filename': 'argus/__init__.py'}])
    assert requires_manual_review({'changed_files': 1}, [{'filename': '.github/actions/setup/action.yml'}])
    assert requires_manual_review({'changed_files': 1}, [{'filename': 'docs/old.py', 'previous_filename': 'argus/core/session.py'}])
    assert not requires_manual_review({'changed_files': 1}, [{'filename': 'host-fixtures/example.txt'}])


import pytest

from argus.github_host.delivery import _delivery_for_revision, workspace_marker
from argus.github_host.protocol import ContractError, Task

SHA = 'a' * 40


class _FakePulls:
    """Minimal GitHub double: closed PRs keep the head they were closed with."""

    repository = 'owner/repo'

    def __init__(self, prs):
        self.prs = prs
        self.created = []

    def request(self, method, path, body=None):
        if method == 'GET' and path == '/':
            return {'default_branch': 'main'}
        if method == 'GET' and path.startswith('/pulls?'):
            assert 'state=open' in path
            return [pr for pr in self.prs if pr['state'] == 'open']
        if method == 'POST' and path == '/pulls':
            pr = {'number': 100 + len(self.created), 'html_url': 'https://example/pr', 'state': 'open',
                  'title': body['title'], 'body': body['body'], 'head': {'sha': 'c' * 40}, 'changed_files': 1}
            self.created.append(pr)
            self.prs.append(pr)
            return pr
        raise AssertionError((method, path))

    def pages(self, path, key=None):
        return [{'filename': 'src/lib.rs'}]


def _task(attempt):
    return Task('t-1', 'item-1', 1, SHA, SHA, SHA, attempt=attempt)


def _result():
    return {'outcome': {'success': True}, 'run_id': 7}


def test_retry_after_rejected_pr_opens_a_fresh_marked_pr():
    closed = {'number': 1, 'state': 'closed', 'title': 'Argus: item-1', 'body': '',
              'head': {'sha': 'b' * 40}, 'html_url': 'https://example/1', 'changed_files': 1}
    api = _FakePulls([closed])
    delivery = _delivery_for_revision(api, _task(2), _result(), 'c' * 40, 'item-1', 'argus/work/item-1', {})
    assert delivery['pr'] == 100
    assert api.created[0]['body'].startswith(workspace_marker('item-1'))


def test_open_pr_is_adopted_not_duplicated():
    api = _FakePulls([])
    first = _delivery_for_revision(api, _task(1), _result(), 'c' * 40, 'item-1', 'argus/work/item-1', {})
    again = _delivery_for_revision(api, _task(1), _result(), 'c' * 40, 'item-1', 'argus/work/item-1', {})
    assert first['pr'] == again['pr'] and len(api.created) == 1


def test_foreign_open_pr_on_candidate_branch_fails_closed():
    foreign = {'number': 5, 'state': 'open', 'title': 'Someone else', 'body': 'manual work',
               'head': {'sha': 'c' * 40}, 'html_url': 'https://example/5', 'changed_files': 1}
    with pytest.raises(ContractError):
        _delivery_for_revision(_FakePulls([foreign]), _task(1), _result(), 'c' * 40, 'item-1', 'argus/work/item-1', {})
