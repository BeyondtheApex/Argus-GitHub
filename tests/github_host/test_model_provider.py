from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest

from argus.agent_cli.models import AgentRunResult
from argus.core.knobs import backend_uses_openai_catalog
from argus.github_host.model_check import run_check
from argus.github_host.model_provider import OpenAIProvider, apply_role_defaults
from argus.github_host.portability import configure_provider_state
from argus.github_host.protocol import ContractError
from argus.github_host.snapshots import capture, make_delta, merge_delta
from argus.tools.capability_vault import read_codex_default_model, read_codex_provider_config


@pytest.fixture
def provider_env(monkeypatch, tmp_path):
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://provider.example.test/v1')
    monkeypatch.setenv('OPENAI_MODEL', 'provider-native-model')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-key-do-not-publish')
    monkeypatch.delenv('OPENAI_WIRE_API', raising=False)
    monkeypatch.setenv('ARGUS_SKILL_HOME', str(tmp_path / 'isolated'))


def test_custom_catalog_and_native_role_defaults_are_preserved(provider_env, tmp_path):
    configure_provider_state(tmp_path, owner='controller')
    home = Path(os.environ['CODEX_HOME'])
    config = tomllib.loads((home / 'config.toml').read_text(encoding='utf-8'))
    assert config['model_provider'] == 'argus_repository'
    assert config['model'] == 'provider-native-model'
    assert os.environ['OPENAI_API_KEY'] not in (home / 'config.toml').read_text()
    assert read_codex_provider_config().base_url == 'https://provider.example.test/v1'
    assert read_codex_default_model() == 'provider-native-model'
    assert not backend_uses_openai_catalog('codex')
    options = {'backend': 'codex', 'reviewer_model': 'explicit-review-model'}
    apply_role_defaults(options)
    assert options['engineer_model'] == 'provider-native-model'
    assert options['reviewer_model'] == 'explicit-review-model'
    other = {'backend': 'memory'}
    apply_role_defaults(other)
    assert other == {'backend': 'memory'}


@pytest.mark.parametrize('update', [
    {'OPENAI_BASE_URL': ''}, {'OPENAI_MODEL': ''}, {'OPENAI_API_KEY': ''},
    {'OPENAI_BASE_URL': 'https://user:password@provider.test/v1'},
    {'OPENAI_BASE_URL': 'https://provider.test/v1?api_key=unsafe'},
    {'OPENAI_BASE_URL': 'http://remote-provider.test/v1'},
    {'OPENAI_BASE_URL': 'https://[invalid'},
    {'OPENAI_WIRE_API': 'chat'},
])
def test_incomplete_or_unsupported_routes_fail_before_calling_a_model(provider_env, update):
    with pytest.raises(ContractError):
        OpenAIProvider.read({**os.environ, **update}, require=True)


def test_session_state_persists_but_secret_derived_route_config_cannot(provider_env, tmp_path):
    configure_provider_state(tmp_path, owner='controller')
    home = Path(os.environ['CODEX_HOME'])
    (home / 'auth.json').write_text('{"secret":"do not persist"}')
    (home / 'session.jsonl').write_text('{"memory":"retained"}\n')
    files = capture(tmp_path)
    assert 'provider-sessions/controller/codex/session.jsonl' in files
    assert not any(name.endswith(('auth.json', 'config.toml')) for name in files)
    injected = {'provider-sessions/controller/codex/config.toml': b'api_key="unsafe"'}
    with pytest.raises(ContractError, match='Ephemeral provider'):
        merge_delta({}, {}, make_delta({}, injected))


class FixtureRunner:
    """Exercise smoke-check logic only; never publish this as model evidence."""

    def __init__(self, *, leak=False, wrong_resume=False):
        self.leak, self.wrong_resume = leak, wrong_resume

    def run_exec(self, *, prompt, resume_thread_id, options, run_label):
        home = Path(os.environ['CODEX_HOME'])
        if resume_thread_id:
            assert not (Path(options.working_dir) / 'verification.txt').exists()
            assert options.disable_tools
            memory = json.loads((home / 'session.json').read_text())
            expected = memory['expected'] if not self.wrong_resume else 'unrelated answer'
        else:
            expected = (Path(options.working_dir) / 'verification.txt').read_text()
            (home / 'session.json').write_text(json.dumps({'expected': expected}))
            if self.leak:
                (home / 'accidental-key.txt').write_text(os.environ['OPENAI_API_KEY'])
        return AgentRunResult(command=['test-only'], exit_code=0, thread_id='test-session',
            agent_messages=[expected], turn_completed=True, tool_activity_observed=not resume_thread_id,
            json_events=[{'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 5}}])


def test_probe_restores_the_same_session_on_a_new_root(provider_env, tmp_path):
    first = run_check('first', root=tmp_path / 'machine-one', bundle=tmp_path / 'unused',
                      output=tmp_path / 'transfer', runner=FixtureRunner())
    resumed = run_check('resume', root=tmp_path / 'machine-two', bundle=tmp_path / 'transfer',
                        output=tmp_path / 'report', runner=FixtureRunner())
    assert first['passed'] and resumed['passed']
    assert not resumed['quality_non_regression_tested']
    with pytest.raises(ContractError, match='correct_response'):
        run_check('resume', root=tmp_path / 'machine-three', bundle=tmp_path / 'transfer',
                  output=tmp_path / 'failed', runner=FixtureRunner(wrong_resume=True))
    assert not json.loads((tmp_path / 'failed/report.json').read_text())['passed']


def test_probe_refuses_credentials_in_checkpoint_artifacts(provider_env, tmp_path):
    with pytest.raises(ContractError, match='Known credential'):
        run_check('first', root=tmp_path / 'runtime', bundle=tmp_path / 'unused',
                  output=tmp_path / 'output', runner=FixtureRunner(leak=True))
    assert not (tmp_path / 'output/checkpoint.json').exists()
