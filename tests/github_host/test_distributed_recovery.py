from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from argus.github_host.compatibility import stable_ast
from argus.github_host.git_store import GitStore
from argus.github_host.host import (
    PROJECT_PATH,
    SETTINGS_PATH,
    accept_worker,
    publish_controller,
    read_project,
)
from argus.github_host.native import build_runtime, run_slice, save_supervisor
from argus.github_host.portability import rebase
from argus.github_host.protocol import Project
from argus.github_host.snapshots import capture, make_delta, replace_tree, restore
from argus.life.memory import BacklogItem


def test_ast_lock_ignores_new_empty_fields_but_preserves_semantics():
    node = ast.parse('def f(): return 1').body[0]
    before = stable_ast(node)
    node._fields = (*node._fields, 'new_optional_field')
    node.new_optional_field = []
    assert stable_ast(node) == before
    node.new_optional_field = ['behavior']
    assert stable_ast(node) != before


def test_two_native_workers_merge_after_real_controller_run(tmp_path: Path):
    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
    source = tmp_path / 'source'
    source.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=source, check=True)
    project = Project(1, 'owner/repo', 'Verify two independent results', 'a' * 40, 'b' * 40, open_ended=False)
    settings = {'backend': 'memory', 'mission_width': 2, 'slice_seconds': 30}
    with GitStore(project.repository, remote=str(remote)) as store:
        store.load()
        root = store.root / 'runtime'
        supervisor, memory, dispatches = build_runtime(project, root, source, settings)
        for name in ('one', 'two'):
            memory.backlog.add(BacklogItem.new(title='Verify result ' + name, objective='Report the result ' + name,
                tags=['scope:bounded'], manager_decision={'routed': True, 'vertical': 'software', 'workflow_mode': 'direct'},
                parallel_safe=True, owns_paths=['fixture/' + name]))
        before = rebase(capture(root), root, source, portable=True)
        replace_tree(root, capture(root), before)
        store.write(PROJECT_PATH, project.json())
        store.write(SETTINGS_PATH, settings)
        initial = store.publish('initial')
        replace_tree(root, before, rebase(before, root, source, portable=False))
        try:
            outcome = supervisor.run()
            save_supervisor(supervisor, memory.project_root / 'repository-supervisor.json')
        finally:
            supervisor._host_timer.cancel()
            supervisor.sink.close()
        after = rebase(capture(root), root, source, portable=True)
        replace_tree(root, capture(root), before)
        proposal = {'id': 'controller-test', 'project_id': project.id, 'goal_revision': 1,
            'runtime_sha': project.runtime_sha, 'base_commit': initial, 'delta': make_delta(before, after),
            'dispatches': dispatches, 'native_outcome': outcome, 'next_pending': False}
        publish_controller(store, proposal, settings=settings)
        store.publish('controller')
        project = read_project(store)
        assert len(project.tasks) == 2
        results = []
        for index, task in enumerate(project.tasks.values()):
            worker_root = tmp_path / f'worker-{index}'
            base = store.files_at(task['snapshot_commit'])
            restore(worker_root, rebase(base, worker_root, source, portable=False))
            native = run_slice(project, worker_root, source, settings, worker_item=task['item_id'])
            assert native['worker_executed'] and native['native_outcome']['success'], native
            assert native['planning_cycles_this_slice'] == 0
            after = rebase(capture(worker_root), worker_root, source, portable=True)
            results.append({'schema': 1, 'repository_id': 1, 'task_id': task['id'], 'attempt': 1,
                'goal_revision': 1, 'runtime_sha': task['runtime_sha'], 'source_sha': task['source_sha'],
                'run_id': index + 1, 'outcome': native['native_outcome'], 'execution_completed': True,
                'delta': make_delta(base, after), 'metrics': {'pricing_status': 'not_billed'}})
        for result in results:
            assert accept_worker(store, result, api=None) == 'accepted', read_project(store).tasks[result['task_id']]['last_error']
            store.publish('worker checkpoint')
        settled = read_project(store)
        assert all(task['state'] == 'completed' for task in settled.tasks.values())
        assert len(settled.feedback) == 2
        assert settled.objective == project.objective
        assert accept_worker(store, results[0], api=None) == 'duplicate'
