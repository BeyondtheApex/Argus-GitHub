"""Immutable runtime promotion, retained checkpoints and explicit rollback."""
from __future__ import annotations

import base64
import json
import time
from typing import Any

from .protocol import ContractError, Project, commit_sha


def revision_document(api: Any, revision: str, path: str) -> dict[str, Any]:
    value = api.request('GET', '/contents/' + path + '?ref=' + commit_sha(revision))
    if value.get('encoding') != 'base64' or value.get('type') != 'file':
        raise ContractError('Candidate is missing its versioned state format or acceptance evidence')
    return json.loads(base64.b64decode(value['content']))


def check_state_format(api: Any, current: str, candidate: str) -> None:
    before = revision_document(api, current, '.argus-host/state-format.json')
    after = revision_document(api, candidate, '.argus-host/state-format.json')
    if not {'host_schema', 'snapshot_formats', 'supervisor_continuation'} <= before.keys() or before != after:
        raise ContractError('Runtime state formats differ; an explicit tested migration is required')


def check_acceptance(project: Project, payload: dict[str, Any], api: Any, revision: str) -> dict[str, Any]:
    """Evidence is separate from its evaluated commit, but pinned and reviewable.

    A maintainer submits the report after running real-model comparisons. CI
    fixtures cannot stand in for accepted quality, token usage or execution time.
    """
    evidence_sha = commit_sha(payload.get('evidence_sha', ''))
    report = revision_document(api, evidence_sha, '.argus-host/acceptance.json')
    from .evaluation import validate_acceptance
    validate_acceptance(report, baseline=project.runtime_sha, candidate=revision)
    return {'evidence_sha': evidence_sha, 'report': report}


def promote(project: Project, payload: dict[str, Any], api: Any, *, state_commit: str) -> None:
    revision = commit_sha(payload['runtime_sha'])
    if project.status != 'paused':
        raise ContractError('Pause and drain the project before promoting a runtime')
    if any(row['state'] in {'dispatched', 'running', 'awaiting_review'} for row in project.tasks.values()):
        raise ContractError('Runtime promotion waits for live tasks and delivery reviews to settle')
    for workflow in ('argus-host-ci.yml', 'tests.yml'):
        runs = api.pages('/actions/workflows/' + workflow + '/runs?head_sha=' + revision, 'workflow_runs')
        if not any(row['conclusion'] == 'success' and row['head_sha'] == revision for row in runs):
            raise ContractError('Candidate has no successful ' + workflow + ' run at this exact commit')
    check_state_format(api, project.runtime_sha, revision)
    acceptance = check_acceptance(project, payload, api, revision)
    api.runtime_tag(revision)
    project.runtime_history.append({'runtime_sha': project.runtime_sha, 'checkpoint': state_commit,
                                    'promoted_at': time.time(), 'goal_revision': project.goal_revision,
                                    'acceptance': acceptance})
    project.runtime_sha = revision


def rollback(project: Project, payload: dict[str, Any], api: Any, *, state_commit: str) -> None:
    revision = commit_sha(payload['runtime_sha'])
    if not any(row['runtime_sha'] == revision for row in project.runtime_history):
        raise ContractError('Rollback target was never a recorded project runtime')
    if project.status != 'paused' or any(row['state'] in {'dispatched', 'running', 'awaiting_review'} for row in project.tasks.values()):
        raise ContractError('Pause and drain the project before rollback')
    check_state_format(api, project.runtime_sha, revision)
    api.runtime_tag(revision)
    project.runtime_history.append({'runtime_sha': project.runtime_sha, 'checkpoint': state_commit,
        'promoted_at': time.time(), 'goal_revision': project.goal_revision, 'operation': 'rollback'})
    project.runtime_sha = revision
