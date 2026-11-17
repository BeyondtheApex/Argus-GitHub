"""Create an isolated upstream merge candidate and explicitly dispatch its CI."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .git_store import GitStore
from .github import GitHub
from .protocol import commit_sha


def main() -> int:
    config = json.loads(Path('.argus-host/upstream.json').read_text(encoding='utf-8'))
    repository = config['repository']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid upstream repository')
    api = GitHub(os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_TOKEN'])
    metadata = api.request('GET', '/')
    current = commit_sha(api.request('GET', '/git/ref/heads/' + metadata['default_branch'])['object']['sha'])
    upstream_api = GitHub(repository, api._token)
    latest = commit_sha(upstream_api.request('GET', '/git/ref/heads/' + config['branch'])['object']['sha'])
    if latest == config['commit']:
        print('Pinned upstream is current.')
        return 0
    branch = 'argus/upstream-' + latest[:16]
    owner = api.repository.split('/')[0]
    prs = api.request('GET', f'/pulls?state=open&head={owner}:{branch}')
    if prs:
        print('Existing upstream candidate: ' + prs[0]['html_url'])
        return 0
    with GitStore(api.repository, token=api._token, branch=branch) as candidate:
        candidate.load(revision=current)
        candidate.git('fetch', '--unshallow', 'origin', metadata['default_branch'])
        candidate.git('remote', 'add', 'upstream', 'https://github.com/' + repository + '.git')
        candidate.git('fetch', 'upstream', config['branch'])
        merge = candidate.git('-c', 'core.hooksPath=/dev/null', 'merge', '--no-ff', '--no-edit', latest, check=False)
        if merge.returncode:
            conflicts = candidate.git('diff', '--name-only', '--diff-filter=U').stdout.decode()
            candidate.git('merge', '--abort')
            report = {'upstream_sha': latest, 'source_sha': current, 'conflicts': conflicts.splitlines()}
            candidate.write('.argus-host/upstream-conflict.json', report)
            candidate.commit_local('Record upstream merge conflicts for agent reconciliation')
            body = ('Upstream merge requires adapter reconciliation. This branch records the exact conflicting paths; '
                    'it does not contain a successful upstream merge.\n\n' + '\n'.join('- `' + path + '`' for path in report['conflicts']))
        else:
            candidate.write('.argus-host/upstream.json', {**config, 'commit': latest})
            candidate.commit_local('Record candidate upstream revision; retain explicit seam gate')
            body = ('Isolated upstream merge candidate. The seam lock is intentionally unchanged: any upstream '
                    'contract changes require inspection and regression evidence before updating it. '
                    'Runtime promotion is a separate checkpointed operation after compatibility and model acceptance.')
        candidate.git('push', 'origin', f'HEAD:refs/heads/{branch}')
    pr = api.request('POST', '/pulls', {'title': 'Upstream Argus candidate ' + latest[:12],
        'head': branch, 'base': metadata['default_branch'], 'draft': True,
        'body': body + f'\n\nUpstream: `{latest}`. Previous pin: `{config["commit"]}`.'})
    print(pr['html_url'])
    api.request('POST', '/actions/workflows/argus-host-ci.yml/dispatches', {'ref': branch})
    # Agent-facing work is a structured, trusted event. It does not revise the
    # project's objective or make a candidate the currently executing runtime.
    with GitStore(api.repository, token=api._token) as state:
        state.load()
        if state.read('host/project.json'):
            state.write('inbox/upstream-' + latest[:16] + '.json', {
                'id': 'upstream-' + latest[:16], 'kind': 'request', 'approved_by': 'upstream-sync-workflow',
                'payload': {'text': f'Review upstream candidate PR #{pr["number"]} at {latest}. '
                                    'Resolve adapter conflicts, verify compatibility, and record native-versus-host evaluation. '
                                    'Do not promote or merge until configured acceptance gates pass.'}})
            state.publish('Record upstream maintenance task without changing project goal')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
