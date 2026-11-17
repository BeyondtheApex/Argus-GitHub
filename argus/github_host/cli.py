"""Finite command entry points used by trusted and model-enabled Actions jobs."""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .artifacts import download_result
from .configuration import effective, read_settings, slice_limits
from .git_store import GitStore, StateConflict
from .github import GitHub, approved_issue
from .host import (
    PROJECT_PATH,
    SETTINGS_PATH,
    accept_worker,
    flush_dispatches,
    ingest_commands,
    publish_controller,
    read_project,
    reconcile_runs,
    recover_failed_runs,
    should_think,
)
from .protocol import ContractError, Project, Task, canonical, commit_sha, identifier
from .snapshots import capture, make_delta, restore


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value) + b'\n')


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def output(name: str, value: Any) -> None:
    text = str(value)
    if '\n' in text or '\r' in text:
        raise ContractError('Workflow output must be one line')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
            stream.write(name + '=' + text + '\n')
    print(name + '=' + text)


def settings() -> dict[str, Any]:
    path = Path('.argus-host/config.json')
    return read_json(path) if path.exists() else {}


def bundle(store: GitStore, project: Project, destination: Path,
           *, task: Task | None = None) -> None:
    revision = task.snapshot_commit if task else store.base
    if revision is None:
        raise ContractError('Bundle requires a durable commit')
    files = store.files_at(revision) if task else store.snapshot()
    restore(destination / 'runtime', files)
    if task and task.role == 'api' and task.evidence['request'].get('upload_id'):
        upload = identifier(task.evidence['request']['upload_id'])
        uploaded = store.files_at(store.base, 'uploads/' + upload)
        restore(destination / 'runtime' / 'repository-uploads' / upload, uploaded)
    config = store.read(SETTINGS_PATH, settings())
    if task:
        raw = store.git('show', f'{revision}:{SETTINGS_PATH}', check=False)
        if raw.returncode == 0:
            config = json.loads(raw.stdout)
    config = effective(config, json.loads(files.get('config.json', b'{}')))
    if task:
        config, _ = slice_limits(config, task.profile)
    reservation_id = (f'{task.id}.{task.attempt}' if task else
                      'controller-' + os.environ.get('GITHUB_RUN_ID', 'local') + '-' + os.environ.get('GITHUB_RUN_ATTEMPT', '1'))
    grant = project.reservations.get(reservation_id)
    if grant:
        config = {**config, 'global_daily_cap_usd': grant['native_cap_usd'],
                  'global_daily_token_cap': grant.get('native_token_cap', config.get('global_daily_token_cap', 0))}
    write_json(destination / 'manifest.json', {'schema': 1, 'base_commit': revision,
        'project': project.json(), 'settings': config, 'task': task.json() if task else None})


