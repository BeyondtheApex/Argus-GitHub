from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from argus.github_host.configuration import replace_settings
from argus.github_host.git_store import GitStore
from argus.github_host.host import PROJECT_PATH, ingest_commands
from argus.github_host.learning import execute_learning, queue_learning
from argus.github_host.protocol import ContractError, Project, Task
from argus.github_host.snapshots import capture, replace_tree
from argus.life import answer_learning as native
from argus.life.memory import Backlog, BacklogItem


def test_learning_is_serial_durable_and_does_not_repeat_completed_job(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('ARGUS_GITHUB_HOST', '1')
    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
    project = Project(1, 'owner/repo', 'goal', 'a' * 40, 'b' * 40)
    with GitStore(project.repository, remote=str(remote)) as store:
        store.load()
        root = store.root / 'runtime'
        root.mkdir()
        native.enqueue_answer(root=root, sid=project.id, operator_text='question', reply='answer', vertical='software', evidence={}, turn_id='one').join()
        with native._database(root) as db:
            job = dict(db.execute('SELECT * FROM jobs').fetchone())
            assert job['status'] == 'queued' and job['attempts'] == 0
        files = capture(root)
        replace_tree(root, files, files)
        store.write(PROJECT_PATH, project.json())
        store.publish('initial')
        queue_learning(store, project)
        queue_learning(store, project)
        assert len(project.tasks) == 1
        from argus.github_host.sqlite_state import materialize
        materialize(root)
        calls = []
        def process(root, row):
            calls.append(row['id'])
            native._finish(root, row, {}, [])
        monkeypatch.setattr(native, '_process', process)
        task = Task.read(next(iter(project.tasks.values())))
        assert execute_learning(root, task)['status'] == 'learning_unchanged'
        assert execute_learning(root, task)['status'] == 'learning_unchanged'
        assert calls == [job['id']]


def test_explicit_controls_apply_to_native_backlog_and_emit_receipts(tmp_path: Path):
    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
    state = Project(1, 'owner/repo', 'goal', 'a' * 40, 'b' * 40, status='paused')
    with GitStore(state.repository, remote=str(remote)) as store:
        store.load()
        backlog = Backlog(store.root / 'runtime/projects' / state.id / 'backlog.jsonl')
        item = BacklogItem.new(title='Resume controlled work', objective='Recover after operator pause')
        backlog.add(item)
        backlog.update(item.id, status='paused_operator')
        store.write('inbox/dispatch-1.json', {'id': 'dispatch-1', 'kind': 'resume', 'approved_by': 'maintainer'})
        assert ingest_commands(store, state) == ['dispatch-1']
        assert state.status == 'active'
        assert backlog.next_pending().id == item.id
        assert store.read('commands/dispatch-1.json')['status'] == 'applied'
        store.write('inbox/dispatch-2.json', {'id': 'dispatch-2', 'kind': 'cancel', 'approved_by': 'maintainer'})
        ingest_commands(store, state)
        assert state.status == 'cancelled'
        assert not backlog.active()
        assert json.loads((store.root / 'receipts/dispatch-2.json').read_text())['kind'] == 'cancel'


def test_reconfiguration_requires_pause_and_validates_runner_profile():
    state = Project(1, 'owner/repo', 'goal', 'a' * 40, 'b' * 40)
    class Store:
        def write(self, path, value):
            self.saved = (path, value)
    store = Store()
    config = {'backend': 'codex', 'mission_width': 2, 'profiles': {'ubuntu': {'runs_on': ['ubuntu-latest'], 'timeout_minutes': 60}}}
    with pytest.raises(ContractError):
        replace_settings(store, state, config)
    state.status = 'paused'
    replace_settings(store, state, config)
    assert store.saved == ('host/settings.json', config)


def test_native_preferences_cannot_raise_repository_budget():
    from argus.github_host.configuration import effective
    base = {'global_daily_cap_usd': 10, 'global_daily_token_cap': 10000}
    assert effective(base, {'ARGUS_SKILL_GLOBAL_DAILY_CAP_USD': '0', 'ARGUS_SKILL_GLOBAL_DAILY_TOKEN_CAP': '20000'}) == base
    assert effective(base, {'ARGUS_SKILL_GLOBAL_DAILY_CAP_USD': '5'})['global_daily_cap_usd'] == 5
