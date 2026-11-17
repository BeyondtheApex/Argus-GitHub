"""Recover finished controller artifacts and apply explicit control transitions."""
from __future__ import annotations

from typing import Any

from .protocol import ContractError, Project


def recover_controllers(store: Any, project: Project, api: Any, *, current_run: str) -> Project:
    from .artifacts import download_json
    from .budgets import settle
    from .host import publish_controller, read_project
    for key, reservation in list(project.reservations.items()):
        if not key.startswith('controller-') or reservation['status'] != 'reserved':
            continue
        run_id = key.split('-')[1]
        if run_id == current_run or not run_id.isdigit():
            continue
        run = api.request('GET', f'/actions/runs/{int(run_id)}')
        if run['status'] != 'completed':
            continue
        if (run.get('path', '').split('@')[0] != '.github/workflows/argus-controller.yml'
                or run.get('head_repository', {}).get('id') != project.repository_id):
            raise ContractError('Controller recovery provenance mismatch')
        jobs = api.pages(f'/actions/runs/{int(run_id)}/jobs', 'jobs')
        if any(job['name'] == 'think' and job.get('conclusion') == 'success' for job in jobs):
            try:
                proposal = download_json(api, int(run_id), artifact_name='argus-controller-proposal', filename='controller.json')
                if proposal['id'] != key:
                    raise ContractError('Controller artifact identity mismatch')
                project = publish_controller(store, proposal, settings=store.read('host/settings.json', {}))
                store.publish('Recover completed controller proposal after publisher interruption')
                project = read_project(store)
                continue
            except ContractError as error:
                reservation['recovery_error'] = str(error)
        settle(project, key, {'pricing_status': 'unpriced'})
        project.continuations = {'requested': False, 'reason': 'controller_interrupted', 'run_id': int(run_id)}
    return project


def cancel_obsolete_runs(project: Project, api: Any) -> None:
    for task in project.tasks.values():
        if task['state'] not in {'cancelled', 'superseded'} or task['evidence'].get('cancellation_requested'):
            continue
        if task.get('run_id'):
            run = api.request('GET', f"/actions/runs/{task['run_id']}")
            if run['status'] != 'completed':
                api.request('POST', f"/actions/runs/{task['run_id']}/cancel")
        task['evidence']['cancellation_requested'] = True