def prepare(args: argparse.Namespace, api: GitHub, store: GitStore) -> None:
    if store.read(PROJECT_PATH) is None and not args.task_id:
        output('think', 'false')
        output('reason', 'not_initialized')
        return
    project = read_project(store)
    metadata = api.request('GET', '/')
    if metadata['id'] != project.repository_id:
        raise ContractError('Fork identity differs; initialize a new project identity')
    if args.task_id:
        task = Task.read(project.tasks[identifier(args.task_id)])
        if (task.attempt != args.attempt or task.snapshot_commit != args.state_commit
                or task.goal_revision != project.goal_revision or task.state not in {'pending', 'dispatched', 'running'}):
            raise ContractError('Worker dispatch is no longer authorized')
        if project.status != 'active' and not ((task.role == 'api' and project.status in {'paused', 'completed'})
                or (task.role == 'learning' and project.status == 'completed')):
            raise ContractError('The project is paused or cancelled')
        if task.runtime_sha != os.environ.get('GITHUB_SHA', task.runtime_sha):
            raise ContractError('Worker runtime version does not match the task')
        current_run = int(os.environ.get('GITHUB_RUN_ID', '0'))
        if current_run:
            if task.run_id and task.run_id != current_run:
                output('execute', 'false')
                return
            task.run_id = current_run
            task.state = 'running'
            project.tasks[task.id] = task.json()
            store.write(PROJECT_PATH, project.json())
            store.publish('Claim remote task for exactly one workflow run')
        output('execute', 'true')
        bundle(store, project, args.output, task=task)
        output('source_sha', task.source_sha)
        profile = store.read(SETTINGS_PATH, {}).get('profiles', {}).get(task.profile)
        if not profile or not isinstance(profile.get('runs_on'), list):
            raise ContractError('Worker resource profile has not been configured')
        output('runs_on', json.dumps(profile['runs_on']))
        _, timeout = slice_limits(read_settings(store), task.profile)
        output('timeout', timeout)
        return
    from .recovery import cancel_obsolete_runs, recover_controllers
    project = recover_controllers(store, project, api, current_run=os.environ.get('GITHUB_RUN_ID', ''))
    ingest_commands(store, project)
    cancel_obsolete_runs(project, api)
    ready_results = reconcile_runs(project, api)
    recover_failed_runs(store, project)
    store.write(PROJECT_PATH, project.json())
    new_results = 0
    for run_id in ready_results:
        disposition = accept_worker(store, download_result(api, run_id), api=api)
        new_results += disposition in {'accepted', 'conflict'}
    project = read_project(store)
    from .delivery import reconcile_deliveries
    delivery_changes = reconcile_deliveries(store, project, api, store.read(SETTINGS_PATH, {}))
    source_branch = store.read(SETTINGS_PATH, {}).get('source_branch', metadata['default_branch'])
    source = commit_sha(api.request('GET', '/git/ref/heads/' + source_branch)['object']['sha'])
    if source != project.source_sha:
        project.source_sha = source
        project.continuations = {'requested': True, 'reason': 'source_changed'}
    store.write(PROJECT_PATH, project.json())
    store.publish('Reconcile trusted repository events and worker runs')
    flush_dispatches(store, api)
    store.publish('Recover pending dispatch acknowledgements')
    project = read_project(store)
    think = should_think(project, new_events=bool(delivery_changes))
    if think:
        from .budgets import known_spend, known_tokens, reserve
        key = 'controller-' + os.environ.get('GITHUB_RUN_ID', 'local') + '-' + os.environ.get('GITHUB_RUN_ATTEMPT', '1')
        config = read_settings(store)
        grant = reserve(project, key, config, spent=known_spend(store.root / 'runtime'),
                        tokens=known_tokens(store.root / 'runtime') if config.get('global_daily_token_cap') else 0)
        think = grant is not None
        store.write(PROJECT_PATH, project.json())
        store.publish('Reserve distributed controller budget before model execution')
    output('think', 'true' if think else 'false')
    output('source_sha', project.source_sha)
    output('runtime_sha', project.runtime_sha)
    bundle(store, project, args.output)


