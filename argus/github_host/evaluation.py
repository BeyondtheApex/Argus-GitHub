"""Validate explicit real-model non-regression evidence before promotion.

This is an evidence gate, not a claim that deterministic fixtures measure model
quality. All observations and evaluator verdicts must come from recorded runs.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from .protocol import ContractError, commit_sha

REQUIRED_SCENARIOS = {'software_refactor', 'research_replan', 'resume_after_failure', 'goal_completion'}


def validate_acceptance(report: dict[str, Any], *, baseline: str, candidate: str) -> None:
    if report.get('schema') != 1 or report.get('mode') != 'real-model':
        raise ContractError('Promotion requires real-model acceptance; fixtures are not quality evidence')
    if report.get('baseline_sha') != baseline or report.get('candidate_sha') != candidate:
        raise ContractError('Acceptance evidence evaluates a different runtime pair')
    commit_sha(baseline)
    commit_sha(candidate)
    if not str(report.get('reviewed_by', '')).strip() or not str(report.get('review_notes', '')).strip():
        raise ContractError('Acceptance requires a named evaluator and review rationale')
    cases = report.get('cases', [])
    if not REQUIRED_SCENARIOS <= {case.get('scenario') for case in cases}:
        raise ContractError('Acceptance omits a required long-running behavior scenario')
    for case in cases:
        if not case.get('backend') or case['backend'] == 'memory' or not case.get('model') or not case.get('dataset_sha'):
            raise ContractError('Every comparison must pin its real backend, model and input dataset')
        commit_sha(case['dataset_sha'])
        samples = case.get('samples', [])
        if len(samples) < 3:
            raise ContractError('At least three paired observations per scenario are required')
        totals = {'native': {'tokens': 0.0, 'model_seconds': 0.0, 'quality': 0.0},
                  'host': {'tokens': 0.0, 'model_seconds': 0.0, 'quality': 0.0}}
        for sample in samples:
            for side in totals:
                value = sample.get(side, {})
                if value.get('accepted') is not True or not str(value.get('evidence', '')).strip():
                    raise ContractError('A paired observation failed acceptance or lacks a trace reference')
                for metric in totals[side]:
                    number = value.get(metric)
                    if type(number) not in {int, float} or not math.isfinite(number) or number < 0:
                        raise ContractError('Invalid observed metric: ' + metric)
                    if metric in {'tokens', 'model_seconds'} and number == 0:
                        raise ContractError('Real-model observations cannot report zero model usage')
                    totals[side][metric] += number
                if not 0 <= value['quality'] <= 1:
                    raise ContractError('Quality is an evaluator score between zero and one')
            overhead = sample['host'].get('orchestration_seconds')
            if type(overhead) not in {int, float} or not math.isfinite(overhead) or overhead < 0:
                raise ContractError('Actions orchestration time must be reported separately')
        if totals['host']['quality'] < totals['native']['quality']:
            raise ContractError('Candidate accepted quality regressed')
        # No silent configurable allowance: a weaker requirement is a reviewed
        # product-policy change, not a field supplied with the same evidence.
        for metric in ('tokens', 'model_seconds'):
            if totals['host'][metric] > totals['native'][metric]:
                raise ContractError('Candidate model-efficiency regressed: ' + metric)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding='utf-8'))
    validate_acceptance(report, baseline=report['baseline_sha'], candidate=report['candidate_sha'])
    print('Recorded real-model comparisons satisfy the explicit promotion policy.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
