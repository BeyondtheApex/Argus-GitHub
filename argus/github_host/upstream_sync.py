"""Create an isolated upstream merge candidate and explicitly dispatch its CI."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .git_store import GitStore
from .github import GitHub, GitHubError
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
    workflow_authorized = os.environ.get('ARGUS_UPSTREAM_CAN_WRITE_WORKFLOWS') == 'true'
    resume_blocked = False
    if prs:
        if workflow_authorized:
            try:
                api.request('GET', '/contents/.argus-host/upstream-blocked.json?ref=' + prs[0]['head']['sha'])
                resume_blocked = True
            except GitHubError as error:
                if error.status != 404:
                    raise
        if not resume_blocked:
            print('Existing upstream candidate: ' + prs[0]['html_url'])
            return 0
    with GitStore(api.repository, token=api._token, branch=branch) as candidate:
        base = prs[0]['head']['sha'] if resume_blocked else current
        candidate.load(revision=base)
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
            workflow_changes = candidate.git('diff', '--name-only', current, 'HEAD', '--', '.github/workflows/').stdout.decode().splitlines()
            if workflow_changes and not workflow_authorized:
                patch = candidate.git('diff', '--binary', current, 'HEAD').stdout
                candidate.git('reset', '--hard', current)
                candidate.write('.argus-host/upstream-blocked.json', {'upstream_sha': latest, 'source_sha': current,
                    'reason': 'workflow_write_permission_required', 'workflow_paths': workflow_changes})
                (candidate.root / '.argus-host/upstream-candidate.patch').write_bytes(patch)
                candidate.commit_local('Retain complete upstream patch awaiting workflow-write authorization')
                body = ('Upstream candidate is BLOCKED, not applied. The default Actions token cannot update workflow files. '
                    'The complete merge diff is retained in `.argus-host/upstream-candidate.patch`; no upstream changes were silently omitted. '
                    'Configure a repository-scoped `ARGUS_UPSTREAM_TOKEN` with Contents, Pull requests, Actions and Workflows write permissions, '
                    'then rerun Argus upstream candidate to update this same PR. Alternatively apply and inspect the patch from an authorized local checkout. '
                    'The explicit blocked marker prevents compatibility approval and runtime promotion.')
            else:
                for name in ('upstream-blocked.json', 'upstream-candidate.patch'):
                    (candidate.root / '.argus-host' / name).unlink(missing_ok=True)
                candidate.write('.argus-host/upstream.json', {**config, 'commit': latest})
                candidate.commit_local('Record candidate upstream revision; retain explicit seam gate')
                body = ('Isolated upstream merge candidate. The seam lock is intentionally unchanged: any upstream '
                        'contract changes require inspection and regression evidence before updating it. '
                        'Runtime promotion is a separate checkpointed operation after compatibility and model acceptance.')
        candidate.git('push', 'origin', f'HEAD:refs/heads/{branch}')
    description = body + f'\n\nUpstream: `{latest}`. Previous pin: `{config["commit"]}`.'
    pr = (api.request('PATCH', '/pulls/' + str(prs[0]['number']), {'body': description}) if resume_blocked
          else api.request('POST', '/pulls', {'title': 'Upstream Argus candidate ' + latest[:12],
            'head': branch, 'base': metadata['default_branch'], 'draft': True, 'body': description}))
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
