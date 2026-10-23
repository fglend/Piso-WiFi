"""Loyalty points: how many points a purchase earns.

Side-effect free, like pricing.py. The coinslot snapshots a PointsRules at
claim time (as it does the rate table) so a settings change mid-session can't
split one customer's coins across two earning rules.

Earn modes:
    peso   - points_per_peso for every peso paid
    minute - points_per_minute for every minute bought, so larger tiers (which
             carry bonus minutes) earn proportionally more
"""
from dataclasses import dataclass

EARN_MODES = ('peso', 'minute')


@dataclass(frozen=True)
class PointsRules:
    enabled: bool
    earn_mode: str
    per_peso: float
    per_minute: float
    redeem_cost: float
    redeem_minutes: float


def rules_from_settings(settings):
    return PointsRules(
        enabled=bool(settings.points_enabled),
        earn_mode=settings.points_earn_mode,
        per_peso=float(settings.points_per_peso),
        per_minute=float(settings.points_per_minute),
        redeem_cost=float(settings.points_redeem_cost),
        redeem_minutes=float(settings.points_redeem_minutes),
    )


def compute_points(pesos, minutes, rules):
    """Points earned for one purchase of `pesos` that bought `minutes`."""
    if not rules.enabled:
        return 0
    if rules.earn_mode == 'peso':
        earned = max(0.0, float(pesos)) * rules.per_peso
    elif rules.earn_mode == 'minute':
        earned = max(0.0, float(minutes)) * rules.per_minute
    else:
        return 0
    return max(0.0, earned)
