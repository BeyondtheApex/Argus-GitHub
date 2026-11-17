"""Turn native background submissions into durable remote tasks.

The receiving runner invokes the original submit/preflight/resource/monitor
implementation. Only idle discussion parking is replaced by a saved turn.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .protocol import ContractError, Project, Task, canonical, digest, identifier
from .snapshots import capture, restore


def root() -> Path:
    return Path(os.environ['ARGUS_SKILL_HOME']) / 'repository-subagents'


def transport(args: argparse.Namespace) -> int:
    from ..tools.subagent._registry import _read_task, _write_task
    directory = root()
    directory.mkdir(parents=True, exist_ok=True)
    task_id = identifier(getattr(args, 'task_id', 'all'))
    if args.subcommand == 'submit':
        existing = _read_task(task_id)
        if existing and existing.get('state') in {'starting', 'running', 'discussing', 'waiting_resource'}:
            print(json.dumps({'error': 'Remote task still owns this identity', 'task_id': task_id}))
            return 1
        from ..tools.subagent._cli import _declared_resource_demand
        _declared_resource_demand(args)  # native demand validation before dispatch
        for file in Path('.argus_subagents').glob('*.json'):
            row = json.loads(file.read_text(encoding='utf-8'))
            if row.get('state') == 'discussing' and not args.override_discussion:
                print(json.dumps({'error': 'Supervisor discussion must be resolved first', 'blocking_task': row['task_id']}))
                return 1
        request = {'args': vars(args), 'parent_task': os.environ.get('ARGUS_GITHUB_TASK_ID'),
                   'created_at': time.time(), 'operation': 'submit'}
        request_id = 'sub-' + digest(request)[:24]
        request['id'] = request_id
        (directory / (request_id + '.json')).write_bytes(canonical(request) + b'\n')
        _write_task(task_id, {'task_id': task_id, 'state': 'starting', 'transport': 'github-actions',
            'remote_task': request_id, 'description': args.description, 'command': args.command,
            'mode': args.mode, 'cwd': args.cwd or os.getcwd(), 'run_dir': args.run_dir,
            'submitted_at': time.time(), 'run_id': request_id})
        print(json.dumps({'state': 'submitted', 'task_id': task_id, 'remote_task': request_id,
                          'hint': 'Remote work starts after this slice checkpoints. Yield with external_wait; do not poll.'}))
        return 0
    if args.subcommand in {'status', 'wait', 'list'}:
        rows = [json.loads(path.read_text(encoding='utf-8')) for path in Path('.argus_subagents').glob('*.json')]
        if args.subcommand != 'list':
            rows = [row for row in rows if row.get('task_id') == task_id]
        print(json.dumps({'tasks': rows, 'waiting': any(row['state'] in {'starting', 'running', 'discussing', 'waiting_resource'} for row in rows),
                          'hint': 'A new repository event resumes the parent; this command never blocks a runner.'}))
        return 0 if rows or args.subcommand == 'list' else 1
    if args.subcommand == 'reply':
        from ..tools.subagent._discussion_log import _append_discussion, _mirror_discussion_md
        row = _read_task(task_id)
        if row is None:
            raise ContractError('Unknown remote discussion')
        message = args.message
        if args.message_file:
            message = sys.stdin.read() if args.message_file == '-' else Path(args.message_file).read_text(encoding='utf-8')
        if not message.strip():
            raise ContractError('Discussion reply must not be empty')
        _append_discussion(task_id, 'engineer', message)
        _mirror_discussion_md(task_id, row.get('run_dir'))
        request = {'operation': 'discussion', 'args': {'task_id': task_id}, 'created_at': time.time(),
                   'parent_task': os.environ.get('ARGUS_GITHUB_TASK_ID')}
        request['id'] = 'sub-' + digest(request)[:24]
        (directory / (request['id'] + '.json')).write_bytes(canonical(request) + b'\n')
        print(json.dumps({'state': 'reply_queued', 'task_id': task_id, 'remote_task': request['id']}))
        return 0
    if args.subcommand == 'clean':
        print(json.dumps({'state': 'retained', 'reason': 'Repository records remain available for recovery and audit'}))
        return 0
    raise ContractError('Unsupported remote subagent operation')


def yield_discussion(task_id: str, task_data: dict[str, Any], model: str, cwd: str,
                     run_dir: str | None, thread_id: str | None,
                     usage_totals: tuple[int, int, int, int]) -> None:
    from ..tools.subagent._discussion_log import _append_discussion, _mirror_discussion_md
    from ..tools.subagent._registry import _apply_supervisor_usage_fields, _write_task
    task = dict(task_data)
    task.update(state='discussing', transport='github-actions', supervisor_thread_id=thread_id,
                supervisor_model=model, discussion_resolution='unresolved', worker_pid=None, pid=None)
    _apply_supervisor_usage_fields(task, model=model, totals=usage_totals)
    _append_discussion(task_id, 'supervisor', str(task.get('concern') or task.get('last_supervisor_concern')
        or 'Run stopped. Explain the concrete fix before relaunching.'))
    _mirror_discussion_md(task_id, run_dir)
    _write_task(task_id, task)


def execute_remote(request: dict[str, Any], state_root: Path, worktree: Path, settings: dict[str, Any]) -> dict[str, Any]:
    from ..tools.subagent._discussion_log import _append_discussion, _mirror_discussion_md
    from ..tools.subagent._registry import _read_task, _write_task
    args = request['args']
    task_id = identifier(args['task_id'])
    previous = Path.cwd()
    os.chdir(worktree)
    try:
        if request['operation'] == 'discussion':
            from ..tools.subagent._discuss_run import _supervisor_discuss_with_usage
            row = _read_task(task_id)
            if row is None or row.get('state') != 'discussing':
                raise ContractError('Discussion no longer awaits a turn')
            resolved, message, thread_id, usage = _supervisor_discuss_with_usage(task_id, row,
                row.get('supervisor_model') or 'gpt-5.5', str(worktree), row.get('supervisor_thread_id'))
            _append_discussion(task_id, 'supervisor', message)
            _mirror_discussion_md(task_id, row.get('run_dir'))
            row.update(state='early_stopped' if resolved else 'discussing',
                       discussion_resolution='resolved' if resolved else 'unresolved', supervisor_thread_id=thread_id)
            from ..tools.subagent._registry import _add_usage_totals, _apply_supervisor_usage_fields
            prior = tuple(int(row.get(key) or 0) for key in ('supervisor_input_tokens', 'supervisor_cached_input_tokens',
                                                          'supervisor_output_tokens', 'supervisor_reasoning_output_tokens'))
            _apply_supervisor_usage_fields(row, model=row.get('supervisor_model') or 'gpt-5.5', totals=_add_usage_totals(prior, usage))
            _write_task(task_id, row)
        else:
            command = [sys.executable, '-m', 'argus.tools.subagent', 'submit']
            for key, value in args.items():
                if key == 'subcommand' or value is None or value is False:
                    continue
                option = '--' + key.replace('_', '-')
                command.append(option)
                if value is not True:
                    command.append(str(value))
            environment = dict(os.environ, ARGUS_GITHUB_REMOTE_EXECUTE='1')
            process = subprocess.run(command, cwd=worktree, env=environment, check=False, timeout=120)
            if process.returncode:
                return {'status': 'subagent_preflight_rejected', 'success': False, 'item_id': task_id}
            deadline = time.monotonic() + float(settings.get('slice_seconds', 2400))
            while time.monotonic() < deadline:
                row = _read_task(task_id) or {}
                if row.get('state') not in {'starting', 'preflight', 'waiting_resource', 'running'}:
                    break
                time.sleep(2)
            else:
                # Preserve the native run signals. An arbitrary process cannot
                # be transparently checkpointed; only declared resume commands
                # may continue a computation on a different runner.
                row = _read_task(task_id) or {}
                run_dir = row.get('run_dir')
                if run_dir:
                    stop = Path(run_dir) / 'STOP'
                    stop.parent.mkdir(parents=True, exist_ok=True)
                    stop.write_text('GitHub finite execution deadline: checkpoint and stop.\n', encoding='utf-8')
                row.update(state='timeout', error='Finite runner deadline; checkpoint/resume required')
                _write_task(task_id, row)
        return {'status': 'subagent_' + row.get('state', 'unknown'),
                'success': row.get('state') in {'done', 'early_stopped', 'discussing'},
                'item_id': task_id, 'remote_record': row}
    finally:
        os.chdir(previous)


def import_registry(state_root: Path, worktree: Path) -> None:
    restore(worktree / '.argus_subagents', capture(state_root / 'subagent-registry'))


def export_registry(state_root: Path, worktree: Path) -> None:
    files = capture(worktree / '.argus_subagents')
    for name, data in list(files.items()):
        if name.endswith('.json') and '/' not in name:
            row = json.loads(data)
            if 'task_id' in row:
                row.update(transport='github-actions', worker_pid=None, pid=None, submitter_pid=None)
                files[name] = canonical(row) + b'\n'
    restore(state_root / 'subagent-registry', files)


def queue_remote_requests(store: Any, project: Project, *, parent: Task | None,
                          requests: list[dict[str, Any]]) -> None:
    from .host import SETTINGS_PATH
    if not requests:
        return
    config = store.read(SETTINGS_PATH, {})
    snapshot = store.commit_local('Checkpoint remote subagent requests and source lineage')
    for request in requests:
        task_id = identifier(request['id'])
        if task_id in project.tasks:
            continue
        args = request['args']
        accelerator = args.get('accelerator') or 'none'
        gpu_count = int(args.get('gpu_count') or 0)
        profiles = config.get('profiles', {})
        candidates = [name for name, row in profiles.items()
                      if (accelerator == 'none' or row.get('accelerator') in {accelerator, 'any'})
                      and int(row.get('gpu_count', 0)) >= gpu_count]
        parent_item = parent.evidence.get('parent_item_id', parent.item_id) if parent else ''
        source = project.workspaces.get(parent_item, parent.source_sha if parent else project.source_sha)
        task = Task(task_id, identifier(args['task_id']), project.goal_revision, project.runtime_sha,
                    source, snapshot, role='subagent', profile=candidates[0] if candidates else 'ubuntu',
                    evidence={'subagent_request': request, 'parent_item_id': parent_item,
                              'parent_task_id': parent.id if parent else None})
        if candidates:
            project.queue(task)
        else:
            task.state, task.last_error = 'blocked', 'No configured runner profile satisfies the declared accelerator demand'
            project.tasks[task.id] = task.json()
            project.accepted_requests.append({'id': 'resource-' + task.id, 'approved_by': 'resource-admission',
                'payload': {'text': f'Remote subagent {task.item_id} cannot start: {task.last_error}.'}})
            project.continuations = {'requested': True, 'reason': 'resource_profile_missing'}
