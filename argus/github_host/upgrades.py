"""Immutable runtime promotion, retained checkpoints and explicit rollback."""
from __future__ import annotations

import time
from typing import Any

from .protocol import ContractError, Project, commit_sha


def promote(project: Project, payload: dict[str, Any], api: Any, *, state_commit: str) -> None:
    revision = commit_sha(payload['runtime_sha'])
    if project.status != 'paused':
        raise ContractError('Pause and drain the project before promoting a runtime')
    if any(row['state'] in {'pending', 'dispatched', 'running', 'awaiting_review'} for row in project.tasks.values()):
        raise ContractError('Runtime promotion waits for live tasks and delivery reviews to settle')
    runs = api.pages('/actions/workflows/argus-host-ci.yml/runs?head_sha=' + revision, 'workflow_runs')
    if not any(row['conclusion'] == 'success' and row['head_sha'] == revision for row in runs):
        raise ContractError('Candidate has no successful compatibility run at this exact commit')
    api.runtime_tag(revision)
    project.runtime_history.append({'runtime_sha': project.runtime_sha, 'checkpoint': state_commit,
                                    'promoted_at': time.time(), 'goal_revision': project.goal_revision})
    project.runtime_sha = revision


def rollback(project: Project, payload: dict[str, Any], api: Any, *, state_commit: str) -> None:
    revision = commit_sha(payload['runtime_sha'])
    if not any(row['runtime_sha'] == revision for row in project.runtime_history):
        raise ContractError('Rollback target was never a recorded project runtime')
    # Current v1 state has no destructive migrations. State is retained, not
    # rolled back silently along with executable code.
    promote(project, {'runtime_sha': revision}, api, state_commit=state_commit)
