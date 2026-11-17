"""Strict, provider-independent repository task and event contracts."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA = 1
TERMINAL = frozenset({'completed', 'failed', 'cancelled', 'superseded'})
ACTIVE = frozenset({'pending', 'dispatched', 'running', 'awaiting_review'})
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
_SHA = re.compile(r'[a-f0-9]{40}\Z')


class ContractError(ValueError):
    """Untrusted or incompatible input must not advance authoritative state."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ContractError('Invalid identifier')
    return value


def commit_sha(value: Any) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ContractError('Expected an immutable Git commit SHA')
    return value


@dataclass
class Task:
    id: str
    item_id: str
    goal_revision: int
    runtime_sha: str
    source_sha: str
    snapshot_commit: str
    role: str = 'mission'
    profile: str = 'ubuntu'
    attempt: int = 1
    state: str = 'pending'
    run_id: int | None = None
    created_at: float = field(default_factory=time.time)
    dispatched_at: float | None = None
    finished_at: float | None = None
    lease_until: float | None = None
    owns_paths: list[str] = field(default_factory=list)
    parallel_safe: bool = False
    last_error: str = ''
    result_digest: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        identifier(self.id)
        identifier(self.item_id)
        for value in (self.runtime_sha, self.source_sha, self.snapshot_commit):
            commit_sha(value)
        if type(self.attempt) is not int or self.attempt < 1:
            raise ContractError('Invalid task attempt')
        if type(self.goal_revision) is not int or self.goal_revision < 1:
            raise ContractError('Invalid goal revision')
        if self.role not in {'mission', 'review', 'research', 'upgrade'}:
            raise ContractError('Unknown task role')
        identifier(self.profile)
        if self.state not in ACTIVE | TERMINAL | {'blocked'}:
            raise ContractError('Unknown task state')

    @classmethod
    def read(cls, value: dict[str, Any]) -> Task:
        try:
            task = cls(**value)
        except TypeError as exc:
            raise ContractError('Task schema mismatch') from exc
        task.validate()
        return task

    def json(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass
class Project:
    repository_id: int
    repository: str
    objective: str
    runtime_sha: str
    source_sha: str
    schema: int = SCHEMA
    id: str = field(default_factory=lambda: 'p-' + uuid.uuid4().hex)
    goal_revision: int = 1
    revision: int = 0
    status: str = 'active'
    open_ended: bool = True
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    processed_events: dict[str, str] = field(default_factory=dict)
    outbox: dict[str, dict[str, Any]] = field(default_factory=dict)
    accepted_requests: list[dict[str, Any]] = field(default_factory=list)
    continuations: dict[str, Any] = field(default_factory=dict)
    goal_history: list[dict[str, Any]] = field(default_factory=list)
    feedback: list[dict[str, Any]] = field(default_factory=list)
    deliveries: dict[str, dict[str, Any]] = field(default_factory=dict)
    runtime_history: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def read(cls, value: dict[str, Any]) -> Project:
        if value.get('schema') != SCHEMA:
            raise ContractError('Unsupported state schema; explicit migration required')
        try:
            state = cls(**value)
        except TypeError as exc:
            raise ContractError('Project schema mismatch') from exc
        identifier(state.id)
        commit_sha(state.runtime_sha)
        commit_sha(state.source_sha)
        if not state.objective.strip() or type(state.repository_id) is not int:
            raise ContractError('Project identity and objective are required')
        for task in state.tasks.values():
            Task.read(task)
        return state

    def json(self) -> dict[str, Any]:
        return asdict(self)

    def accept_event(self, event_id: str, payload: dict[str, Any]) -> bool:
        identifier(event_id)
        fingerprint = digest(payload)
        prior = self.processed_events.get(event_id)
        if prior is not None:
            if prior != fingerprint:
                raise ContractError('Conflicting delivery under the same event id')
            return False
        self.processed_events[event_id] = fingerprint
        self.revision += 1
        return True

    def queue(self, task: Task) -> None:
        task.validate()
        if self.status != 'active' or task.goal_revision != self.goal_revision:
            raise ContractError('Task cannot be dispatched under the current goal/status')
        existing = self.tasks.get(task.id)
        retry = existing and existing['state'] in {'failed', 'blocked'} and task.attempt == existing['attempt'] + 1
        if existing and existing != task.json() and not retry:
            raise ContractError('Task id reused for different work')
        self.tasks[task.id] = task.json()
        key = f'{task.id}.{task.attempt}'
        self.outbox.setdefault(key, {'kind': 'dispatch', 'task_id': task.id,
                                    'attempt': task.attempt, 'status': 'pending'})

    def revise_goal(self, objective: str, *, approved_by: str, request_id: str) -> None:
        if not objective.strip() or not approved_by:
            raise ContractError('Goal revision requires an explicit approved objective')
        self.goal_history.append({'revision': self.goal_revision, 'objective': self.objective,
                                  'approved_by': approved_by, 'request_id': request_id})
        self.goal_revision += 1
        self.objective = objective
        self.status = 'active'
        for task in self.tasks.values():
            if task['state'] in ACTIVE:
                task['state'] = 'superseded'
        for dispatch in self.outbox.values():
            if dispatch.get('kind') == 'dispatch' and dispatch['status'] == 'pending':
                dispatch['status'] = 'cancelled'


def validate_result(task: Task, result: dict[str, Any], *, repository_id: int) -> str:
    """Return disposition; execution provenance is independently checked by the host."""
    expected = {'schema': SCHEMA, 'task_id': task.id, 'attempt': task.attempt,
                'goal_revision': task.goal_revision, 'runtime_sha': task.runtime_sha,
                'source_sha': task.source_sha, 'repository_id': repository_id}
    for key, value in expected.items():
        if type(result.get(key)) is not type(value) or result[key] != value:
            raise ContractError(f'Result does not match authorized {key}')
    if type(result.get('run_id')) is not int or result['run_id'] < 1:
        raise ContractError('Result lacks a workflow run id')
    if task.run_id is not None and result['run_id'] != task.run_id:
        raise ContractError('Result belongs to a different workflow run')
    if not isinstance(result.get('outcome'), dict) or not isinstance(result.get('delta'), dict):
        raise ContractError('Result must contain native outcome and snapshot delta')
    if task.state in {'superseded', 'cancelled'}:
        return 'stale'
    if task.result_digest:
        if task.result_digest != digest(result):
            raise ContractError('Conflicting settled result')
        return 'duplicate'
    return 'accept'
