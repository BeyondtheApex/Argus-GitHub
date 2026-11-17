from __future__ import annotations

import json
from pathlib import Path

import pytest

from argus.github_host.protocol import ContractError, Project, Task, validate_result
from argus.github_host.snapshots import capture, make_delta, merge_delta, restore, safe_path

SHA = 'a' * 40


def project():
    return Project(repository_id=12, repository='example/repo', objective='Preserve the goal',
                   runtime_sha=SHA, source_sha=SHA)


def task():
    return Task('t-1', 'item-1', 1, SHA, SHA, SHA)


def test_event_idempotency_does_not_accept_mutated_payload():
    state = project()
    assert state.accept_event('e-1', {'value': 'first'})
    assert not state.accept_event('e-1', {'value': 'first'})
    with pytest.raises(ContractError):
        state.accept_event('e-1', {'value': 'second'})


def test_goal_revision_seals_old_work_and_cancels_pending_dispatch():
    state = project()
    state.queue(task())
    state.revise_goal('New approved goal', approved_by='maintainer', request_id='r-1')
    assert state.goal_revision == 2
    assert state.tasks['t-1']['state'] == 'superseded'
    assert state.outbox['t-1.1']['status'] == 'cancelled'


def test_result_requires_exact_types_and_all_bound_versions():
    item = task()
    result = {'schema': 1, 'task_id': item.id, 'attempt': 1, 'goal_revision': 1,
              'runtime_sha': SHA, 'source_sha': SHA, 'repository_id': 12,
              'run_id': 99, 'outcome': {}, 'delta': {}}
    assert validate_result(item, result, repository_id=12) == 'accept'
    for key, value in [('attempt', True), ('goal_revision', 2), ('source_sha', 'b' * 40),
                       ('repository_id', 13), ('task_id', 'other')]:
        with pytest.raises(ContractError):
            validate_result(item, dict(result, **{key: value}), repository_id=12)


@pytest.mark.parametrize('path', ['../escape', '/absolute', 'C:/file', 'a\\b', '.git/config', 'a/..'])
def test_snapshot_paths_cannot_escape(path):
    with pytest.raises(ContractError):
        safe_path(path)


def test_snapshot_roundtrip_and_secret_exclusion(tmp_path: Path):
    root = tmp_path / 'input'
    restore(root, {'identity.md': b'my identity', 'projects/p/events.jsonl': b'{"x":1}\n',
                   'auth.json': b'secret'})
    files = capture(root)
    assert 'auth.json' not in files
    restore(tmp_path / 'output', files)
    assert capture(tmp_path / 'output') == files


def test_concurrent_independent_json_fields_merge():
    base = {'state.json': b'{"one":0,"two":0}'}
    live = {'state.json': b'{"one":1,"two":0}'}
    proposed = {'state.json': b'{"one":0,"two":2}'}
    assert json.loads(merge_delta(base, live, make_delta(base, proposed))['state.json']) == {'one': 1, 'two': 2}


def test_concurrent_keyed_backlog_updates_merge_without_resetting_other_task():
    base = {'backlog.jsonl': b'{"id":"a","status":"running"}\n{"id":"b","status":"running"}\n'}
    live = {'backlog.jsonl': base['backlog.jsonl'].replace(b'"a","status":"running"', b'"a","status":"done"')}
    proposed = {'backlog.jsonl': base['backlog.jsonl'].replace(b'"b","status":"running"', b'"b","status":"done"')}
    result = merge_delta(base, live, make_delta(base, proposed))
    assert all(json.loads(line)['status'] == 'done' for line in result['backlog.jsonl'].splitlines())


def test_concurrent_log_appends_keep_both_results():
    base = {'events.jsonl': b'{"event":0}\n'}
    live = {'events.jsonl': base['events.jsonl'] + b'{"event":1}\n'}
    proposed = {'events.jsonl': base['events.jsonl'] + b'{"event":2}\n'}
    merged = merge_delta(base, live, make_delta(base, proposed))
    assert [json.loads(row)['event'] for row in merged['events.jsonl'].splitlines()] == [0, 1, 2]


def test_conflict_and_bad_hash_never_partially_modify_input():
    base = {'a.txt': b'original', 'b.txt': b'same'}
    live = {'a.txt': b'live', 'b.txt': b'same'}
    proposed = {'a.txt': b'worker', 'b.txt': b'changed'}
    delta = make_delta(base, proposed)
    with pytest.raises(ContractError):
        merge_delta(base, live, delta)
    assert live['b.txt'] == b'same'
    delta['a.txt']['base_sha256'] = 'forged'
    with pytest.raises(ContractError):
        merge_delta(base, base, delta)
