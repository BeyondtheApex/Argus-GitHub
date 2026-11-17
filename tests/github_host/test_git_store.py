from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from argus.github_host.git_store import GitStore, StateConflict


def test_real_git_cas_rejects_lost_update_and_restores_pinned_snapshot(tmp_path: Path):
    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
    with GitStore('test/repo', remote=str(remote)) as first:
        assert first.load() is None
        first.write('runtime/context.json', {'goal': 'original'})
        original = first.publish('initial state')
    with GitStore('test/repo', remote=str(remote)) as left, GitStore('test/repo', remote=str(remote)) as right:
        assert left.load() == right.load() == original
        left.write('runtime/context.json', {'goal': 'new authoritative goal'})
        advanced = left.publish('advance')
        right.write('runtime/context.json', {'goal': 'stale overwrite'})
        with pytest.raises(StateConflict):
            right.publish('must fail')
        assert left.files_at(original)['context.json'] == b'{"goal":"original"}\n'
    with GitStore('test/repo', remote=str(remote)) as check:
        assert check.load() == advanced
        assert check.read('runtime/context.json')['goal'] == 'new authoritative goal'
