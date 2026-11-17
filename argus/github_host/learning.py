"""Retain native answer reflection as separately dispatched, serial work."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .protocol import Project, Task, digest


def deferred_thread() -> threading.Thread:
    # Preserve the native return contract without starting a model in a web
    # projection, or losing a daemon thread at the end of an Actions job.
    thread = threading.Thread(target=lambda: None, name='argus-learning-deferred')
    thread.start()
    return thread


def queue_learning(store: Any, project: Project) -> None:
    if project.status == 'cancelled' or any(row['role'] == 'learning' and row['state'] in {'pending', 'dispatched', 'running'} for row in project.tasks.values()):
        return
    path = store.root / 'runtime/answer-learning.sqlite3'
    if not path.is_file():
        return
    record = json.loads(path.read_bytes())
    jobs = [entry['values'] for entry in record['tables']['jobs']['rows'].values()
            if entry['values']['status'] == 'queued' and not entry['values']['id'].startswith('mission-')]
    for job in sorted(jobs, key=lambda row: row['created']):
        task_id = 'learn-' + digest({'id': job['id'], 'attempts': job['attempts']})[:24]
        if task_id in project.tasks:
            continue
        snapshot = store.commit_local('Checkpoint pending answer learning')
        project.queue(Task(task_id, job['id'], project.goal_revision, project.runtime_sha,
            project.source_sha, snapshot, role='learning', evidence={'learning_job': job['id']}))
        break


def execute_learning(root: Path, task: Task) -> dict[str, Any]:
    from ..life import answer_learning as native
    from ..webapi.manager_state import answer_learning_backend
    root = root.resolve()
    job_id = task.evidence['learning_job']
    with native._database(root) as db:
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('Authorized learning job disappeared')
        job = dict(row)
        if job['status'] == 'done':
            return {'status': 'learning_done', 'success': True, 'item_id': job_id}
        db.execute("UPDATE jobs SET status='running', updated=?, attempts=attempts+1 WHERE id=?", (time.time(), job_id))
    native._BACKEND_FACTORIES[root] = answer_learning_backend
    try:
        native._process(root, job)
    finally:
        native._BACKEND_FACTORIES.pop(root, None)
        native._BACKENDS.pop((root, job['sid']), None)
    with native._database(root) as db:
        result = dict(db.execute('SELECT status,outcome FROM jobs WHERE id=?', (job_id,)).fetchone())
    return {'status': 'learning_' + result['status'], 'success': result['status'] != 'failed',
            'item_id': job_id, 'learning_outcome': json.loads(result['outcome'])}
