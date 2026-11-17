from __future__ import annotations

from pathlib import Path

import pytest

from argus.github_host.host import accept_command, should_think
from argus.github_host.native import AuthorizedBacklog, DeliveryBacklog, build_runtime
from argus.github_host.portability import rebase
from argus.github_host.protocol import ContractError, Project, Task
from argus.life.memory import BacklogItem


def project():
    return Project(1, 'owner/repo', 'Keep improving the project', 'a' * 40, 'b' * 40)


def test_rejected_command_does_not_consume_id_or_change_state():
    state = project()
    before = state.json()
    with pytest.raises(ContractError):
        accept_command(state, {'id': 'bad', 'approved_by': 'owner', 'kind': 'unknown'})
    assert state.json() == before


def test_unchanged_wait_makes_no_model_decision():
    state = project()
    assert not should_think(state, new_events=False)
    state.continuations = {'requested': True, 'not_before': 100}
    assert not should_think(state, new_events=False, now=99)
    assert should_think(state, new_events=False, now=100)
    state.status = 'paused'
    assert not should_think(state, new_events=True)


def test_explicit_retry_keeps_identity_and_increments_attempt():
    state = project()
    task = Task('t-1', 'i-1', 1, 'a' * 40, 'b' * 40, 'c' * 40, state='failed')
    state.tasks[task.id] = task.json()
    accept_command(state, {'id': 'retry-1', 'approved_by': 'owner', 'kind': 'retry', 'payload': {'task_id': task.id}})
    assert state.tasks[task.id]['attempt'] == 2
    assert state.outbox['t-1.2']['status'] == 'pending'


def test_delivery_holds_dependencies_and_cannot_auto_resume(tmp_path: Path):
    item = BacklogItem.new(title='Implement integration', objective='Implement repository integration')
    dependent = BacklogItem.new(title='Build on integration', objective='Use repository integration', deps=[item.id])
    backlog = DeliveryBacklog(tmp_path / 'backlog.jsonl', {item.id})
    backlog.add(item)
    backlog.add(dependent)
    backlog.update(item.id, status='done')
    assert backlog.next_pending() is None
    assert backlog.resume_paused(item.id) is None
    backlog.held.clear()
    backlog.update(item.id, status='done')
    assert backlog.next_pending().id == dependent.id


def test_worker_cannot_claim_another_higher_priority_item(tmp_path: Path):
    a = BacklogItem.new(title='Authorized calculation', objective='Calculate the authorized result', priority=100)
    b = BacklogItem.new(title='Other urgent work', objective='Handle unrelated work', priority=1)
    backlog = AuthorizedBacklog(tmp_path / 'backlog.jsonl', a.id, set())
    backlog.add(a)
    backlog.add(b)
    assert backlog.claim_next(expected_id=a.id).id == a.id
    assert backlog.next_pending() is None


def test_paths_roundtrip_without_summarizing_memory(tmp_path: Path):
    import json
    root, code = tmp_path / 'state', tmp_path / 'source'
    value = {'cwd': str(code), 'memory': str(root / 'notes.md'), 'goal': 'original exact goal'}
    files = {'session.json': json.dumps(value).encode()}
    portable = rebase(files, root, code, portable=True)
    moved = rebase(portable, tmp_path / 'new-state', tmp_path / 'new-source', portable=False)
    result = json.loads(moved['session.json'])
    assert result['cwd'] == str(tmp_path / 'new-source')
    assert result['goal'] == value['goal']


def test_remote_outcome_reenters_native_controller_policy(tmp_path: Path, monkeypatch):
    state = project()
    state.feedback = [{'id': 'result-1', 'consumed': False, 'outcome': {
        'status': 'done', 'success': True, 'item_id': 'work-1',
        'planner_report': {'forward_progress': False}}}]
    code = tmp_path / 'code'
    code.mkdir()
    supervisor, memory, _ = build_runtime(state, tmp_path / 'runtime', code, {'backend': 'memory', 'mission_width': 1})
    try:
        supervisor._consecutive_no_progress_missions = 1
        observed = []
        monkeypatch.setattr(supervisor, '_maybe_second_reading', lambda result: observed.append(result) or None)
        supervisor.config.post_mission_hook = lambda result: 'test_after_policy'
        outcome = supervisor.run()
        assert observed[0]['item_id'] == 'work-1'
        assert supervisor._host_consumed_feedback == ['result-1']
        assert supervisor._consecutive_no_progress_missions == 2
        assert outcome['stopped_by'] == 'test_after_policy'
    finally:
        supervisor._host_timer.cancel()
        supervisor.sink.close()