def execute(args: argparse.Namespace) -> None:
    # This command runs in a job without repository write permissions. Remove
    # accidentally forwarded token variables before constructing provider CLIs.
    for key in ('GITHUB_TOKEN', 'GH_TOKEN', 'GIT_CONFIG_VALUE_0'):
        os.environ.pop(key, None)
    manifest = read_json(args.bundle / 'manifest.json')
    project = Project.read(manifest['project'])
    task = Task.read(manifest['task']) if manifest['task'] else None
    state_root = args.runtime.resolve()
    source = args.worktree.resolve()
    from .portability import configure_provider_state, rebase
    before = capture(args.bundle / 'runtime')
    restore(state_root, rebase(before, state_root, source, portable=False))
    from .sqlite_state import materialize
    materialize(state_root)
    configure_provider_state(state_root, owner=task.item_id if task else 'controller', settings=manifest['settings'])
    os.environ.update(ARGUS_SKILL_HOME=str(state_root), ARGUS_GITHUB_HOST='1',
                      ARGUS_GITHUB_WORKTREE=str(source), ARGUS_GITHUB_TASK_ID=task.id if task else 'controller',
                      ARGUS_SKILL_SESSION_ID=project.id,
                      ARGUS_SKILL_SESSION_ROOT=str(state_root / 'projects' / project.id),
                      ARGUS_SKILL_RUNNER_BACKEND=str(manifest['settings'].get('backend', 'codex')),
                      ARGUS_SKILL_GLOBAL_DAILY_CAP_USD=str(manifest['settings'].get('global_daily_cap_usd', 0)),
                      ARGUS_SKILL_GLOBAL_DAILY_TOKEN_CAP=str(manifest['settings'].get('global_daily_token_cap', 0)))
    from .subagents import execute_remote, export_registry, import_registry
    import_registry(state_root, source)
    actual_source = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=source,
        check=True, capture_output=True, text=True).stdout.strip()
    expected = task.source_sha if task else project.source_sha
    if actual_source != expected:
        raise ContractError('Project checkout differs from the approved immutable source')
    from .native import run_slice

    started = time.monotonic()
    from .budgets import known_spend
    initial_spend = known_spend(state_root)
    api_receipt = None
    try:
        if task and task.role == 'learning':
            from .learning import execute_learning
            outcome = execute_learning(state_root, task)
            native = {'native_outcome': outcome, 'worker_executed': True, 'planning_cycles_this_slice': 0}
        elif task and task.role == 'subagent':
            spec = task.evidence['subagent_request']
            spec = json.loads(rebase({'spec.json': canonical(spec)}, state_root, source, portable=False)['spec.json'])
            outcome = execute_remote(spec, state_root, source, manifest['settings'])
            native = {'native_outcome': outcome, 'worker_executed': True, 'planning_cycles_this_slice': 0}
        elif task and task.role == 'api':
            from .web_bridge import execute_request
            api_receipt = execute_request(state_root, project, task.evidence['request'])
            native = {'native_outcome': {'status': 'api_completed', 'success': api_receipt['status'] < 400},
                      'worker_executed': True, 'planning_cycles_this_slice': 0}
        else:
            native = run_slice(project, state_root, source, manifest['settings'],
                               worker_item=task.item_id if task else None)
    except Exception as error:
        from .security import redact
        failure = {'status': 'host_error', 'stopped_by': 'host_error', 'success': False,
                   'error': redact(type(error).__name__ + ': ' + str(error), os.environ)}
        native = {'native_outcome': failure, 'worker_executed': False,
                  'planning_cycles_this_slice': 0, 'dispatches': [], 'next_pending': False}
        if task and task.role == 'api':
            api_receipt = {'path': task.evidence['request']['path'], 'status': 503,
                'content_type': 'application/json', 'body_b64': base64.b64encode(canonical({'detail': failure['error']})).decode()}
    export_registry(state_root, source)
    subagent_requests = []
    for path in (state_root / 'repository-subagents').glob('*.json'):
        portable = rebase({'request.json': path.read_bytes()}, state_root, source, portable=True)
        subagent_requests.append(json.loads(portable['request.json']))
    from .web_bridge import collect_views
    views = collect_views(state_root, project)
    after = rebase(capture(state_root), state_root, source, portable=True)
    native = json.loads(rebase({'native.json': canonical(native)}, state_root, source, portable=True)['native.json'])
    from .security import scan_payload
    scan_payload(after, os.environ)
    scan_payload({'native.json': canonical(native)}, os.environ)
    scan_payload({key: base64.b64decode(row['body_b64']) for key, row in views.items()}, os.environ)
    delta = make_delta(before, after)
    metrics = {'execution_seconds': time.monotonic() - started,
               'snapshot_bytes': sum(len(data) for data in after.values()),
               'changed_files': len(delta), 'planning_cycles': native['planning_cycles_this_slice']}
    from ..core.cost_control import global_daily_usage_summary
    usage = global_daily_usage_summary(global_root=state_root)
    metrics.update(cost_usd=max(0.0, usage.known_cost_usd - initial_spend), pricing_status=usage.pricing_status)
    if task:
        # Include new non-ignored files in the reviewable candidate patch.
        subprocess.run(['git', 'add', '--intent-to-add', '--all'], cwd=source, check=True)
        patch = subprocess.run(['git', 'diff', '--binary', expected, '--', '.', ':(exclude).argus_subagents'], cwd=source,
                               check=True, capture_output=True).stdout
        scan_payload({'source.patch': patch}, os.environ)
        result = {'schema': 1, 'repository_id': project.repository_id, 'task_id': task.id,
            'attempt': task.attempt, 'goal_revision': task.goal_revision, 'runtime_sha': task.runtime_sha,
            'source_sha': task.source_sha, 'run_id': int(os.environ['GITHUB_RUN_ID']),
            'outcome': native['native_outcome'], 'execution_completed': native['worker_executed'],
            'delta': delta, 'changes': {'patch_b64': base64.b64encode(patch).decode()}, 'metrics': metrics,
            'api_receipt': api_receipt, 'views': views, 'subagent_requests': subagent_requests}
        write_json(args.output / 'result.json', result)
    else:
        write_json(args.output / 'controller.json', {
            'schema': 1, 'id': 'controller-' + os.environ.get('GITHUB_RUN_ID', str(time.time_ns()))
            + '-' + os.environ.get('GITHUB_RUN_ATTEMPT', '1'),
            'project_id': project.id, 'goal_revision': project.goal_revision,
            'runtime_sha': project.runtime_sha, 'base_commit': manifest['base_commit'],
            'delta': delta, **native, 'metrics': metrics, 'views': views, 'subagent_requests': subagent_requests})


