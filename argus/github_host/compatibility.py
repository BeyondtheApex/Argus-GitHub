"""Explicit lock on the small upstream seams used by the repository host."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
from typing import Any

SEAMS = {
    'argus/life/supervisor/_core.py': ['LifeSupervisor.__init__', 'LifeSupervisor.tick', 'LifeSupervisor._run_with_inbox'],
    'argus/life/supervisor/_mission_execution.py': ['MissionExecutionMixin._run_one'],
    'argus/daemon/_life_worker_runtime_context.py': ['_runner_namespace', '_build_supervisor_config'],
    'argus/apps/_runtime_construction.py': ['build_life_runner'],
    'argus/life/memory.py': ['Backlog.claim_next', 'Backlog.next_pending', 'Backlog._is_ready', 'Backlog._done_ids'],
    'argus/tools/subagent/_cli.py': ['cmd_submit', 'main'],
    'argus/tools/subagent/_registry.py': ['reconcile_terminal_task'],
    'argus/tools/subagent/_discuss_run.py': ['_run_discussion'],
}


def stable_ast(node: ast.AST) -> Any:
    """Ignore empty optional AST fields introduced by newer Python versions."""
    fields = {}
    for key, value in ast.iter_fields(node):
        if value is None or value == []:
            continue
        if isinstance(value, ast.AST):
            fields[key] = stable_ast(value)
        elif isinstance(value, list):
            fields[key] = [stable_ast(item) if isinstance(item, ast.AST) else item for item in value]
        else:
            fields[key] = value
    return [type(node).__name__, fields]


def fingerprint(root: Path) -> dict[str, str]:
    result = {}
    for file, names in SEAMS.items():
        module = ast.parse((root / file).read_text(encoding='utf-8'))
        found = {}
        for node in module.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found[node.name] = node
            elif isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        found[node.name + '.' + member.name] = member
        for name in names:
            if name not in found:
                raise ValueError(f'Upstream integration seam disappeared: {file}:{name}')
            value = json.dumps(stable_ast(found[name]), sort_keys=True, ensure_ascii=True,
                               default=lambda value: {'literal_type': type(value).__name__, 'repr': repr(value)})
            result[file + ':' + name] = hashlib.sha256(value.encode()).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--record', action='store_true')
    args = parser.parse_args()
    path = args.root / '.argus-host/compatibility.json'
    actual = fingerprint(args.root)
    if args.record:
        path.write_text(json.dumps({'schema': 1, 'seams': actual}, indent=2) + '\n', encoding='utf-8')
        return 0
    expected = json.loads(path.read_text(encoding='utf-8'))['seams']
    changed = [key for key in expected.keys() | actual.keys() if expected.get(key) != actual.get(key)]
    if changed:
        raise SystemExit('Upstream seams changed; inspect and run parity tests before updating the lock:\n' + '\n'.join(changed))
    print(f'Upstream seam lock verified: {len(actual)} contracts')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
