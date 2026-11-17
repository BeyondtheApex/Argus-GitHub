"""Receive bounded workflow artifacts without executing their contents."""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Any

from .github import GitHub
from .protocol import ContractError

MAX_ARCHIVE = 128 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def download_result(api: GitHub, run_id: int) -> dict[str, Any]:
    return download_json(api, run_id, artifact_name='argus-result', filename='result.json')


def download_json(api: GitHub, run_id: int, *, artifact_name: str, filename: str) -> dict[str, Any]:
    artifacts = api.pages(f'/actions/runs/{run_id}/artifacts', 'artifacts')
    found = [item for item in artifacts if item['name'] == artifact_name and not item['expired']]
    if len(found) != 1 or found[0]['size_in_bytes'] > MAX_ARCHIVE:
        raise ContractError('Expected exactly one bounded worker result artifact')
    url = f'https://api.github.com/repos/{api.repository}/actions/artifacts/{found[0]["id"]}/zip'
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + api._token,
                                                  'User-Agent': 'argus-github-host'})
    try:
        response = urllib.request.build_opener(_NoRedirect).open(request, timeout=45)
    except urllib.error.HTTPError as error:
        if error.code not in (301, 302, 303, 307):
            raise
        destination = urllib.parse.urlsplit(error.headers['Location'])
        if (destination.scheme != 'https' or not destination.hostname
                or not destination.hostname.endswith(('.blob.core.windows.net', '.actions.githubusercontent.com'))):
            raise ContractError('Unexpected workflow artifact download destination') from None
        # Deliberately create a new request without the repository token.
        response = urllib.request.urlopen(error.headers['Location'], timeout=60)
    with response:
        data = response.read(MAX_ARCHIVE + 1)
    if len(data) > MAX_ARCHIVE:
        raise ContractError('Artifact exceeds download limit')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) != 1 or entries[0].filename != filename or entries[0].file_size > MAX_ARCHIVE:
            raise ContractError('Unexpected result archive members')
        return json.loads(archive.read(entries[0]))
