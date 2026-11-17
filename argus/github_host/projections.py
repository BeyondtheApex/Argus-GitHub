"""Rebuild Pages read models without upgrading or invoking the agent runtime.

Rendering and publication run in different jobs. A stale render is discarded;
this command never writes native memory, tasks, objectives or provider state.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from .cli import read_json, write_json
from .git_store import GitStore, StateConflict
from .host import read_project
from .portability import configure_provider_state, rebase
from .protocol import Project
from .snapshots import capture, restore
from .sqlite_state import materialize
from .web_bridge import collect_views, persist_views


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'render', 'publish'])
    parser.add_argument('--bundle', type=Path, default=Path('projection-input'))
    parser.add_argument('--result', type=Path, default=Path('projections.json'))
    parser.add_argument('--runtime', type=Path, default=Path('projection-runtime'))
    parser.add_argument('--source', type=Path, default=Path('project'))
    args = parser.parse_args()
    if args.mode == 'render':
        for key in ('GITHUB_TOKEN', 'GH_TOKEN', 'OPENAI_API_KEY', 'CODEX_API_KEY', 'ANTHROPIC_API_KEY'):
            os.environ.pop(key, None)
        manifest = read_json(args.bundle / 'manifest.json')
        project = Project.read(manifest['project'])
        root, source = args.runtime.resolve(), args.source.resolve()
        restore(root, rebase(capture(args.bundle / 'runtime'), root, source, portable=False))
        materialize(root)
        os.environ.update(ARGUS_GITHUB_HOST='1', ARGUS_SKILL_HOME=str(root), ARGUS_GITHUB_WORKTREE=str(source))
        configure_provider_state(root, owner='projection')
        views = collect_views(root, project)
        write_json(args.result, {'base_commit': manifest['base_commit'], 'views': views})
        return 0
    with GitStore(os.environ['GITHUB_REPOSITORY'], token=os.environ['GITHUB_TOKEN']) as store:
        store.load()
        if args.mode == 'prepare':
            if store.read('host/project.json') is None:
                from .cli import output
                output('ready', 'false')
                return 0
            project = read_project(store)
            restore(args.bundle / 'runtime', store.snapshot())
            write_json(args.bundle / 'manifest.json', {'project': project.json(), 'base_commit': store.base})
            from .cli import output
            output('ready', 'true')
            output('source_sha', project.source_sha)
        else:
            result = read_json(args.result)
            if result['base_commit'] != store.base:
                print('State advanced during render; retained the newer projections. Re-run if still needed.')
                return 0
            persist_views(store, result['views'])
            if store.read('unapproved/index.json') is None:
                store.write('unapproved/index.json', {})
            try:
                store.publish('Refresh Pages read models without running or upgrading the agent')
            except StateConflict:
                print('State advanced before projection publication; retained newer state.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
