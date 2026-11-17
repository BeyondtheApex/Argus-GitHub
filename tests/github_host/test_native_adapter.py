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