def intake(args: argparse.Namespace, api: GitHub, store: GitStore) -> None:
    event = read_json(Path(os.environ['GITHUB_EVENT_PATH']))
    metadata = event.get('repository') or api.request('GET', '/')
    actor = os.environ.get('GITHUB_TRIGGERING_ACTOR') or os.environ['GITHUB_ACTOR']
    event_type = os.environ['GITHUB_EVENT_NAME']
    if event_type == 'issues':
        issue = event['issue']
        from .protocol import digest
        snapshot = {'number': issue['number'], 'title': issue['title'], 'body': issue.get('body') or ''}
        content_hash = digest(snapshot)
        record = {
            'number': issue['number'], 'title': issue['title'], 'body': issue.get('body') or '',
            'updated_at': issue['updated_at'], 'content_hash': content_hash,
            'approval_command': '/argus approve ' + content_hash}
        store.write(f"unapproved/issue-{issue['number']}.json", record)
        index = store.read('unapproved/index.json', {})
        index[str(issue['number'])] = record
        store.write('unapproved/index.json', index)
        summary = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary, 'a', encoding='utf-8') as stream:
                stream.write(f"Issue #{issue['number']} collected without a model call. A maintainer can review the snapshot in Pages or approve this exact version with:\n\n`/argus approve {content_hash}`\n")
        store.publish('Collect issue without exposing its content to a model')
        return
    if event_type == 'issue_comment':
        command = approved_issue(api, event, metadata['id'])
        if command is None:
            return
    else:
        api.require_maintainer(actor)
        inputs = event.get('inputs', {})
        command = {'id': 'dispatch-' + os.environ['GITHUB_RUN_ID'],
                   'kind': inputs.get('command', 'request'), 'approved_by': actor,
                   'payload': json.loads(inputs.get('payload') or '{}')}
        if command['kind'] == 'api':
            from .web_bridge import normalize_path
            command['payload']['path'] = normalize_path(command['payload']['path'])
            identifier(command['payload']['request_id'])
        if command['kind'] == 'approve-issue':
            from .protocol import digest
            payload = command['payload']
            issue = api.request('GET', '/issues/' + str(int(payload['number'])))
            snapshot = {'number': issue['number'], 'title': issue['title'], 'body': issue.get('body') or ''}
            if digest(snapshot) != payload.get('content_hash'):
                raise ContractError('Issue changed since review; reload the snapshot before approving')
            command = {**command, 'kind': 'request', 'snapshot': snapshot,
                       'content_hash': payload['content_hash'], 'payload': {}, 'goal_change': False}
    if command.get('content_hash') and command.get('snapshot'):
        index = store.read('unapproved/index.json', {})
        index.pop(str(command['snapshot']['number']), None)
        store.write('unapproved/index.json', index)
    if command['kind'] == 'initialize':
        if store.read(PROJECT_PATH) is not None:
            raise ContractError('Project already initialized; use an explicit goal revision')
        objective = str(command['payload'].get('objective', '')).strip()
        if not objective:
            raise ContractError('Initialization requires an objective')
        source = api.request('GET', '/git/ref/heads/' + metadata['default_branch'])['object']['sha']
        runtime = commit_sha(os.environ['GITHUB_SHA'])
        project = Project(metadata['id'], api.repository, objective, runtime, source,
                          open_ended=bool(command['payload'].get('open_ended', True)))
        if command['payload'].get('start') is False:
            project.status = 'paused'
        project.continuations = {'requested': True, 'reason': 'initialized'}
        store.write(PROJECT_PATH, project.json())
        store.write(SETTINGS_PATH, settings())
        from ..core.session import SessionMeta, write_session_meta
        from ..life.memory import MemoryBundle
        from .portability import rebase
        from .snapshots import replace_tree
        from .web_bridge import collect_views, persist_views
        state_root = store.root / 'runtime'
        worktree = Path.cwd().resolve()
        MemoryBundle.for_cwd(worktree, global_root=state_root, fingerprint=project.id,
                             label=project.repository).init()
        write_session_meta(state_root, SessionMeta(id=project.id, display_name=project.repository,
            created=time.time(), last_active=time.time(), cwd=str(worktree), workdir=str(worktree),
            objective=project.objective, origin='github'))
        persist_views(store, collect_views(state_root, project))
        files = capture(state_root)
        replace_tree(state_root, files, rebase(files, state_root, worktree, portable=True))
        api.runtime_tag(runtime)
        store.write('commands/' + command['id'] + '.json', {'status': 'applied', 'kind': 'initialize'})
        store.publish('Initialize repository identity and authoritative objective')
    elif command['kind'] == 'configure':
        from .configuration import replace_settings
        project = read_project(store)
        replace_settings(store, project, command['payload'])
        store.write('commands/' + command['id'] + '.json', {'status': 'applied', 'kind': 'configure'})
        store.publish('Apply authenticated execution settings after pause and drain')
    elif command['kind'] in {'promote-runtime', 'rollback-runtime'}:
        from .upgrades import promote, rollback
        project = read_project(store)
        operation = promote if command['kind'] == 'promote-runtime' else rollback
        operation(project, command['payload'], api, state_commit=store.base)
        store.write(PROJECT_PATH, project.json())
        store.write('commands/' + command['id'] + '.json', {'status': 'applied', 'kind': command['kind']})
        store.publish('Record authenticated runtime version transition')
    else:
        read_project(store)
        store.write('inbox/' + identifier(command['id']) + '.json', command)
        store.publish('Record authenticated repository command')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['intake', 'prepare', 'execute', 'publish-controller', 'publish-worker', 'reconcile'])
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY', ''))
    parser.add_argument('--branch', default='argus-state')
    parser.add_argument('--output', type=Path, default=Path('host-output'))
    parser.add_argument('--bundle', type=Path, default=Path('host-input'))
    parser.add_argument('--runtime', type=Path, default=Path('/argus/runtime'))
    parser.add_argument('--worktree', type=Path, default=Path('/argus/project'))
    parser.add_argument('--task-id', default='')
    parser.add_argument('--attempt', type=int, default=1)
    parser.add_argument('--state-commit', default='')
    parser.add_argument('--result', type=Path)
    args = parser.parse_args()
    if args.command == 'execute':
        execute(args)
        return 0
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN', '')
    api = GitHub(args.repository, token)
    for attempt in range(5):
        try:
            return _transaction(args, api, token)
        except StateConflict:
            if attempt == 4:
                raise
            time.sleep(min(2 ** attempt, 8))
    return 1


