"""Prevent known job credentials from entering versioned state or artifacts."""
from __future__ import annotations

from typing import Mapping

from .protocol import ContractError


def secret_values(environment: Mapping[str, str]) -> list[bytes]:
    return [value.encode() for key, value in environment.items()
            if len(value) >= 8 and key.upper().endswith(('_TOKEN', '_API_KEY', '_PASSWORD', '_SECRET'))]


def scan_payload(files: Mapping[str, bytes], environment: Mapping[str, str]) -> None:
    secrets = secret_values(environment)
    for path, data in files.items():
        if any(secret in data for secret in secrets):
            raise ContractError(f'Known credential detected in outgoing payload: {path}')


def redact(message: str, environment: Mapping[str, str]) -> str:
    for secret in secret_values(environment):
        message = message.replace(secret.decode(), '[REDACTED]')
    return message
