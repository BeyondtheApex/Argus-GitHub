"""Trusted repository state transitions; no model or project code executes here."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .git_store import GitStore
from .github import GitHub
from .protocol import ACTIVE, ContractError, Project, Task, digest, identifier, validate_result
from .snapshots import merge_delta, replace_tree

PROJECT_PATH = 'host/project.json'
SETTINGS_PATH = 'host/settings.json'


def read_project(store: GitStore) -> Project:
    data = store.read(PROJECT_PATH)
    if data is None:
        raise ContractError('Initialize this repository before running its agent')
    return Project.read(data)


def accept_command(project: Project, command: dict[str, Any]) -> bool:
    """Caller has verified the actor against GitHub, never against model text."""
    if not command.get('approved_by'):
        raise ContractError('Command has no authenticated maintainer')
    if not project.accept_event(identifier(command['id']), command):
        return False
    kind = command.get('kind')
    payload = command.get('payload', {})
    if not isinstance(payload, dict):
        raise ContractError('Command payload must be an object')
    if kind == 'pause':
        project.status = 'paused'
    elif kind == 'resume':
        project.status = 'active'
        project.continuations = {'requested': True, 'reason': 'operator_resume'}
    elif kind == 'cancel':
        project.status = 'cancelled'
        for task in project.tasks.values():
            if task['state'] in ACTIVE:
                task['state'] = 'cancelled'
    elif kind == 'revise-goal':
        project.revise_goal(str(payload.get('objective', '')),
                            approved_by=command['approved_by'], request_id=command['id'])
    elif kind in {'request', 'answer', 'review', 'merged', 'source_changed'}:
        project.accepted_requests.append(command)
        project.continuations = {'requested': True, 'reason': kind}
    elif kind == 'retry':
        task = Task.read(project.tasks[identifier(payload['task_id'])])
        if task.state not in {'failed', 'blocked'}:
            raise ContractError('Only a failed/blocked task can be explicitly retried')
        if task.goal_revision != project.goal_revision:
            raise ContractError('Replan obsolete-goal work instead of retrying it')
        task.attempt += 1
        task.state, task.run_id, task.result_digest = 'pending', None, None
        task.last_error, task.dispatched_at = '', None
        project.queue(task)
    else:
        raise ContractError('Unknown repository command')
    return True


def ingest_commands(store: GitStore, project: Project) -> list[str]:
    consumed = []
    for path in sorted((store.root / 'inbox').glob('*.json')):
        command = json.loads(path.read_text(encoding='utf-8'))
        if command['id'] in project.processed_events:
            continue
        accept_command(project, command)
        consumed.append(command['id'])
    return consumed


def publish_controller(store: GitStore, proposal: dict[str, Any], *, settings: dict[str, Any]) -> Project:
    project = read_project(store)
    if (proposal.get('project_id') != project.id
            or proposal.get('goal_revision') != project.goal_revision
            or proposal.get('runtime_sha') != project.runtime_sha):
        raise ContractError('Controller proposal has an obsolete identity/goal/runtime')
    event_id = identifier(proposal['id'])
    if event_id in project.processed_events:
        return project
    if project.status != 'active':
        raise ContractError('Project is paused or cancelled; do not commit new model decisions')
    base = store.files_at(proposal['base_commit'])
    current = store.snapshot()
    updated = merge_delta(base, current, proposal['delta'])
    replace_tree(store.root / 'runtime', current, updated)
    # Materialize the exact native state (including newly planned items) before
    # creating task references. Both commits are sent in a single Git push.
    snapshot = store.commit_local('Checkpoint native Argus controller state')
    active = [Task.read(row) for row in project.tasks.values() if row['state'] in ACTIVE]
    width = int(settings.get('mission_width', 2))
    available = max(0, width - len(active))
    candidates = proposal.get('dispatches', [])
    if len(candidates) > available:
        raise ContractError('Controller exceeded the configured concurrent mission width')
    for row in candidates:
        item_id = identifier(row['id'])
        if any(item.item_id == item_id for item in active):
            raise ContractError('The same native item already has an active execution')
        task_id = 't-' + digest({'project': project.id, 'goal': project.goal_revision,
                                 'item': item_id, 'snapshot': snapshot})[:24]
        task = Task(task_id, item_id, project.goal_revision, project.runtime_sha,
                    project.source_sha, snapshot,
                    owns_paths=list(row.get('owns_paths') or []),
                    parallel_safe=bool(row.get('parallel_safe', False)))
        project.queue(task)
    project.accept_event(event_id, {'digest': digest(proposal)})
    stopped = proposal.get('native_outcome', {}).get('stopped_by', '')
    if stopped == 'project_done' and not project.open_ended:
        project.status = 'completed'
    has_work = bool(proposal.get('next_pending'))
    # Native retry/budget/provider decisions remain visible. Transient retries
    # are scheduled; settled waits require an actual event or an explicit due time.
    wait = stopped in {'await_external', 'terminal_idle', 'paused_operator',
                       'operator_decision_required', 'paused_provider_fence',
                       'paused_budget', 'paused_cost', 'auth_failure'}
    project.continuations = {
        'requested': project.status == 'active' and not wait and (has_work or not candidates),
        'reason': stopped, 'not_before': time.time() + (60 if stopped == 'planner_retry' else 0),
    }
    store.write(PROJECT_PATH, project.json())
    return project


def accept_worker(store: GitStore, result: dict[str, Any], *, api: GitHub | None) -> str:
    project = read_project(store)
    task_id = identifier(result['task_id'])
    task = Task.read(project.tasks[task_id])
    disposition = validate_result(task, result, repository_id=project.repository_id)
    if api is not None:
        api.validate_run(task, result['run_id'], repository_id=project.repository_id)
    if disposition != 'accept':
        return disposition
    if task.goal_revision != project.goal_revision:
        return 'stale'
    base = store.files_at(task.snapshot_commit)
    current = store.snapshot()
    # Store result evidence even when native state needs conflict resolution.
    store.write(f'results/{task.id}.{task.attempt}.json', result)
    try:
        updated = merge_delta(base, current, result['delta'])
    except ContractError as error:
        task.state = 'blocked'
        task.last_error = 'Native state merge requires reconciliation: ' + str(error)
        project.tasks[task.id] = task.json()
        project.continuations = {'requested': True, 'reason': 'worker_state_conflict'}
        store.write(PROJECT_PATH, project.json())
        return 'conflict'
    replace_tree(store.root / 'runtime', current, updated)
    outcome = result['outcome']
    task.run_id = result['run_id']
    task.result_digest = digest(result)
    task.finished_at = time.time()
    # A successful Action means an execution report exists. Native outcome and
    # later PR/research acceptance still decide what has actually been achieved.
    task.state = 'completed' if result.get('execution_completed') else 'failed'
    task.evidence = {'native_outcome': outcome, 'run_id': task.run_id,
                     'source_sha': task.source_sha, 'changes': result.get('changes', {})}
    project.tasks[task.id] = task.json()
    project.continuations = {'requested': project.status == 'active', 'reason': 'worker_result'}
    store.write(PROJECT_PATH, project.json())
    return 'accepted'


def flush_dispatches(store: GitStore, api: GitHub, *, lease_seconds: int = 21600) -> int:
    """A durable outbox survives process exit between commit and dispatch."""
    project = read_project(store)
    if project.status != 'active':
        return 0
    count = 0
    for key, operation in project.outbox.items():
        if operation.get('kind') != 'dispatch' or operation['status'] != 'pending':
            continue
        task = Task.read(project.tasks[operation['task_id']])
        if task.goal_revision != project.goal_revision or task.state in {'cancelled', 'superseded'}:
            operation['status'] = 'cancelled'
            continue
        run_id = api.dispatch(task)
        task.run_id = run_id
        task.state = 'dispatched'
        task.dispatched_at = time.time()
        task.lease_until = time.time() + lease_seconds
        operation['status'] = 'submitted'
        project.tasks[task.id] = task.json()
        count += 1
    store.write(PROJECT_PATH, project.json())
    return count


def reconcile_runs(project: Project, api: GitHub, *, max_attempts: int = 3) -> list[int]:
    completed = []
    for key, raw in list(project.tasks.items()):
        task = Task.read(raw)
        if task.state not in {'dispatched', 'running'}:
            continue
        run = api.request('GET', f'/actions/runs/{task.run_id}') if task.run_id else api.find_task_run(task)
        if run is None:
            # Allow time for the dispatch/list visibility gap. Never create a
            # new attempt simply because a freshly submitted run is not visible.
            if task.dispatched_at and time.time() - task.dispatched_at > 600:
                task.state = 'blocked'
                task.last_error = 'Dispatch acknowledgement missing; explicit reconciliation required'
        elif run['status'] == 'completed':
            task.run_id = run['id']
            if run['conclusion'] == 'success':
                completed.append(run['id'])
            else:
                task.last_error = f"GitHub execution ended: {run['conclusion']}"
                task.state = 'failed' if task.attempt >= max_attempts else 'blocked'
                project.continuations = {'requested': True, 'reason': 'worker_failed'}
        else:
            task.run_id, task.state = run['id'], 'running'
            if task.lease_until and time.time() > task.lease_until:
                api.request('POST', f"/actions/runs/{run['id']}/cancel")
                task.state, task.last_error = 'blocked', 'Worker exceeded its authorized lease'
        project.tasks[key] = task.json()
    return completed


def should_think(project: Project, *, new_events: bool, now: float | None = None) -> bool:
    if project.status != 'active':
        return False
    if new_events:
        return True
    continuation = project.continuations
    return bool(continuation.get('requested')
                and float(continuation.get('not_before', 0)) <= (now or time.time()))


def restore_failed_item(runtime_root: Path, project: Project, task_id: str) -> None:
    """Requeue only a confirmed failed distributed execution, not all running work."""
    from ..life.memory import Backlog

    task = Task.read(project.tasks[task_id])
    if task.state not in {'failed', 'blocked'}:
        raise ContractError('Live work is not an orphan')
    backlog = Backlog(runtime_root / 'projects' / project.id / 'backlog.jsonl')
    item = next((row for row in backlog.active() if row.id == task.item_id), None)
    if item is not None and item.status == 'running':
        backlog.update(item.id, status='pending', started_ts=None, running_owner='',
                       last_error=task.last_error, orphan_retries=item.orphan_retries + 1)
