"""Real Actions transport fixture using Argus' explicitly deterministic backend."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .git_store import GitStore
from .github import GitHub
from .host import PROJECT_PATH, SETTINGS_PATH, flush_dispatches, publish_controller, read_project
from .native import build_runtime, save_supervisor
from .portability import rebase
from .protocol import Project, Task
from .snapshots import capture, make_delta, replace_tree


def main() -> int:
    from ..life.memory import BacklogItem
    repo = os.environ['GITHUB_REPOSITORY']
    api = GitHub(repo, os.environ['GITHUB_TOKEN'])
    branch = 'argus-fixture-' + os.environ['GITHUB_RUN_ID']
    sha = os.environ['GITHUB_SHA']
    settings = {'backend': 'memory', 'mission_width': 2, 'slice_seconds': 120,
                'global_daily_cap_usd': 0, 'event_log_verbosity': 'full',
                'profiles': {'ubuntu': {'runs_on': ['ubuntu-latest'], 'timeout_minutes': 10}}}
    with GitStore(repo, token=api._token, branch=branch) as store:
        if store.load() is not None:
            raise RuntimeError('Fixture branch already exists; use a new workflow run')
        metadata = api.request('GET', '/')
        project = Project(metadata['id'], repo, 'Fixture: verify two independent native missions survive runner replacement', sha, sha,
                          open_ended=False)
        source, root = Path.cwd(), store.root / 'runtime'
        supervisor, memory, dispatches = build_runtime(project, root, source, settings)
        for name in ('one', 'two'):
            memory.backlog.add(BacklogItem.new(title='Verify repository checkpoint ' + name,
                objective='Report the deterministic checkpoint result for ' + name,
                tags=['scope:bounded'], manager_decision={'routed': True, 'vertical': 'software', 'workflow_mode': 'direct'},
                parallel_safe=True, owns_paths=['fixture/' + name]))
        files = capture(root)
        portable = rebase(files, root, source, portable=True)
        replace_tree(root, files, portable)
        store.write(PROJECT_PATH, project.json())
        store.write(SETTINGS_PATH, settings)
        initial = store.publish('Initialize isolated deterministic Actions acceptance fixture')
        # The native objects still refer to this root; restore only path values
        # for the finite controller call, then compare portable snapshots.
        replace_tree(root, portable, rebase(portable, root, source, portable=False))
        try:
            outcome = supervisor.run()
            save_supervisor(supervisor, memory.project_root / 'repository-supervisor.json')
        finally:
            supervisor._host_timer.cancel()
            supervisor.sink.close()
        after = rebase(capture(root), root, source, portable=True)
        replace_tree(root, capture(root), portable)
        proposal = {'id': 'fixture-controller', 'project_id': project.id, 'goal_revision': 1,
            'runtime_sha': sha, 'base_commit': initial, 'delta': make_delta(portable, after),
            'dispatches': dispatches, 'native_outcome': outcome, 'next_pending': False}
        publish_controller(store, proposal, settings=settings)
        store.publish('Checkpoint native controller dispatch decisions')
        flush_dispatches(store, api)
        store.publish('Record separate Actions worker dispatches')
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        with GitStore(repo, token=api._token, branch=branch) as store:
            store.load()
            state = read_project(store)
            rows = list(state.tasks.values())
            if rows and all(row['state'] == 'completed' for row in rows):
                report = {'state_branch': branch, 'project_id': state.id,
                    'native_tasks': len(rows), 'separate_worker_runs': [row['run_id'] for row in rows],
                    'goal_unchanged': state.objective == project.objective, 'mode': 'deterministic-native-not-model-evaluation'}
                report.update(nested_delivery_fixture(api, branch, sha))
                print(json.dumps(report))
                return 0
            if any(row['state'] in {'failed', 'blocked'} for row in rows):
                raise RuntimeError('Fixture task failed: ' + json.dumps(rows))
        time.sleep(15)
    raise RuntimeError('Fixture did not finish within its acceptance deadline')


def nested_delivery_fixture(api: GitHub, branch: str, sha: str) -> dict:
    """A child writes code, its parent settles, a different run validates it.

    The PR targets a fresh fixture source branch. Product source and the live
    project's goal/state are never modified by this acceptance test.
    """
    from ..life.memory import Backlog, BacklogItem
    from .delivery import reconcile_deliveries
    from .subagents import queue_remote_requests
    source_branch = 'argus-fixture-source-' + os.environ['GITHUB_RUN_ID']
    api.request('POST', '/git/refs', {'ref': 'refs/heads/' + source_branch, 'sha': sha})
    artifact = 'host-fixtures/' + os.environ['GITHUB_RUN_ID'] + '.txt'
    code = f"from pathlib import Path; p=Path('{artifact}'); p.parent.mkdir(exist_ok=True); p.write_text('independent child evidence', encoding='utf-8')"
    with GitStore(api.repository, token=api._token, branch=branch) as store:
        store.load()
        project = read_project(store)
        config = store.read(SETTINGS_PATH)
        config.update(source_branch=source_branch, auto_merge=True,
            validation_commands=[['python', '-c', f"from pathlib import Path; assert Path('{artifact}').read_text() == 'independent child evidence'"]])
        store.write(SETTINGS_PATH, config)
        backlog = Backlog(store.root / 'runtime' / 'projects' / project.id / 'backlog.jsonl')
        parent = BacklogItem.new(title='Verify child-generated evidence', objective='Settle the independent child result',
            tags=['scope:bounded'], manager_decision={'routed': True, 'vertical': 'software', 'workflow_mode': 'direct'})
        backlog.add(parent)
        snapshot = store.publish('Prepare isolated nested subagent and delivery acceptance')
        request = {'id': 'fixture-child-' + os.environ['GITHUB_RUN_ID'], 'operation': 'submit',
            'created_at': time.time(), 'args': {'task_id': 'acceptance::child', 'command': 'python -c "' + code + '"',
                'description': 'Write a deterministic acceptance artifact', 'mode': 'direct', 'no_preflight': True,
                'timeout': 60, 'cpu_count': 1}}
        owner = Task('fixture-parent', parent.id, 1, sha, sha, snapshot)
        queue_remote_requests(store, project, parent=owner, requests=[request])
        store.write(PROJECT_PATH, project.json())
        store.publish('Queue independent native subagent')
        flush_dispatches(store, api)
        store.publish('Record subagent dispatch receipt')
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        with GitStore(api.repository, token=api._token, branch=branch) as store:
            store.load()
            project = read_project(store)
            child = project.tasks[request['id']]
            if child['state'] == 'completed':
                child_run = child['run_id']
                assert parent.id in project.workspaces, 'Subagent source checkpoint is missing'
                root, source = store.root / 'runtime', Path.cwd()
                before = capture(root)
                replace_tree(root, before, rebase(before, root, source, portable=False))
                supervisor, memory, dispatches = build_runtime(project, root, source, config)
                try:
                    outcome = supervisor.run()
                    save_supervisor(supervisor, memory.project_root / 'repository-supervisor.json')
                finally:
                    supervisor._host_timer.cancel()
                    supervisor.sink.close()
                after = rebase(capture(root), root, source, portable=True)
                replace_tree(root, capture(root), before)
                publish_controller(store, {'id': 'fixture-parent-controller', 'project_id': project.id,
                    'goal_revision': 1, 'runtime_sha': sha, 'base_commit': store.base,
                    'delta': make_delta(before, after), 'dispatches': dispatches, 'native_outcome': outcome,
                    'next_pending': False, 'consumed_feedback': supervisor._host_consumed_feedback}, settings=config)
                store.publish('Checkpoint parent after separate subagent completion')
                flush_dispatches(store, api)
                store.publish('Record resumed parent dispatch')
                break
            if child['state'] in {'failed', 'blocked'}:
                raise RuntimeError('Nested fixture failed: ' + json.dumps(child))
        time.sleep(15)
    else:
        raise RuntimeError('Nested subagent deadline exceeded')
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        with GitStore(api.repository, token=api._token, branch=branch) as store:
            store.load()
            project = read_project(store)
            if project.deliveries:
                reconcile_deliveries(store, project, api, config)
                store.write(PROJECT_PATH, project.json())
                store.publish('Reconcile isolated acceptance PR')
                for delivery in project.deliveries.values():
                    if delivery['state'] == 'merged':
                        pr = api.request('GET', f"/pulls/{delivery['pr']}")
                        assert pr['base']['ref'] == source_branch
                        assert delivery['validation']['conclusion'] == 'success'
                        return {'nested_subagent_run': child_run, 'delivery_pr': delivery['url'],
                            'validation_run': delivery['validation']['run_id'], 'merged_fixture_only': True,
                            'fixture_source_branch': source_branch}
            failed = [task for task in project.tasks.values() if task['state'] in {'failed', 'blocked'}]
            if failed:
                raise RuntimeError('Parent delivery failed: ' + json.dumps(failed))
        time.sleep(15)
    raise RuntimeError('Isolated PR validation or merge deadline exceeded')


if __name__ == '__main__':
    raise SystemExit(main())
