"""Candidate validation runs configured argv arrays without repository secrets."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from .git_store import GitStore
from .github import GitHub
from .host import SETTINGS_PATH, read_project
from .protocol import ContractError, commit_sha, identifier


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['prepare', 'execute'])
    parser.add_argument('--task-id', default=os.environ.get('TASK_ID', ''))
    parser.add_argument('--candidate-sha', default=os.environ.get('CANDIDATE_SHA', ''))
    parser.add_argument('--validation-key', default=os.environ.get('VALIDATION_KEY', ''))
    parser.add_argument('--state-branch', default=os.environ.get('STATE_BRANCH', 'argus-state'))
    parser.add_argument('--manifest', type=Path, default=Path('validation.json'))
    parser.add_argument('--worktree', type=Path, default=Path('candidate'))
    args = parser.parse_args()
    if args.mode == 'prepare':
        api = GitHub(os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_TOKEN'])
        with GitStore(api.repository, token=api._token, branch=args.state_branch) as store:
            store.load()
            project = read_project(store)
            delivery = project.deliveries[identifier(args.task_id)]
            if (delivery['head_sha'] != commit_sha(args.candidate_sha)
                    or delivery['validation_key'] != identifier(args.validation_key)
                    or delivery['runtime_sha'] != os.environ['GITHUB_SHA']):
                raise ContractError('Validation request does not match the recorded candidate')
            commands = store.read(SETTINGS_PATH, {}).get('validation_commands', [])
            if not commands:
                raise ContractError('Configure validation_commands before accepting code candidates')
            args.manifest.write_text(json.dumps({'sha': delivery['head_sha'], 'commands': commands}), encoding='utf-8')
    else:
        for name in ('GITHUB_TOKEN', 'GH_TOKEN', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY'):
            os.environ.pop(name, None)
        manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
        actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=args.worktree, text=True).strip()
        if actual != commit_sha(manifest['sha']):
            raise ContractError('Validation checkout changed')
        for command in manifest['commands']:
            if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
                raise ContractError('Validation commands must be argv arrays')
            subprocess.run(command, cwd=args.worktree, check=True, timeout=2700)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
