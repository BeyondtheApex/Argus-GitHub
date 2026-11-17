"""Narrow GitHub API client. No model-generated URLs, shell or credentials."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .protocol import ContractError, Task, commit_sha


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(f'GitHub HTTP {status}: {message[:300]}')


class GitHub:
    def __init__(self, repository: str, token: str) -> None:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ContractError('Invalid repository')
        self.repository, self._token = repository, token

    def request(self, method: str, path: str, data: Any = None, *, raw: bool = False) -> Any:
        if not path.startswith('/') or path.startswith('//') or '\\' in path:
            raise ContractError('API path must be repository-relative')
        url = f'https://api.github.com/repos/{self.repository}{"" if path == "/" else path}'
        request = urllib.request.Request(url, method=method,
            data=None if data is None else json.dumps(data, allow_nan=False).encode(),
            headers={'Authorization': f'Bearer {self._token}',
                     'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
                     'Content-Type': 'application/json', 'User-Agent': 'argus-github-host'})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                body = response.read()
                return body if raw else (json.loads(body) if body else None)
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read()).get('message', 'Request failed')
            except (ValueError, AttributeError):
                detail = 'Request failed'
            raise GitHubError(error.code, str(detail)) from None

    def pages(self, path: str, key: str | None = None) -> list[Any]:
        result = []
        for page in range(1, 101):
            response = self.request('GET', path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
            rows = response[key] if key else response
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise ContractError('API pagination limit reached; refine the query')

    def require_maintainer(self, login: str) -> None:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?', login):
            raise ContractError('Invalid actor login')
        value = self.request('GET', '/collaborators/' + urllib.parse.quote(login) + '/permission')
        if value.get('permission') not in {'admin', 'maintain', 'write'}:
            raise ContractError('This operation requires a repository maintainer')

    def runtime_tag(self, revision: str) -> str:
        revision = commit_sha(revision)
        name = 'argus-runtime-' + revision
        try:
            found = self.request('GET', '/git/ref/tags/' + name)
        except GitHubError as error:
            if error.status != 404:
                raise
            self.request('POST', '/git/refs', {'ref': 'refs/tags/' + name, 'sha': revision})
        else:
            if found['object']['sha'] != revision:
                raise ContractError('Runtime tag changed; refusing ambiguous execution')
        return name

    def find_task_run(self, task: Task) -> dict[str, Any] | None:
        title = f'Argus worker / {task.id} / {task.attempt}'
        created = datetime.fromtimestamp(task.created_at - 60, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        query = urllib.parse.urlencode({'event': 'workflow_dispatch', 'head_sha': task.runtime_sha, 'created': '>=' + created})
        rows = self.pages('/actions/workflows/argus-worker.yml/runs?' + query, 'workflow_runs')
        matches = [run for run in rows if run.get('display_title') == title
                   and run.get('head_sha') == task.runtime_sha]
        return min(matches, key=lambda run: run['id']) if matches else None

    def dispatch(self, task: Task, *, state_branch: str = 'argus-state') -> int | None:
        existing = self.find_task_run(task)
        if existing:
            return int(existing['id'])
        tag = self.runtime_tag(task.runtime_sha)
        receipt = self.request('POST', '/actions/workflows/argus-worker.yml/dispatches', {
            'ref': tag, 'return_run_details': True, 'inputs': {'task_id': task.id, 'attempt': str(task.attempt),
                                   'profile': task.profile, 'state_commit': task.snapshot_commit,
                                   'state_branch': state_branch}})
        # An accepted dispatch is not completion and may not be listable yet.
        return int(receipt['workflow_run_id']) if receipt and receipt.get('workflow_run_id') else None

    def validate_run(self, task: Task, run_id: int, *, repository_id: int) -> dict[str, Any]:
        run = self.request('GET', f'/actions/runs/{run_id}')
        expected_title = f'Argus worker / {task.id} / {task.attempt}'
        if (run.get('head_repository', {}).get('id') != repository_id
                or run.get('path', '').split('@')[0] != '.github/workflows/argus-worker.yml'
                or run.get('head_sha') != task.runtime_sha
                or run.get('event') != 'workflow_dispatch'
                or run.get('display_title') != expected_title):
            raise ContractError('Worker provenance does not match its task')
        jobs = self.pages(f'/actions/runs/{run_id}/jobs', 'jobs')
        execution = [job for job in jobs if job['name'] == 'execute']
        if not execution or any(job.get('conclusion') != 'success' for job in execution):
            raise ContractError('Worker execution has no successful result-producing job')
        return run

    def wake_controller(self, runtime_sha: str, *, reason: str = 'continuation') -> None:
        self.request('POST', '/actions/workflows/argus-controller.yml/dispatches', {
            'ref': self.runtime_tag(runtime_sha), 'inputs': {'reason': reason}})


def approved_issue(api: GitHub, event: dict[str, Any], repository_id: int) -> dict[str, Any] | None:
    """Ordinary issue text is never passed to a model before a trusted approval."""
    if event.get('repository', {}).get('id') != repository_id:
        raise ContractError('Event repository mismatch')
    comment = event.get('comment', {})
    if event.get('action') != 'created':
        return None
    command = str(comment.get('body', '')).strip()
    # Require a content hash: editing an issue after approval cannot smuggle a
    # different payload into the delayed controller invocation.
    match = re.fullmatch(r'/argus approve ([a-f0-9]{64})', command)
    if not match:
        return None
    actor = str(comment.get('user', {}).get('login', ''))
    api.require_maintainer(actor)
    issue = api.request('GET', f"/issues/{int(event['issue']['number'])}")
    from .protocol import digest

    snapshot = {'number': issue['number'], 'title': issue['title'], 'body': issue.get('body') or ''}
    if digest(snapshot) != match[1]:
        raise ContractError('Approved issue hash no longer matches; review the current content')
    return {'id': f"issue-{issue['number']}-comment-{comment['id']}", 'kind': 'request',
            'approved_by': actor, 'approved_at': time.time(), 'snapshot': snapshot,
            'content_hash': match[1], 'goal_change': False}
