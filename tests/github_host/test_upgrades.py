from __future__ import annotations

import base64
import copy
import json

import pytest

from argus.github_host.evaluation import REQUIRED_SCENARIOS, validate_acceptance
from argus.github_host.protocol import ContractError, Project
from argus.github_host.upgrades import promote, rollback


def evidence():
    # Synthetic unit input tests gate semantics only. It is never published as
    # an acceptance result and cannot be used to claim model performance.
    observation = {'accepted': True, 'evidence': 'test-only', 'tokens': 10, 'model_seconds': 1, 'quality': 1}
    return {'schema': 1, 'mode': 'real-model', 'baseline_sha': 'a' * 40, 'candidate_sha': 'b' * 40,
        'reviewed_by': 'test-evaluator', 'review_notes': 'test-only', 'cases': [
            {'scenario': name, 'backend': 'test-provider', 'model': 'test-model', 'dataset_sha': 'c' * 40,
             'samples': [{'native': dict(observation), 'host': {**observation, 'orchestration_seconds': 2}} for _ in range(3)]}
            for name in REQUIRED_SCENARIOS]}


class API:
    def __init__(self):
        self.format = {'host_schema': 1, 'snapshot_formats': ['files-v1', 'sqlite-logical-v1'], 'supervisor_continuation': 1}
        self.report = evidence()
        self.tags = []

    def pages(self, path, key):
        return [{'conclusion': 'success', 'head_sha': 'b' * 40}]

    def request(self, method, path):
        value = self.report if 'acceptance.json' in path else self.format
        return {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(json.dumps(value).encode()).decode()}

    def runtime_tag(self, sha):
        self.tags.append(sha)


def test_promotion_and_rollback_preserve_checkpoint_and_require_evidence():
    api = API()
    project = Project(1, 'owner/repo', 'goal', 'a' * 40, 'c' * 40, status='paused')
    with pytest.raises(ContractError):
        promote(project, {'runtime_sha': 'b' * 40}, api, state_commit='d' * 40)
    assert project.runtime_sha == 'a' * 40 and not api.tags
    promote(project, {'runtime_sha': 'b' * 40, 'evidence_sha': 'e' * 40}, api, state_commit='d' * 40)
    assert project.runtime_history[0]['checkpoint'] == 'd' * 40
    rollback(project, {'runtime_sha': 'a' * 40}, api, state_commit='f' * 40)
    assert project.runtime_sha == 'a' * 40
    assert project.runtime_history[-1]['checkpoint'] == 'f' * 40


@pytest.mark.parametrize('mutate', [
    lambda report: report.update(mode='deterministic'),
    lambda report: report['cases'].pop(),
    lambda report: report['cases'][0]['samples'][0]['host'].update(quality=0),
    lambda report: report['cases'][0]['samples'][0]['host'].update(tokens=20),
    lambda report: report['cases'][0]['samples'][0]['host'].update(model_seconds=float('nan')),
])
def test_fixture_missing_coverage_and_regressions_cannot_promote(mutate):
    report = copy.deepcopy(evidence())
    mutate(report)
    with pytest.raises(ContractError):
        validate_acceptance(report, baseline='a' * 40, candidate='b' * 40)
