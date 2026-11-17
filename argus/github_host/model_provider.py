"""Recreate an operator-selected OpenAI-compatible route from Actions secrets.

The provider's generated config is ephemeral. Conversation/session data stays
in the existing private provider home and is checkpointed independently.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .protocol import ContractError


@dataclass(frozen=True)
class OpenAIProvider:
    base_url: str
    model: str
    wire_api: str = 'responses'

    @classmethod
    def read(cls, env: Mapping[str, str], *, require: bool = False) -> OpenAIProvider | None:
        base = env.get('OPENAI_BASE_URL', '').strip()
        model = env.get('OPENAI_MODEL', '').strip()
        wire = env.get('OPENAI_WIRE_API', '').strip() or 'responses'
        if not base and not model and not require:
            return None
        if not base or not model:
            raise ContractError('Configure both OPENAI_BASE_URL and OPENAI_MODEL for the repository provider')
        try:
            url = urlsplit(base)
            valid = bool(url.hostname) and (url.scheme == 'https' or (
                url.scheme == 'http' and url.hostname in {'localhost', '127.0.0.1', '::1'}))
            _ = url.port
        except ValueError:
            valid = False
        if not valid or url.username or url.password or url.query or url.fragment:
            raise ContractError('OPENAI_BASE_URL must be an HTTPS API root without credentials, query or fragment')
        if any(ord(char) < 32 for char in model + base):
            raise ContractError('Provider configuration contains control characters')
        if wire != 'responses':
            raise ContractError('The pinned native Codex backend requires Responses API; chat-only routes need an explicitly selected native backend')
        if require and not env.get('OPENAI_API_KEY', '').strip():
            raise ContractError('Repository Secret OPENAI_API_KEY is missing')
        return cls(base.rstrip('/'), model, wire)

    def write(self, home: Path) -> None:
        home.mkdir(parents=True, exist_ok=True)
        # JSON quoted strings are valid TOML basic strings for these validated
        # values. Never put an actual API key in this file.
        def quote(value: str) -> str:
            return json.dumps(value, ensure_ascii=False)
        content = '\n'.join([
            'model = ' + quote(self.model),
            'model_provider = "argus_repository"',
            '', '[model_providers.argus_repository]',
            'name = "Repository OpenAI-compatible API"',
            'base_url = ' + quote(self.base_url),
            'env_key = "OPENAI_API_KEY"',
            'wire_api = "responses"',
            'requires_openai_auth = false', '',
        ])
        (home / 'config.toml').write_text(content, encoding='utf-8')


def configure(home: Path) -> None:
    provider = OpenAIProvider.read(os.environ)
    if provider is not None:
        provider.write(home)


def apply_role_defaults(options: dict) -> None:
    if options.get('backend', 'codex') != 'codex':
        return
    provider = OpenAIProvider.read(os.environ)
    if provider is not None:
        for role in ('engineer', 'reviewer'):
            options.setdefault(role + '_model', provider.model)
