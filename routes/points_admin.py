"""Admin pages for loyalty points: rules, manual adjustments, the ledger.

A separate blueprint so routes/admin.py doesn't keep growing; it shares that
module's MAC/number validators and its loopback-only guard.
"""
import logging
import math

from flask import (Blueprint, flash, redirect, render_template, request,
                   url_for)

from auth import admin_required
from loyalty import EARN_MODES, rules_from_settings
from network.ap_manager import is_valid_mac
from pricing import format_duration
from routes.admin import (_form_mac, _form_number, _services,
                          require_local_admin_connection)

points_admin_bp = Blueprint('points_admin', __name__)
points_admin_bp.before_request(require_local_admin_connection)
logger = logging.getLogger(__name__)

MAX_ADJUST = 1_000_000
MAX_NOTE = 120


def _form_float(name, minimum, maximum):
    """Like _form_number, but also rejects NaN, which slips past both range
    comparisons because every comparison with NaN is False."""
    value = _form_number(name, minimum=minimum, maximum=maximum, cast=float)
    return value if value is not None and math.isfinite(value) else None


@points_admin_bp.route('/admin/points')
@admin_required
def points_page():
    svc = _services()
    svc.refresh_runtime_settings()
    mac_filter = (request.args.get('mac') or '').strip().upper()
    if mac_filter and not is_valid_mac(mac_filter):
        flash('Invalid MAC address filter', 'error')
        mac_filter = ''
    rules = rules_from_settings(svc.settings)
    return render_template(
        'points.html',
        rules=rules,
        redeem_label=format_duration(rules.redeem_minutes),
        summary=svc.user_manager.get_points_summary(),
        ledger=svc.user_manager.get_points_ledger(mac_filter or None),
        mac_filter=mac_filter,
        account=(svc.user_manager.get_points(mac_filter)
                 if mac_filter else None),
        earn_modes=EARN_MODES,
    )


@points_admin_bp.route('/admin/points/settings', methods=['POST'])
@admin_required
def save_points_settings():
    svc = _services()
    mode = (request.form.get('points_earn_mode') or '').strip().lower()
    per_peso = _form_float('points_per_peso', 0, 1000)
    per_minute = _form_float('points_per_minute', 0, 100)
    cost = _form_float('points_redeem_cost', 1, MAX_ADJUST)
    minutes = _form_float('points_redeem_minutes', 1, 525600)

    if mode not in EARN_MODES or None in (per_peso, per_minute, cost, minutes):
        flash('Points settings must use a valid earn mode and numbers within '
              'the allowed ranges', 'error')
        return redirect(url_for('points_admin.points_page'))

    values = {
        # Unchecked checkbox is absent from the POST body: presence is the value.
        'points_enabled': '1' if request.form.get('points_enabled') else '0',
        'points_earn_mode': mode,
        'points_per_peso': per_peso,
        'points_per_minute': per_minute,
        'points_redeem_cost': cost,
        'points_redeem_minutes': minutes,
    }
    if svc.user_manager.update_app_settings(values):
        svc.refresh_runtime_settings()
        svc.user_manager.log_audit(
            'points_settings_updated',
            detail=', '.join(f'{k}={v}' for k, v in values.items()),
            actor_ip=request.remote_addr or '')
        flash('Points settings updated', 'success')
    else:
        flash('Error updating points settings', 'error')
    return redirect(url_for('points_admin.points_page'))


@points_admin_bp.route('/admin/points/adjust', methods=['POST'])
@admin_required
def adjust_points():
    svc = _services()
    mac = _form_mac()
    if not mac:
        return redirect(url_for('points_admin.points_page'))
    delta = _form_float('delta', -MAX_ADJUST, MAX_ADJUST)
    if not delta:
        flash('Enter a non-zero number of points to add or remove', 'error')
        return redirect(url_for('points_admin.points_page', mac=mac))
    note = (request.form.get('note') or '').strip()[:MAX_NOTE]

    balance = svc.user_manager.adjust_points(mac, delta, note=note)
    if balance is None:
        flash('Error adjusting points', 'error')
    else:
        svc.user_manager.log_audit(
            'points_adjusted', target=mac,
            detail=f'delta={delta:g} balance={balance:g} note={note}',
            actor_ip=request.remote_addr or '')
        logger.info("Admin adjusted points for %s by %g", mac, delta)
        flash(f'{mac} now has {balance:g} points', 'success')
    return redirect(url_for('points_admin.points_page', mac=mac))
