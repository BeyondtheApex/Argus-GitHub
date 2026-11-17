"""Reserve per-slice capacity before parallel runners see independent ledgers."""
from __future__ import annotations

import math
import time
from typing import Any

from .protocol import ContractError, Project


def reserve(project: Project, key: str, settings: dict[str, Any], *, spent: float) -> dict[str, Any] | None:
    cap = float(settings.get('global_daily_cap_usd', 0))
    if not math.isfinite(cap) or cap < 0 or not math.isfinite(spent) or spent < 0:
        raise ContractError('Budget values must be finite and nonnegative')
    if cap == 0:
        return {'native_cap_usd': 0, 'amount_usd': 0}
    existing = project.reservations.get(key)
    if existing:
        return existing if existing['status'] == 'reserved' else None
    held = sum(row['amount_usd'] for row in project.reservations.values() if row['status'] in {'reserved', 'uncertain'})
    available = max(0.0, cap - spent - held)
    target = float(settings.get('slice_budget_usd', cap / max(1, int(settings.get('mission_width', 2)) + 1)))
    if not math.isfinite(target) or target <= 0:
        raise ContractError('Configured slice_budget_usd must be finite and positive')
    amount = min(available, target)
    if amount < float(settings.get('minimum_slice_budget_usd', 0.01)):
        return None
    grant = {'amount_usd': amount, 'native_cap_usd': spent + amount,
             'status': 'reserved', 'created_at': time.time()}
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


def known_spend(root: Any) -> float:
    from ..core.cost_control import global_daily_usage_summary
    return float(global_daily_usage_summary(global_root=root).known_cost_usd)
