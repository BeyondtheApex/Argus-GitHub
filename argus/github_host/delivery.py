"""Publish untrusted patches as reviewable Git candidates; never execute them."""
from __future__ import annotations

import base64
import json
import time
from typing import Any

from .git_store import GitStore, StateConflict
from .github import GitHub
from .protocol import ContractError, Project, Task, commit_sha
from .snapshots import safe_path


def publish_candidate(api: GitHub, task: Task, result: dict[str, Any], settings: dict[str, Any],
                      *, workspace_head: str | None = None) -> dict[str, Any] | None:
    encoded = result.get('changes', {}).get('patch_b64', '')
    patch = base64.b64decode(encoded, validate=True)
    if not patch:
        if workspace_head and task.role == 'mission' and result['outcome'].get('success'):
            workspace_id = task.evidence.get('parent_item_id') or task.item_id
            return _delivery_for_revision(api, task, result, commit_sha(workspace_head), workspace_id,
                                          f'argus/work/{workspace_id}')
        return None
    if len(patch) > int(settings.get('max_patch_bytes', 32 * 1024 * 1024)):
        raise ContractError('Candidate exceeds the configured patch size limit')
    workspace_id = task.evidence.get('parent_item_id') or task.item_id
    branch = f'argus/work/{workspace_id}'
    with GitStore(api.repository, token=api._token, branch=branch) as candidate:
        candidate.load(revision=task.source_sha)
        candidate.git('apply', '--cached', '--binary', '--whitespace=nowarn', '-', data=patch)
        names = candidate.git('diff', '--cached', '--name-only', '-z').stdout.decode('utf-8').split('\0')
        for name in filter(None, names):
            safe_path(name)
            parts = name.lower().split('/')
            if any(p in {'.env', 'auth.json', '.argus-skill', '.argus_subagents'} for p in parts):
                raise ContractError('Machine state or credentials cannot be published as project code')
            if name.startswith('.github/workflows/') and not settings.get('allow_workflow_edits', False):
                raise ContractError('Workflow edits need an explicitly configured publishing policy')
            if task.parallel_safe and task.owns_paths and not any(
                name == root.rstrip('/') or name.startswith(root.rstrip('/') + '/') for root in task.owns_paths
            ):
                raise ContractError('Parallel candidate changed paths outside its lease')
        rows = candidate.git('diff', '--cached', '--raw', '-z').stdout.decode('utf-8').split('\0')
        if any(row.startswith(':') and row.split()[1] in {'120000', '160000'} for row in rows):
            raise ContractError('Candidate symlinks/submodules require separate explicit review')
        # Commit only the validated index. No checkout, hooks or repository code.
        candidate.git('-c', 'core.hooksPath=/dev/null', 'commit', '-q', '-m', f'Argus task {task.item_id}')
        revision = candidate.git('rev-parse', 'HEAD').stdout.decode().strip()
        existing = candidate.git('ls-remote', 'origin', 'refs/heads/' + branch).stdout.decode().split()
        if existing:
            previous = commit_sha(existing[0])
            candidate.git('fetch', '-q', '--depth=1', 'origin', previous)
            if previous == task.source_sha:
                candidate.git('push', 'origin', f'HEAD:refs/heads/{branch}')
            else:
                expected_tree = candidate.git('rev-parse', 'HEAD^{tree}').stdout
                actual_tree = candidate.git('rev-parse', previous + '^{tree}').stdout
                if expected_tree != actual_tree:
                    if previous != workspace_head:
                        raise ContractError('Workspace branch changed outside its recorded lineage')
                    candidate.git('reset', '--soft', previous)
                    candidate.git('read-tree', previous)
                    applied = candidate.git('apply', '--cached', '--3way', '--binary', '--whitespace=nowarn', '-', data=patch, check=False)
                    if applied.returncode:
                        raise ContractError('Concurrent workspace changes overlap; retain both proposals for reconciliation')
                    if candidate.git('diff', '--cached', '--quiet', check=False).returncode:
                        candidate.git('-c', 'core.hooksPath=/dev/null', 'commit', '-q', '-m', f'Merge independent task {task.id}')
                    pushed = candidate.git('push', 'origin', f'HEAD:refs/heads/{branch}', check=False)
                    if pushed.returncode:
                        raise StateConflict('Workspace advanced during candidate publication; replay with current lineage')
                    revision = candidate.git('rev-parse', 'HEAD').stdout.decode().strip()
                else:
                    revision = previous
        else:
            candidate.git('push', 'origin', f'HEAD:refs/heads/{branch}')
    return _delivery_for_revision(api, task, result, revision, workspace_id, branch)


