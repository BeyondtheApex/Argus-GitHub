"""Resolve repository policy and native operator preferences consistently."""
from __future__ import annotations

from typing import Any

from .protocol import ContractError

_KNOBS = {'ARGUS_SKILL_RUNNER_BACKEND': ('backend', str),
          'ARGUS_SKILL_GLOBAL_DAILY_CAP_USD': ('global_daily_cap_usd', float),
          'ARGUS_SKILL_GLOBAL_DAILY_TOKEN_CAP': ('global_daily_token_cap', int),
          'ARGUS_SKILL_ENGINEER_MODEL': ('engineer_model', str),
          'ARGUS_SKILL_REVIEWER_MODEL': ('reviewer_model', str),
          'ARGUS_SKILL_ENGINEER_REASONING_EFFORT': ('engineer_reasoning_effort', str),
          'ARGUS_SKILL_REVIEWER_REASONING_EFFORT': ('reviewer_reasoning_effort', str)}


def effective(base: dict[str, Any], knobs: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    if knobs.get('ARGUS_SKILL_MODEL'):
        result.update(engineer_model=knobs['ARGUS_SKILL_MODEL'], reviewer_model=knobs['ARGUS_SKILL_MODEL'])
    for key, (target, cast) in _KNOBS.items():
        if knobs.get(key) not in ('', None):
            result[target] = cast(knobs[key])
    # Native preferences can tighten, but never silently relax, the repository
    # owner's distributed budget policy. Only authenticated configure can do so.
    for name in ('global_daily_cap_usd', 'global_daily_token_cap'):
        cap = base.get(name, 0)
        if cap > 0 and (result.get(name, cap) <= 0 or result.get(name, cap) > cap):
            result[name] = cap
    return result


def read_settings(store: Any) -> dict[str, Any]:
    return effective(store.read('host/settings.json', {}), store.read('runtime/config.json', {}))


def slice_limits(config: dict[str, Any], profile: str) -> tuple[dict[str, Any], int]:
    resource = config.get('profiles', {}).get(profile)
    if not resource or not isinstance(resource.get('runs_on'), list):
        raise ContractError('Worker resource profile has not been configured')
    timeout = int(resource.get('timeout_minutes', 60))
    maximum = 7200 if 'self-hosted' in resource['runs_on'] else 360
    if not 5 <= timeout <= maximum:
        raise ContractError(f'Worker timeout must be between 5 and {maximum} minutes for this runner')
    duration = float(resource.get('slice_seconds', config.get('slice_seconds', 2400)))
    duration = min(duration, timeout * 60 - 180)
    if duration <= 0:
        raise ContractError('Execution slice must leave time to checkpoint and upload')
    return {**config, 'slice_seconds': duration}, timeout


def replace_settings(store: Any, project: Any, config: dict[str, Any]) -> None:
    if project.status != 'paused' or any(row['state'] in {'dispatched', 'running', 'awaiting_review'} for row in project.tasks.values()):
        raise ContractError('Pause and drain tasks before replacing repository execution settings')
    if not isinstance(config, dict) or not config.get('backend') or not config.get('profiles'):
        raise ContractError('Settings need an explicit backend and runner profiles')
    if type(config.get('mission_width')) is not int or config['mission_width'] < 1:
        raise ContractError('mission_width must be a positive integer')
    for profile in config['profiles']:
        slice_limits(config, profile)
    commands = config.get('validation_commands', [])
    if not isinstance(commands, list) or not all(isinstance(row, list) and row and all(isinstance(arg, str) for arg in row) for row in commands):
        raise ContractError('Validation commands must be lists of argument strings')
    store.write('host/settings.json', config)
