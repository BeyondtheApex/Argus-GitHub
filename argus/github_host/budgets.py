"""Reserve per-slice capacity before parallel runners see independent ledgers."""
from __future__ import annotations

import math
import time
from typing import Any

from .protocol import ContractError, Project


def reserve(project: Project, key: str, settings: dict[str, Any], *, spent: float, tokens: int = 0) -> dict[str, Any] | None:
    cap = float(settings.get('global_daily_cap_usd', 0))
    token_cap = int(settings.get('global_daily_token_cap', 0))
    if not math.isfinite(cap) or cap < 0 or not math.isfinite(spent) or spent < 0:
        raise ContractError('Budget values must be finite and nonnegative')
    existing = project.reservations.get(key)
    if existing:
        return existing if existing['status'] == 'reserved' else None
    held = sum(row['amount_usd'] for row in project.reservations.values() if row['status'] in {'reserved', 'uncertain'})
    available = max(0.0, cap - spent - held)
    target = float(settings.get('slice_budget_usd', cap / max(1, int(settings.get('mission_width', 2)) + 1)))
    if cap and (not math.isfinite(target) or target <= 0):
        raise ContractError('Configured slice_budget_usd must be finite and positive')
    amount = min(available, target)
    if cap and amount < float(settings.get('minimum_slice_budget_usd', 0.01)):
        return None
    held_tokens = sum(row.get('amount_tokens', 0) for row in project.reservations.values() if row['status'] in {'reserved', 'uncertain'})
    token_grant = min(max(0, token_cap - tokens - held_tokens),
                      int(settings.get('slice_token_budget', token_cap // max(1, int(settings.get('mission_width', 2)) + 1))))
    if token_cap < 0 or tokens < 0:
        raise ContractError('Token budgets must be nonnegative')
    if token_cap and token_grant < int(settings.get('minimum_slice_tokens', 1)):
        return None
    grant = {'amount_usd': amount if cap else 0, 'native_cap_usd': spent + amount if cap else 0,
             'amount_tokens': token_grant if token_cap else 0, 'native_token_cap': tokens + token_grant if token_cap else 0,
             'day': time.strftime('%Y-%m-%d', time.gmtime()), 'status': 'reserved', 'created_at': time.time()}
    project.reservations[key] = grant
    return grant


def settle(project: Project, key: str, metrics: dict[str, Any]) -> None:
    grant = project.reservations.get(key)
    if not grant:
        return
    # An interrupted/unpriced result never silently frees potentially spent
    # capacity. The original usage ledgers remain the source of billed cost.
    priced = metrics.get('pricing_status') in {'priced', 'not_billed', 'empty'}
    grant['status'] = 'settled' if priced else 'uncertain'
    grant['settled_at'] = time.time()
    grant['observed_cost_usd'] = metrics.get('cost_usd')


def acknowledge(project: Project, key: str, *, reason: str, actor: str) -> None:
    grant = project.reservations.get(key)
    if not grant or grant['status'] != 'uncertain' or not reason.strip():
        raise ContractError('Only an uncertain settled execution can be acknowledged, with a reason')
    grant.update(status='acknowledged', acknowledged_by=actor, reason=reason, acknowledged_at=time.time())


def known_spend(root: Any) -> float:
    from ..core.cost_control import global_daily_usage_summary
    return float(global_daily_usage_summary(global_root=root).known_cost_usd)


def known_tokens(root: Any) -> int:
    from ..core.cost_control import cost_control_snapshot
    return int(cost_control_snapshot(global_root=root)['daily_tokens'])