def _transaction(args: argparse.Namespace, api: GitHub, token: str) -> int:
    with GitStore(args.repository, token=token, branch=args.branch) as store:
        store.load()
        if args.command == 'intake':
            intake(args, api, store)
        elif args.command in {'prepare', 'reconcile'}:
            prepare(args, api, store)
        elif args.command == 'publish-controller':
            proposal = read_json(args.result or args.bundle / 'controller.json')
            expected = 'controller-' + os.environ['GITHUB_RUN_ID'] + '-' + os.environ.get('GITHUB_RUN_ATTEMPT', '1')
            if proposal.get('id') != expected:
                raise ContractError('Controller proposal belongs to another run')
            project = publish_controller(store, proposal, settings=store.read(SETTINGS_PATH, {}))
            store.publish('Accept native Argus decisions and durable task outbox')
            flush_dispatches(store, api)
            store.publish('Record workflow dispatch acknowledgements')
            if should_think(project, new_events=False) and not proposal.get('dispatches'):
                api.wake_controller(project.runtime_sha)
        elif args.command == 'publish-worker':
            result = read_json(args.result or args.bundle / 'result.json')
            if result.get('run_id') != int(os.environ['GITHUB_RUN_ID']):
                raise ContractError('Worker artifact belongs to another run')
            outcome = accept_worker(store, result, api=api)
            store.publish('Accept verified worker result and checkpoint')
            flush_dispatches(store, api)
            store.publish('Record delivery validation dispatch')
            output('result', outcome)
    return 0
