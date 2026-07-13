"""Tiered pricing: map a peso amount to minutes using an admin-editable
rate table, decomposed greedily (largest tier first) so totals always land
on the best combination of tiers for the customer.

Example with the default table: ₱6 = ₱5 tier (60 min) + ₱1 tier (10 min).
"""

# pesos -> minutes
DEFAULT_RATES = {
    1: 10,          # 10 minutes
    5: 60,          # 1 hour
    10: 150,        # 2.5 hours
    20: 360,        # 6 hours
    50: 1440,       # 1 day
    100: 4320,      # 3 days
    150: 7200,      # 5 days
    300: 21600,     # 15 days
    500: 43200,     # 30 days
}


def compute_minutes(total_pesos, rates, fallback_minutes_per_peso=0):
    """Minutes earned for a cumulative peso amount.

    Greedy largest-tier-first decomposition of the total. Pesos that cannot
    be decomposed (below the smallest tier) earn the fallback per-peso rate.
    With an empty table the whole amount uses the fallback rate.
    """
    total_pesos = max(0, int(total_pesos))
    if not rates:
        return total_pesos * fallback_minutes_per_peso

    minutes = 0.0
    remaining = total_pesos
    for pesos in sorted(rates, reverse=True):
        if pesos <= 0:
            continue
        count, remaining = divmod(remaining, pesos)
        minutes += count * rates[pesos]
    minutes += remaining * fallback_minutes_per_peso
    return minutes


def format_duration(minutes):
    """Human-friendly duration: '10 minutes', '1 hour', '2.5 hours', '3 days'."""
    minutes = float(minutes)
    if minutes < 60:
        value, unit = minutes, 'minute'
    elif minutes < 1440:
        value, unit = minutes / 60, 'hour'
    else:
        value, unit = minutes / 1440, 'day'
    rounded = round(value, 1)
    text = f'{rounded:g}'
    plural = '' if rounded == 1 else 's'
    return f'{text} {unit}{plural}'
