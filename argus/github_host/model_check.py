"""Check the selected provider through native tool use and cross-run resume.

This two-turn smoke check is not a research-quality or non-regression benchmark.
It never resumes the real project or promotes a runtime.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path

from ..agent_cli.agent_cli_runner import AgentCliRunner, RunnerOptions
from ..core.run_gateway import run_exec as gateway_run_exec
from .model_provider import OpenAIProvider
from .portability import configure_provider_state, rebase
from .protocol import ContractError, canonical, digest
from .security import scan_payload
from .snapshots import capture, restore
from .sqlite_state import materialize


def run_check(mode: str, *, root: Path, bundle: Path, output: Path,
              runner: AgentCliRunner | None = None) -> dict:
    provider = OpenAIProvider.read(os.environ, require=True)
    assert provider is not None
    for key in ('GITHUB_TOKEN', 'GH_TOKEN', 'GIT_CONFIG_VALUE_0'):
        os.environ.pop(key, None)
    route = digest({'base_url': provider.base_url, 'model': provider.model, 'wire_api': provider.wire_api})
    root, output = root.resolve(), output.resolve()
    state, source = root / 'native', root / 'workspace'
    source.mkdir(parents=True, exist_ok=True)
    prior = None
    if mode == 'resume':
        prior = json.loads((bundle / 'checkpoint.json').read_text(encoding='utf-8'))
        if prior['route'] != route:
            raise ContractError('Provider configuration changed between initial and resumed jobs')
        restore(state, rebase(capture(bundle / 'state'), state, source, portable=False))
        materialize(state)
        expected = prior['expected']
    else:
        expected = 'ARGUS-PROBE-' + uuid.uuid4().hex
        (source / 'verification.txt').write_text(expected, encoding='utf-8')
    configure_provider_state(state, owner='model-check')
    os.environ.update(ARGUS_SKILL_HOME=str(state), ARGUS_GITHUB_HOST='1',
                      ARGUS_GITHUB_WORKTREE=str(source), ARGUS_SKILL_RUNNER_BACKEND='codex',
                      ARGUS_SKILL_SAFE_MODE='1', ARGUS_SKILL_ENGINEER_TURN_MAX_SECONDS='180')
    runner = runner or AgentCliRunner(backend='codex')
    options = RunnerOptions(model=provider.model, sandbox_mode='read-only', force_safe_mode=True,
                            working_dir=str(source), skip_git_repo_check=True, disable_tools=mode == 'resume')
    prompt = ('Use a file-reading tool to read verification.txt. Reply with its complete contents only. '
              'Remember those contents for the next turn.' if mode == 'first' else
              'Recall the exact contents of verification.txt from the previous turn. '
              'That file is not present on this machine. Reply with the contents only, without using tools.')
    started = time.monotonic()
    result = gateway_run_exec(runner, prompt=prompt, resume_thread_id=prior['thread_id'] if prior else None,
                              options=options, run_label='engineer-provider-check')
    elapsed = time.monotonic() - started
    usage = [event['usage'] for event in result.json_events
             if event.get('type') == 'turn.completed' and isinstance(event.get('usage'), dict)]
    checks = {
        'turn_completed': result.exit_code == 0 and result.turn_completed and not result.turn_failed,
        'correct_response': expected in result.last_agent_message,
        'session_identity': bool(result.thread_id) and (not prior or result.thread_id == prior['thread_id']),
        'tool_contract': not result.tool_activity_observed if prior else result.tool_activity_observed,
        'usage_reported': bool(usage) and all(type(row.get(key)) is int and row[key] >= 0
                                          for row in usage for key in ('input_tokens', 'output_tokens')),
    }
    report = {'schema': 1, 'mode': 'real-provider-smoke-check', 'phase': mode,
              'model': provider.model, 'wire_api': provider.wire_api, 'route_fingerprint': route,
              'source_sha': os.environ.get('GITHUB_SHA', ''), 'run_id': os.environ.get('GITHUB_RUN_ID', ''),
              'checks': checks, 'passed': all(checks.values()), 'runner_seconds': elapsed,
              'usage': usage, 'quality_non_regression_tested': False}
    output.mkdir(parents=True, exist_ok=True)
    report_bytes = canonical(report) + b'\n'
    scan_payload({'report.json': report_bytes}, os.environ)
    (output / 'report.json').write_bytes(report_bytes)
    if not report['passed']:
        raise ContractError('Provider smoke check failed: ' + ', '.join(key for key, ok in checks.items() if not ok))
    if mode == 'first':
        files = rebase(capture(state), state, source, portable=True)
        scan_payload(files, os.environ)
        restore(output / 'state', files)
        (output / 'checkpoint.json').write_bytes(canonical({
            'route': route, 'thread_id': result.thread_id, 'expected': expected}) + b'\n')
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['first', 'resume'])
    parser.add_argument('--root', type=Path, default=Path('provider-check-runtime'))
    parser.add_argument('--bundle', type=Path, default=Path('provider-check-input'))
    parser.add_argument('--output', type=Path, default=Path('provider-check-output'))
    args = parser.parse_args()
    try:
        report = run_check(args.mode, root=args.root, bundle=args.bundle, output=args.output)
    except ContractError as error:
        # Deliberately do not print provider stdout/stderr or endpoint contents.
        print(str(error))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
