from __future__ import annotations

from pathlib import Path

from argus.github_host.native import build_runtime, restore_supervisor, save_supervisor
from argus.github_host.protocol import Project
from argus.life.memory import BacklogItem


def test_controller_delegates_without_executing_and_preserves_counters(tmp_path: Path, monkeypatch):
    for name in ('ARGUS_SKILL_HOME', 'ARGUS_SKILL_SESSION_ID', 'ARGUS_SKILL_SESSION_ROOT',
                 'ARGUS_SKILL_AGENT_IO_LOG', 'ARGUS_SKILL_RUNNER_BACKEND', 'ARGUS_GITHUB_HOST',
                 'ARGUS_GITHUB_WORKTREE'):
        monkeypatch.delenv(name, raising=False)
    worktree = tmp_path / 'project'
    worktree.mkdir()
    state = Project(1, 'owner/repo', 'Implement the original requirement', 'a' * 40, 'b' * 40)
    supervisor, memory, dispatches = build_runtime(state, tmp_path / 'runtime', worktree,
                                                   {'backend': 'memory', 'mission_width': 2})
    try:
        def forbidden(**kwargs):
            raise AssertionError('Controller must never execute delegated work')
        monkeypatch.setattr(supervisor.runner, 'execute', forbidden)
        item = BacklogItem.new(title='First task', objective='Add a useful feature')
        memory.backlog.add(item)
        outcome = supervisor._run_one(item)
        assert outcome['status'] == 'repository_dispatched'
        assert dispatches[0]['id'] == item.id
        assert memory.backlog.active()[0].status == 'running'
        supervisor._consecutive_no_progress_missions = 2
        path = tmp_path / 'continuation.json'
        save_supervisor(supervisor, path)
        supervisor._consecutive_no_progress_missions = 0
        restore_supervisor(supervisor, path)
        assert supervisor._consecutive_no_progress_missions == 2
        assert supervisor.config.continuous_objective == state.objective
    finally:
        supervisor._host_timer.cancel()
        supervisor.sink.close()


def test_native_mission_survives_separate_controller_and_worker_roots(tmp_path: Path, monkeypatch):
    import subprocess

    from argus.github_host.portability import rebase
    from argus.github_host.snapshots import capture, restore
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'commit.gpgsign')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', 'false')
    source = tmp_path / 'code'
    source.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=source, check=True)
    state = Project(1, 'owner/repo', 'Implement a bounded feature', 'a' * 40, 'b' * 40)
    settings = {'backend': 'memory', 'mission_width': 1, 'slice_seconds': 60}
    controller, memory, dispatches = build_runtime(state, tmp_path / 'controller', source, settings)
    item = BacklogItem.new(title='Implement result reporting', objective='Produce the requested result reporting',
        tags=['scope:bounded'], manager_decision={'routed': True, 'vertical': 'software', 'workflow_mode': 'direct'})
    memory.backlog.add(item)
    try:
        controller._run_one(item)
        assert len(dispatches) == 1
        save_supervisor(controller, memory.project_root / 'repository-supervisor.json')
    finally:
        controller._host_timer.cancel()
        controller.sink.close()
    portable = rebase(capture(tmp_path / 'controller'), tmp_path / 'controller', source, portable=True)
    worker_root = tmp_path / 'worker'
    restore(worker_root, rebase(portable, worker_root, source, portable=False))
    worker, memory, _ = build_runtime(state, worker_root, source, settings, worker_item=item.id)
    try:
        outcome = worker.tick()
        assert worker._host_executed
        assert outcome['success'] and outcome['status'] in {'done', 'success'}, outcome
        assert memory.backlog.history()[-1].status == 'done'
        assert worker._host_plans == 0
    finally:
        worker._host_timer.cancel()
        worker.sink.close()
