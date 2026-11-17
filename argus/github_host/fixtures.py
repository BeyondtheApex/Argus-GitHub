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
from .protocol import Project
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
                print(json.dumps({'state_branch': branch, 'project_id': state.id,
                    'native_tasks': len(rows), 'separate_worker_runs': [row['run_id'] for row in rows],
                    'goal_unchanged': state.objective == project.objective, 'mode': 'deterministic-native-not-model-evaluation'}))
                return 0
            if any(row['state'] in {'failed', 'blocked'} for row in rows):
                raise RuntimeError('Fixture task failed: ' + json.dumps(rows))
        time.sleep(15)
    raise RuntimeError('Fixture did not finish within its acceptance deadline')


if __name__ == '__main__':
    raise SystemExit(main())
