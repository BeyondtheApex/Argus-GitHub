from __future__ import annotations

import argparse
import json
from pathlib import Path

from argus.github_host.budgets import reserve, settle
from argus.github_host.protocol import Project
from argus.github_host.subagents import transport
from argus.tools.subagent._registry import reconcile_terminal_task


def test_parallel_capacity_is_not_allocated_twice_and_unknown_cost_stays_held():
    project = Project(1, 'owner/repo', 'goal', 'a' * 40, 'b' * 40)
    config = {'global_daily_cap_usd': 12, 'slice_budget_usd': 5}
    assert reserve(project, 'a', config, spent=2)['amount_usd'] == 5
    assert reserve(project, 'b', config, spent=2)['amount_usd'] == 5
    assert reserve(project, 'c', config, spent=2) is None
    assert reserve(project, 'a', config, spent=2)['amount_usd'] == 5
    settle(project, 'a', {'pricing_status': 'unpriced'})
    assert reserve(project, 'c', config, spent=2) is None
    settle(project, 'a', {'pricing_status': 'priced', 'cost_usd': 1})
    assert reserve(project, 'c', config, spent=3)['amount_usd'] == 4


def test_remote_registry_does_not_use_local_process_liveness():
    row = {'task_id': 'remote', 'state': 'running', 'transport': 'github-actions', 'pid': 99999999}
    assert reconcile_terminal_task('remote', row) == row


def test_subagent_submit_and_wait_are_durable_and_nonblocking(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('ARGUS_SKILL_HOME', str(tmp_path / 'state'))
    monkeypatch.setenv('ARGUS_GITHUB_TASK_ID', 'parent')
    args = argparse.Namespace(subcommand='submit', task_id='research-probe', command='python experiment.py',
        description='Check a scientific hypothesis', mode='supervised', cwd=None, run_dir=None,
        accelerator=None, gpu_count=None, gpu_mem_mib=None, expected_duration=None,
        checkpointable=None, intent=None, override_discussion=None)
    assert transport(args) == 0
    request = next((tmp_path / 'state/repository-subagents').glob('*.json'))
    assert json.loads(request.read_text())['args']['mode'] == 'supervised'
    assert transport(argparse.Namespace(subcommand='wait', task_id='research-probe')) == 0
    assert 'never blocks a runner' in capsys.readouterr().out