def _delivery_for_revision(api: GitHub, task: Task, result: dict[str, Any], revision: str,
                           workspace_id: str, branch: str) -> dict[str, Any]:
    if task.role == 'subagent' or not result['outcome'].get('success'):
        return {'state': 'checkpoint', 'head_sha': revision, 'workspace_id': workspace_id, 'branch': branch}
    owner = api.repository.split('/')[0]
    prs = api.request('GET', f'/pulls?state=all&head={owner}:{branch}')
    if prs:
        pr = prs[0]
        if pr['head']['sha'] != revision:
            raise ContractError('Candidate PR head changed unexpectedly')
    else:
        metadata = api.request('GET', '/')
        pr = api.request('POST', '/pulls', {
            'title': f'Argus: {task.item_id}', 'head': branch, 'base': metadata['default_branch'],
            'body': f'Candidate for task `{task.id}`, attempt {task.attempt}.\n\n'
                    f'Goal revision: {task.goal_revision}. Source: `{task.source_sha}`.\n'
                    f'Native execution evidence: Actions run {result["run_id"]}.\n\n'
                    'Delivery remains pending until the candidate is validated and merged.',
            'draft': not bool(result.get('outcome', {}).get('success'))})
    validation_key = f'{task.id}-{task.attempt}-{revision[:12]}'
    return {'task_id': task.id, 'attempt': task.attempt, 'state': 'awaiting_review',
            'workspace_id': workspace_id,
            'pr': pr['number'], 'url': pr['html_url'], 'head_sha': revision,
            'source_sha': task.source_sha, 'runtime_sha': task.runtime_sha,
            'validation_key': validation_key, 'validation_pending': True,
            'native_success': bool(result['outcome'].get('success')),
            'created_at': time.time()}


def hold_backlog(store: GitStore, project: Project, task: Task, delivery: dict[str, Any]) -> None:
    from ..life.memory import Backlog
    backlog = Backlog(store.root / 'runtime' / 'projects' / project.id / 'backlog.jsonl')
    item = next((row for row in backlog.history() if row.id == task.item_id), None)
    if item is not None:
        delivery['native_status'] = item.status
        # Terminal native receipts are immutable. DeliveryBacklog masks their
        # dependency readiness until the separate GitHub delivery is accepted.


def reconcile_deliveries(store: GitStore, project: Project, api: GitHub, settings: dict[str, Any]) -> int:
    from ..life.memory import Backlog
    changed = 0
    for task_id, delivery in project.deliveries.items():
        if delivery['state'] != 'awaiting_review':
            continue
        pr = api.request('GET', f'/pulls/{int(delivery["pr"])}')
        if pr['head']['sha'] != delivery['head_sha']:
            delivery.update(state='blocked', error='Candidate head changed after validation was requested')
            changed += 1
            continue
        runs = api.pages('/actions/workflows/argus-validate.yml/runs?event=workflow_dispatch&head_sha='
                         + delivery['runtime_sha'] + '&created=>=' + time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(delivery['created_at'] - 60)), 'workflow_runs')
        matches = [row for row in runs if row.get('display_title') == 'Argus validate / ' + delivery['validation_key']]
        validation = max(matches, key=lambda row: row['id']) if matches else None
        if validation and validation['status'] == 'completed':
            delivery['validation'] = {'run_id': validation['id'], 'conclusion': validation['conclusion']}
        validated = delivery.get('validation', {}).get('conclusion') == 'success'
        if (settings.get('auto_merge', False) and validated and delivery['native_success']
                and project.goal_revision == project.tasks[task_id]['goal_revision']
                and project.status == 'active' and not pr['merged'] and pr['state'] == 'open'):
            api.request('PUT', f'/pulls/{pr["number"]}/merge',
                        {'sha': delivery['head_sha'], 'merge_method': 'squash'})
            pr = api.request('GET', f'/pulls/{pr["number"]}')
        if pr['merged'] or pr['state'] == 'closed':
            delivery['state'] = 'merged' if pr['merged'] else 'rejected'
            delivery['merge_sha'] = pr.get('merge_commit_sha')
            if pr['merged']:
                project.workspaces.pop(delivery.get('workspace_id', project.tasks[task_id]['item_id']), None)
            task = Task.read(project.tasks[task_id])
            task.state = 'completed' if pr['merged'] else 'blocked'
            project.tasks[task_id] = task.json()
            backlog = Backlog(store.root / 'runtime' / 'projects' / project.id / 'backlog.jsonl')
            if delivery.get('native_status') == 'done' and not pr['merged']:
                backlog.update(task.item_id, status='failed', last_error='Code candidate was rejected')
            event = {'id': f'delivery-{task_id}-{delivery["state"]}', 'kind': 'merged' if pr['merged'] else 'review',
                     'approved_by': 'github-delivery-reconciler',
                     'payload': {'text': json.dumps({'delivery': delivery['state'], 'task': task_id,
                                                     'pull_request': pr['number']})}}
            project.accepted_requests.append(event)
            project.continuations = {'requested': True, 'reason': 'delivery_changed'}
            changed += 1
    if changed:
        metadata = api.request('GET', '/')
        project.source_sha = commit_sha(api.request('GET', '/git/ref/heads/' + metadata['default_branch'])['object']['sha'])
    return changed
