"""Pack-based Git persistence: one fetch/push, immutable snapshots, CAS writes."""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from .protocol import ContractError, canonical, commit_sha
from .snapshots import capture, restore, safe_path


class StateConflict(RuntimeError):
    """Another publisher advanced the branch; replay against its state."""


class GitStore:
    def __init__(self, repository: str, *, token: str = '', branch: str = 'argus-state',
                 remote: str | None = None) -> None:
        if not __import__('re').fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ContractError('Invalid GitHub repository')
        if not __import__('re').fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,150}', branch) or '..' in branch:
            raise ContractError('Invalid state branch')
        self.repository, self.branch = repository, branch
        self._temp = tempfile.TemporaryDirectory(prefix='argus-git-state-')
        self.root = Path(self._temp.name)
        self.env = dict(os.environ)
        self.env.update({'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_NOSYSTEM': '1',
                         'GIT_AUTHOR_NAME': 'argus-host', 'GIT_AUTHOR_EMAIL': 'argus-host@localhost',
                         'GIT_COMMITTER_NAME': 'argus-host', 'GIT_COMMITTER_EMAIL': 'argus-host@localhost'})
        if token:
            encoded = base64.b64encode(('x-access-token:' + token).encode()).decode()
            self.env.update({'GIT_CONFIG_COUNT': '1',
                             'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraheader',
                             'GIT_CONFIG_VALUE_0': 'AUTHORIZATION: basic ' + encoded})
        self.git('init', '-q')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('config', 'core.autocrlf', 'false')
        self.git('remote', 'add', 'origin', remote or f'https://github.com/{repository}.git')
        self.base: str | None = None

    def git(self, *args: str, check: bool = True, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        process = subprocess.run(['git', *args], cwd=self.root, env=self.env,
                                 input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=180, check=False)
        if check and process.returncode:
            # Never include credential-bearing commands or environment in errors.
            raise RuntimeError(f'Git state operation {args[0]} failed: '
                               + process.stderr.decode(errors='replace')[-1000:])
        return process

    def load(self, *, revision: str | None = None) -> str | None:
        ref = commit_sha(revision) if revision else f'refs/heads/{self.branch}'
        found = self.git('ls-remote', 'origin', ref, check=False)
        if not revision and found.returncode:
            raise RuntimeError('Cannot inspect state branch; refusing to assume empty state')
        if not revision and not found.stdout.strip():
            self.git('symbolic-ref', 'HEAD', f'refs/heads/{self.branch}')
            return None
        self.git('fetch', '-q', '--depth=1', 'origin', ref)
        self.git('checkout', '-q', '--detach', 'FETCH_HEAD')
        self.base = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        return self.base

    def read(self, name: str, default: Any = None) -> Any:
        path = self.root / safe_path(name)
        return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else default

    def write(self, name: str, value: Any) -> None:
        path = self.root / safe_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical(value) + b'\n')

    def commit_local(self, message: str) -> str:
        self.git('add', '--all', '--', '.')
        staged = self.git('diff', '--cached', '--quiet', check=False)
        head = self.git('rev-parse', '--verify', 'HEAD', check=False)
        if not staged.returncode and not head.returncode:
            return head.stdout.decode().strip()
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD').stdout.decode().strip()

    def publish(self, message: str) -> str:
        revision = self.commit_local(message)
        if revision == self.base:
            return revision
        result = self.git('push', '--porcelain', 'origin', f'HEAD:refs/heads/{self.branch}', check=False)
        if result.returncode:
            if b'rejected' in result.stdout + result.stderr:
                raise StateConflict('State branch advanced; pending commands must be replayed')
            raise RuntimeError('Unable to publish state; local proposal remains unaccepted')
        self.base = revision
        return revision

    def snapshot(self, prefix: str = 'runtime') -> dict[str, bytes]:
        return capture(self.root / safe_path(prefix))

    def files_at(self, revision: str, prefix: str = 'runtime') -> dict[str, bytes]:
        revision, prefix = commit_sha(revision), safe_path(prefix)
        if self.git('cat-file', '-e', revision, check=False).returncode:
            self.git('fetch', '-q', '--depth=1', 'origin', revision)
        files = {}
        present = self.git('ls-tree', '--name-only', revision, '--', prefix)
        if not present.stdout.strip():
            return files
        archive = self.git('archive', revision, '--', prefix, check=False)
        if archive.returncode:
            raise ContractError('Cannot read the pinned runtime snapshot')
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode='r:') as entries:
            for entry in entries:
                if entry.isdir():
                    continue
                if not entry.isfile():
                    raise ContractError('Unsupported state archive member')
                name = safe_path(entry.name)
                content = entries.extractfile(entry)
                if content is not None:
                    files[name[len(prefix) + 1:]] = content.read()
        return files

    def export(self, destination: Path) -> None:
        restore(destination, self.snapshot())

    def close(self) -> None:
        self.env.pop('GIT_CONFIG_VALUE_0', None)
        self._temp.cleanup()

    def __enter__(self) -> GitStore:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
