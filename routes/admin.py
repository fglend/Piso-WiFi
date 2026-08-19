"""Admin dashboard and management actions. Every route requires an admin
session and every submitted MAC address is validated before use."""
import csv
import datetime as dt
import io
import logging
import os
from pathlib import Path
import re
import secrets
import time

from flask import (Blueprint, Response, abort, current_app, flash, jsonify,
                   redirect, render_template, request, url_for)

import content_filter
import integrity
import system_info
from auth import admin_required, request_is_loopback
from config import is_valid_color
from network.ap_manager import is_valid_mac
from pricing import compute_minutes, format_duration

# Report ranges the UI offers as one-tap presets, in days back from today.
REPORT_PRESETS = {'today': 0, '7d': 6, '30d': 29, '90d': 89}
REPORT_GROUPINGS = ('day', 'week', 'month')
# Matches UserManager.get_transactions_between's default cap; the report warns
# the operator when a range returns this many rows.
REPORT_ROW_LIMIT = 10000

admin_bp = Blueprint('admin', __name__)
logger = logging.getLogger(__name__)


@admin_bp.before_request
def require_local_admin_connection():
    if not request_is_loopback():
        abort(403)


def _services():
    return current_app.extensions['piso']


def _form_mac():
    """Validated MAC from the form, or None (with a flash) if invalid."""
    mac = (request.form.get('mac_address') or '').strip().upper()
    if not is_valid_mac(mac):
        flash('Invalid MAC address', 'error')
        return None
    return mac


def _form_number(name, minimum=None, maximum=None, cast=int):
    try:
        value = cast(request.form.get(name, ''))
    except (TypeError, ValueError):
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _dashboard_devices(svc):
    default_download = svc.settings.default_download_kbps
    default_upload = svc.settings.default_upload_kbps
    devices = svc.network_controller.get_connected_devices()
    info_by_mac = svc.user_manager.get_devices_info(
        [device['mac_address'] for device in devices])
    for device in devices:
        info = info_by_mac.get(device['mac_address'])
        if info:
            device.update(info)
        else:
            device.update({
                'time_balance': 0,
                'download_limit': default_download,
                'upload_limit': default_upload,
                'plan': 'default',
                'upgrade_requested': False,
                'paused': False,
                'download_bytes': 0,
                'upload_bytes': 0,
            })
    return devices


def _form_text(name, default='', maximum=120):
    value = (request.form.get(name) or default).strip()
    return value[:maximum]


def _device_state_signature(connected, disconnected):
    connected_state = sorted(device['mac_address'] for device in connected)
    disconnected_state = sorted(
        f"{device['mac_address']}@{device['disconnected_at']}"
        for device in disconnected)
    return '|'.join(connected_state) + '::' + '|'.join(disconnected_state)


@admin_bp.route('/admin')
@admin_required
def dashboard():
    svc = _services()
    try:
        app_settings = svc.refresh_runtime_settings()
        devices = _dashboard_devices(svc)
        active_device_count = len([
            device for device in devices if device.get('time_balance', 0) > 0
        ])
        plans = svc.user_manager.get_plans()
        revenue = svc.user_manager.get_revenue_summary()
        usage_today = svc.user_manager.get_usage_today()
        disconnected_devices = svc.user_manager.get_disconnected_devices()
        # Not part of the live-refresh signature: balances tick every minute
        # and would otherwise reload the dashboard constantly.
        balance_devices = svc.user_manager.get_users_with_balance()
        device_state_signature = _device_state_signature(
            devices, disconnected_devices)
        return render_template('admin.html', devices=devices,
                               disconnected_devices=disconnected_devices,
                               balance_devices=balance_devices,
                               device_state_signature=device_state_signature,
                               plans=plans,
                               minutes_per_peso=svc.settings.minutes_per_peso,
                               revenue=revenue,
                               usage_today=usage_today,
                               health=system_info.collect(svc.settings),
                               app_settings=app_settings,
                               active_device_count=active_device_count)
    except Exception as e:
        logger.error(f"Error in admin dashboard: {e}")
        return "Internal Server Error", 500


@admin_bp.route('/admin/live')
@admin_required
def dashboard_live():
    svc = _services()
    svc.refresh_runtime_settings()
    devices = _dashboard_devices(svc)
    revenue = svc.user_manager.get_revenue_summary()
    usage_today = svc.user_manager.get_usage_today()
    active_devices = [device for device in devices if device.get('time_balance', 0) > 0]
    disconnected_devices = svc.user_manager.get_disconnected_devices()
    return jsonify({
        'revenue': revenue,
        'usage_today': usage_today,
        'device_count': len(devices),
        'active_device_count': len(active_devices),
        'disconnected_device_count': len(disconnected_devices),
        'device_state_signature': _device_state_signature(
            devices, disconnected_devices),
        'minutes_per_peso': svc.settings.minutes_per_peso,
        'health': system_info.collect(svc.settings),
    })


@admin_bp.route('/admin/settings', methods=['GET', 'POST'])
@admin_required
def update_settings():
    svc = _services()
    if request.method == 'GET':
        app_settings = svc.refresh_runtime_settings()
        return render_template('settings.html', app_settings=app_settings)

    minutes_per_peso = _form_number('minutes_per_peso', minimum=1, maximum=240, cast=float)
    claim_timeout = _form_number('coinslot_claim_timeout', minimum=10, maximum=600)
    pulses_per_peso = _form_number('coinslot_pulses_per_peso', minimum=1, maximum=20)
    refresh_seconds = _form_number('dashboard_refresh_seconds', minimum=3, maximum=120)
    default_download = _form_number('default_download_kbps', minimum=32, maximum=100000)
    default_upload = _form_number('default_upload_kbps', minimum=32, maximum=100000)

    if None in (
        minutes_per_peso, claim_timeout, pulses_per_peso,
        refresh_seconds, default_download, default_upload,
    ):
        flash('Settings must use valid numbers within the allowed ranges', 'error')
        return redirect(url_for('admin.update_settings'))

    values = {
        'minutes_per_peso': minutes_per_peso,
        'coinslot_claim_timeout': claim_timeout,
        'coinslot_pulses_per_peso': pulses_per_peso,
        'portal_title': _form_text('portal_title', default='PISO WIFI Portal'),
        'portal_subtitle': _form_text(
            'portal_subtitle',
            default='Only one phone can use the coin slot at a time.',
            maximum=180,
        ),
        'dashboard_refresh_seconds': refresh_seconds,
        'default_download_kbps': default_download,
        'default_upload_kbps': default_upload,
        # Unchecked checkboxes are simply absent from the POST body, so
        # presence is the value.
        'pause_on_disconnect': (
            '1' if request.form.get('pause_on_disconnect') else '0'),
        'allow_manual_pause': (
            '1' if request.form.get('allow_manual_pause') else '0'),
        'portal_footer_text': _form_text('portal_footer_text', maximum=120),
    }
    values.update(_theme_values(svc))

    settings_saved = svc.user_manager.update_app_settings(values)
    plan_saved = svc.user_manager.upsert_plan('default', default_download, default_upload)
    if settings_saved and plan_saved:
        svc.refresh_runtime_settings()
        flash('System settings updated', 'success')
    else:
        flash('Error updating system settings', 'error')
    return redirect(url_for('admin.update_settings'))


def _theme_values(svc):
    """Theme keys for the settings write.

    An invalid or absent colour keeps the value already in force rather than
    snapping back to the shipped default, so a typo in one field never wipes
    branding the operator set earlier.
    """
    current = svc.settings
    values = {}
    for field_name, existing in (
        ('theme_accent', current.theme_accent),
        ('theme_accent_strong', current.theme_accent_strong),
    ):
        candidate = (request.form.get(field_name) or '').strip()
        if candidate and not is_valid_color(candidate):
            flash(f'Ignored invalid colour for {field_name.replace("_", " ")}',
                  'warning')
            candidate = ''
        values[field_name] = candidate or existing

    if request.form.get('remove_logo'):
        if current.portal_logo:
            _remove_image(current.portal_logo)
        values['portal_logo'] = ''
        return values

    uploaded = _save_image(request.files.get('portal_logo'))
    if uploaded:
        # Only drop the old file once the replacement is safely on disk.
        if current.portal_logo:
            _remove_image(current.portal_logo)
        values['portal_logo'] = uploaded
    elif request.files.get('portal_logo') and request.files['portal_logo'].filename:
        flash('Logo must be a valid JPG, PNG, GIF or WebP image', 'error')
        values['portal_logo'] = current.portal_logo
    else:
        values['portal_logo'] = current.portal_logo
    return values


@admin_bp.route('/admin/revenue/reset', methods=['POST'])
@admin_required
def reset_revenue():
    svc = _services()
    removed = svc.user_manager.reset_revenue()
    flash(f'Revenue reset to zero ({removed} record(s) cleared).', 'success')
    return redirect(url_for('admin.update_settings'))


def _parse_mac_lines(text):
    """One MAC per line (or comma-separated); invalid entries are dropped
    with a flash rather than rejecting the whole submission."""
    macs, invalid = [], []
    for raw in re.split(r'[,\n\r]+', text or ''):
        candidate = raw.strip().upper()
        if not candidate:
            continue
        if is_valid_mac(candidate):
            if candidate not in macs:
                macs.append(candidate)
        else:
            invalid.append(candidate)
    return macs, invalid


@admin_bp.route('/admin/security')
@admin_required
def security():
    svc = _services()
    app_settings = svc.refresh_runtime_settings()
    baseline = svc.user_manager.get_integrity_baseline()
    integrity_status = (
        integrity.check_integrity(baseline) if baseline
        else {'ok': None, 'changed': [], 'missing': [], 'new': []})
    reassociation_events = svc.user_manager.get_audit_log(
        limit=10, action='device_reassociated')
    return render_template(
        'security.html', app_settings=app_settings,
        integrity_status=integrity_status, has_baseline=bool(baseline),
        reassociation_events=reassociation_events)


@admin_bp.route('/admin/security/ssh_whitelist', methods=['POST'])
@admin_required
def update_ssh_whitelist():
    svc = _services()
    enabled = request.form.get('ssh_whitelist_enabled') == '1'
    macs, invalid = _parse_mac_lines(request.form.get('ssh_whitelist_macs', ''))
    if invalid:
        flash(f'Ignored invalid MAC address(es): {", ".join(invalid)}', 'warning')
    if enabled and not macs:
        flash('Whitelist left effectively open: no valid MACs were entered. '
              'SSH remains reachable from anywhere.', 'warning')

    saved = svc.user_manager.update_app_settings({
        'ssh_whitelist_enabled': '1' if enabled else '0',
        'ssh_whitelist_macs': ','.join(macs),
    })
    if saved:
        svc.refresh_runtime_settings()
        svc.network_controller.apply_ssh_whitelist(macs, enabled)
        svc.user_manager.log_audit(
            'ssh_whitelist_updated', actor_ip=request.remote_addr or '',
            detail=f'enabled={enabled} macs={len(macs)}')
        flash('SSH whitelist updated', 'success')
    else:
        flash('Error updating SSH whitelist', 'error')
    return redirect(url_for('admin.security'))


@admin_bp.route('/admin/security/dos', methods=['POST'])
@admin_required
def update_dos_protection():
    svc = _services()
    enabled = request.form.get('dos_protection_enabled') == '1'
    saved = svc.user_manager.update_app_settings({
        'dos_protection_enabled': '1' if enabled else '0',
    })
    if saved:
        svc.refresh_runtime_settings()
        svc.network_controller.apply_dos_protection(enabled)
        svc.user_manager.log_audit(
            'dos_protection_updated', actor_ip=request.remote_addr or '',
            detail=f'enabled={enabled}')
        flash(f'DoS mitigation {"enabled" if enabled else "disabled"}', 'success')
    else:
        flash('Error updating DoS mitigation setting', 'error')
    return redirect(url_for('admin.security'))


@admin_bp.route('/admin/security/integrity/baseline', methods=['POST'])
@admin_required
def set_integrity_baseline():
    svc = _services()
    hashes = integrity.compute_hashes()
    if svc.user_manager.set_integrity_baseline(hashes):
        svc.user_manager.log_audit(
            'integrity_baseline_set', actor_ip=request.remote_addr or '',
            detail=f'{len(hashes)} file(s)')
        flash(f'Baseline set from {len(hashes)} tracked file(s)', 'success')
    else:
        flash('Error saving integrity baseline', 'error')
    return redirect(url_for('admin.security'))


def _report_range():
    """Resolve the requested report window to (start, end, preset, group_by).

    Explicit start/end win over a preset. Anything unparseable falls back to
    the last 7 days rather than erroring - a mistyped URL should still show
    the operator a usable report.
    """
    today = dt.date.today()
    preset = request.args.get('preset', '')
    group_by = request.args.get('group_by', '')
    if group_by not in REPORT_GROUPINGS:
        group_by = 'day'

    start_raw = request.args.get('start', '')
    end_raw = request.args.get('end', '')
    start = _parse_date(start_raw)
    end = _parse_date(end_raw)

    if start and end:
        preset = 'custom'
    else:
        days_back = REPORT_PRESETS.get(preset)
        if days_back is None:
            preset, days_back = '7d', REPORT_PRESETS['7d']
        start, end = today - dt.timedelta(days=days_back), today

    if start > end:
        start, end = end, start
    return start, end, preset, group_by


def _parse_date(value):
    try:
        return dt.date.fromisoformat((value or '').strip())
    except ValueError:
        return None


@admin_bp.route('/admin/reports')
@admin_required
def sales_report():
    svc = _services()
    start, end, preset, group_by = _report_range()
    report = svc.user_manager.get_sales_report(
        start.isoformat(), end.isoformat(), group_by)
    return render_template(
        'reports.html', report=report, start=start, end=end,
        preset=preset, group_by=group_by,
        groupings=REPORT_GROUPINGS, presets=list(REPORT_PRESETS),
        earnings=svc.user_manager.get_earnings_summary(),
        app_settings=svc.refresh_runtime_settings())


@admin_bp.route('/admin/reports/export.csv')
@admin_required
def export_sales_csv():
    svc = _services()
    start, end, _, _ = _report_range()
    rows = svc.user_manager.get_transactions_between(
        start.isoformat(), end.isoformat(), limit=REPORT_ROW_LIMIT)
    if len(rows) >= REPORT_ROW_LIMIT:
        logger.warning(
            "Sales export for %s..%s hit the %s row cap and was truncated",
            start, end, REPORT_ROW_LIMIT)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['created_at', 'mac_address', 'source', 'amount', 'minutes'])
    for row in rows:
        writer.writerow([row['created_at'], row['mac_address'], row['source'],
                         f"{float(row['amount']):.2f}", row['minutes'] or 0])

    filename = f"sales-{start.isoformat()}-to-{end.isoformat()}.csv"
    return Response(
        buffer.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@admin_bp.route('/admin/devices/reset_unpaid', methods=['POST'])
@admin_required
def reset_unpaid_devices():
    """Clear connected devices that have no time left off the dashboard.

    Re-blocks them, flushes their stale lease/neighbour/conntrack state and
    forgets them. Nothing is deleted: a device still physically connected is
    treated as new on the next poll, and one that turns out to have balance
    gets its access straight back.
    """
    svc = _services()
    devices = _dashboard_devices(svc)
    # Paused customers still hold time, so they are never in this set.
    unpaid = [device['mac_address'] for device in devices
              if (device.get('time_balance') or 0) <= 0]

    if not unpaid:
        flash('No connected devices without time balance', 'info')
        return redirect(url_for('admin.update_settings'))

    forgotten = svc.network_controller.forget_devices(unpaid)
    flash(f'Cleared {len(forgotten)} connected device(s) with no time balance. '
          'They can reconnect and pay as normal.', 'success')
    logger.info("Admin reset %s unpaid connected device(s)", len(forgotten))
    return redirect(url_for('admin.update_settings'))


@admin_bp.route('/admin/devices/clear_history', methods=['POST'])
@admin_required
def clear_disconnected_history():
    """Empty the Disconnected tab of Device History."""
    svc = _services()
    removed = svc.user_manager.clear_disconnected_history()
    if removed:
        flash(f'Cleared {removed} disconnected device record(s)', 'success')
    else:
        flash('No disconnected device records to clear', 'info')
    logger.info("Admin cleared %s disconnected record(s)", removed)
    return redirect(url_for('admin.update_settings'))


@admin_bp.route('/admin/devices/restricted')
@admin_required
def restricted_devices():
    svc = _services()
    return render_template(
        'restricted_devices.html',
        restricted=svc.user_manager.get_restricted_devices())


@admin_bp.route('/admin/devices/block', methods=['POST'])
@admin_required
def block_device():
    """Manually block a device outright, independent of its time balance."""
    svc = _services()
    mac = _form_mac()
    if not mac:
        return redirect(url_for('admin.restricted_devices'))
    reason = _form_text('reason', maximum=200)

    svc.user_manager.restrict_device(mac, reason)
    svc.network_controller.block_mac(mac)
    svc.user_manager.log_audit(
        'device_restricted', target=mac, actor_ip=request.remote_addr or '',
        detail=reason)
    flash(f'{mac} has been restricted', 'success')
    return redirect(url_for('admin.restricted_devices'))


@admin_bp.route('/admin/devices/unblock', methods=['POST'])
@admin_required
def unblock_device():
    """Clear a manual restriction. Whether the device actually regains
    network access still follows the normal balance-based policy."""
    svc = _services()
    mac = _form_mac()
    if not mac:
        return redirect(url_for('admin.restricted_devices'))

    if not svc.user_manager.unrestrict_device(mac):
        flash(f'No restricted device found for {mac}', 'error')
        return redirect(url_for('admin.restricted_devices'))

    info = svc.user_manager.get_device_info(mac)
    if info and not info.get('paused') and info.get('time_balance', 0) > 0:
        svc.network_controller.unblock_mac(mac)
    svc.user_manager.log_audit(
        'device_unrestricted', target=mac, actor_ip=request.remote_addr or '')
    flash(f'{mac} is no longer restricted', 'success')
    return redirect(url_for('admin.restricted_devices'))


@admin_bp.route('/add_time', methods=['POST'])
@admin_required
def add_time():
    svc = _services()
    svc.refresh_runtime_settings()
    mac = _form_mac()
    amount = _form_number('amount', minimum=1)
    if not mac or amount is None:
        if amount is None:
            flash('Please enter a valid amount', 'error')
        return redirect(url_for('admin.dashboard'))

    minutes = compute_minutes(amount, svc.user_manager.get_rates(),
                              svc.settings.minutes_per_peso)
    logger.info(f"Adding {minutes} minutes for MAC {mac} (₱{amount})")
    if svc.user_manager.add_time(mac, amount, minutes):
        svc.network_controller.unblock_mac(mac)
        info = svc.user_manager.get_device_info(mac)
        if info:
            svc.network_controller.set_bandwidth_limit(
                mac, info['download_limit'], info['upload_limit'])
        flash(f'Added {minutes:g} minutes (₱{amount})', 'success')
    else:
        flash('Error adding time', 'error')
    return redirect(url_for('admin.dashboard'))


@admin_bp.route('/deduct_time', methods=['POST'])
@admin_required
def deduct_time():
    svc = _services()
    mac = _form_mac()
    minutes = _form_number('minutes', minimum=1)
    if not mac or minutes is None:
        if minutes is None:
            flash('Please enter a valid number of minutes', 'error')
        return redirect(url_for('admin.dashboard'))

    if svc.user_manager.deduct_time(mac, minutes, manual=True):
        if svc.user_manager.check_balance(mac) <= 0:
            svc.network_controller.block_mac(mac)
            logger.info(f"Blocked {mac} due to zero balance after manual deduction")
        flash(f'Successfully deducted {minutes} minutes', 'success')
    else:
        flash('Error deducting time', 'error')
    return redirect(url_for('admin.dashboard'))


@admin_bp.route('/admin/transfer_balance', methods=['POST'])
@admin_required
def transfer_balance():
    """Move remaining time to a new MAC, for devices whose randomized Wi-Fi
    address rotated and came back as a stranger with zero balance."""
    svc = _services()
    from_mac = _form_mac()
    to_mac = (request.form.get('to_mac') or '').strip().upper()
    if not from_mac:
        return redirect(url_for('admin.dashboard'))
    if not is_valid_mac(to_mac):
        flash('Invalid destination MAC address', 'error')
        return redirect(url_for('admin.dashboard'))
    if from_mac == to_mac:
        flash('Source and destination MAC are the same', 'error')
        return redirect(url_for('admin.dashboard'))

    minutes = svc.user_manager.transfer_balance(from_mac, to_mac)
    if minutes is None:
        flash('No remaining time to transfer from that device', 'error')
        return redirect(url_for('admin.dashboard'))

    svc.network_controller.unblock_mac(to_mac)
    info = svc.user_manager.get_device_info(to_mac)
    if info:
        svc.network_controller.set_bandwidth_limit(
            to_mac, info['download_limit'], info['upload_limit'])
    if svc.user_manager.check_balance(from_mac) <= 0:
        svc.network_controller.block_mac(from_mac)
    flash(f'Transferred {minutes:g} minutes to {to_mac}', 'success')
    return redirect(url_for('admin.dashboard'))


@admin_bp.route('/admin/set_pausable', methods=['POST'])
@admin_required
def set_pausable():
    """Correct a pass that was sold with the wrong pause permission.

    Submitted as an explicit 1/0 rather than a checkbox: an unchecked box is
    simply absent from the POST body, and "absent" is exactly the mistake this
    route exists to undo.
    """
    svc = _services()
    mac = _form_mac()
    if not mac:
        return redirect(url_for('admin.dashboard'))

    allowed = request.form.get('pausable') == '1'
    if not svc.user_manager.set_pausable(mac, allowed):
        flash(f'No device found for {mac}', 'error')
        return redirect(url_for('admin.dashboard'))

    if allowed:
        flash(f'{mac} may pause their time again', 'success')
    elif svc.user_manager.is_paused(mac):
        # The flag governs new pauses only; an existing pause is left alone so
        # the customer's clock is not restarted without warning.
        flash(f'{mac} can no longer pause. They are paused right now - '
              f'they keep their saved time and can still resume once.',
              'warning')
    else:
        flash(f'{mac} can no longer pause their time', 'success')
    logger.info("Admin set pausable=%s for %s", allowed, mac)
    return redirect(url_for('admin.dashboard'))


@admin_bp.route('/admin/devices/pause', methods=['POST'])
@admin_required
def pause_device():
    """Admin-triggered pause, e.g. a customer asks staff to hold their time
    without using the portal button. Mirrors routes/portal.py's pause()."""
    svc = _services()
    mac = _form_mac()
    if not mac:
        return redirect(url_for('admin.dashboard'))
    if svc.user_manager.is_paused(mac):
        flash(f'{mac} is already paused', 'error')
        return redirect(url_for('admin.dashboard'))
    if not svc.user_manager.is_pausable(mac):
        flash(f"{mac}'s pass cannot be paused", 'error')
        return redirect(url_for('admin.dashboard'))
    if not svc.user_manager.set_paused(mac, True):
        flash(f'No device found for {mac}', 'error')
        return redirect(url_for('admin.dashboard'))
    svc.user_manager.clear_session(mac)      # freeze the deduction clock
    svc.network_controller.block_mac(mac)    # cut internet while paused
    svc.user_manager.log_audit(
        'device_paused', target=mac, actor_ip=request.remote_addr or '')
    flash(f"Paused {mac}'s time", 'success')
    return redirect(url_for('admin.dashboard'))


@admin_bp.route('/admin/devices/resume', methods=['POST'])
@admin_required
def resume_device():
    """Mirrors routes/portal.py's resume()."""
    svc = _services()
    mac = _form_mac()
    if not mac:
        return redirect(url_for('admin.dashboard'))
    info = svc.user_manager.get_device_info(mac)
    if not info or info['time_balance'] <= 0:
        flash(f'{mac} has no time left to resume', 'error')
        return redirect(url_for('admin.dashboard'))
    svc.user_manager.set_paused(mac, False)
    svc.user_manager.set_last_deduction(mac, time.time())  # restart the clock now
    svc.network_controller.unblock_mac(mac)
    svc.network_controller.set_bandwidth_limit(
        mac, info['download_limit'], info['upload_limit'])
    svc.user_manager.log_audit(
        'device_resumed', target=mac, actor_ip=request.remote_addr or '')
    flash(f"Resumed {mac}'s time", 'success')
    return redirect(url_for('admin.dashboard'))


@admin_bp.route('/vouchers/pausable', methods=['POST'])
@admin_required
def set_voucher_pausable():
    """Fix a duration pass created with the wrong pause permission.

    Cascades to the redeeming device when the code is already spent, so the
    operator does not have to remember to correct both places.
    """
    svc = _services()
    code = _form_text('code', maximum=64)
    if not code:
        flash('Missing voucher code', 'error')
        return redirect(url_for('admin.vouchers'))

    allowed = request.form.get('pausable') == '1'
    outcome = svc.user_manager.set_voucher_pausable(code, allowed)
    verb = 'may pause' if allowed else 'cannot pause'

    if not outcome['found']:
        flash(f'No voucher found for {code}', 'error')
    elif outcome['cascaded_to']:
        flash(f'Voucher {code} {verb}. Applied to the device that already '
              f'redeemed it ({outcome["cascaded_to"]}).', 'success')
    else:
        flash(f'Voucher {code} {verb} once redeemed', 'success')
    return redirect(url_for('admin.vouchers'))


@admin_bp.route('/set_bandwidth', methods=['POST'])
@admin_required
def set_bandwidth():
    svc = _services()
    svc.refresh_runtime_settings()
    mac = _form_mac()
    download = _form_number('download', minimum=32, maximum=100000)
    upload = _form_number('upload', minimum=32, maximum=100000)
    if not mac or download is None or upload is None:
        if download is None or upload is None:
            flash('Bandwidth must be between 32 kbps and 100 Mbps', 'error')
        return redirect(url_for('admin.dashboard'))

    if svc.user_manager.set_bandwidth(mac, download, upload):
        if svc.network_controller.set_bandwidth_limit(mac, download, upload):
            flash('Bandwidth limits updated successfully', 'success')
        else:
            flash('Saved, but there was an issue applying the limits', 'warning')
    else:
        flash('Error updating bandwidth settings', 'error')
    return redirect(url_for('admin.dashboard'))


@admin_bp.route('/manage_plan', methods=['POST'])
@admin_required
def manage_plan():
    svc = _services()
    mac = _form_mac()
    new_plan = request.form.get('plan', '')
    if not mac:
        return redirect(url_for('admin.dashboard'))

    info = svc.user_manager.get_device_info(mac)
    if info and info['plan'] == new_plan:
        flash('Device is already on this plan', 'info')
        return redirect(url_for('admin.dashboard'))

    speeds = svc.user_manager.set_plan(mac, new_plan)
    if speeds is None:
        flash('Unknown plan', 'error')
        return redirect(url_for('admin.dashboard'))

    download, upload = speeds
    svc.network_controller.remove_bandwidth_limit(mac)
    if svc.network_controller.set_bandwidth_limit(mac, download, upload):
        flash(f'Plan updated to {new_plan}. New speeds: {download}kbps down / '
              f'{upload}kbps up', 'success')
    else:
        flash('Plan updated but there was an issue applying bandwidth limits', 'warning')
    logger.info(f"Updated plan for {mac} to {new_plan} ({download}/{upload})")
    return redirect(url_for('admin.dashboard'))


ALLOWED_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
GENERATED_IMAGE_NAME = re.compile(
    r'^[0-9a-f]{16}\.(?:jpg|jpeg|png|gif|webp)$')


def _matches_image_format(header, extension):
    if extension in {'.jpg', '.jpeg'}:
        return header.startswith(b'\xff\xd8\xff')
    if extension == '.png':
        return header.startswith(b'\x89PNG\r\n\x1a\n')
    if extension == '.gif':
        return header.startswith((b'GIF87a', b'GIF89a'))
    if extension == '.webp':
        return header.startswith(b'RIFF') and header[8:12] == b'WEBP'
    return False


def _upload_dir():
    path = os.path.join(current_app.static_folder, 'uploads')
    os.makedirs(path, exist_ok=True)
    return path


def _save_image(file):
    """Store an uploaded image under a random server-side name; returns the
    filename or None if the file is missing or has a disallowed extension."""
    if not file or not file.filename:
        return None
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return None
    header = file.stream.read(16)
    file.stream.seek(0)
    if not _matches_image_format(header, ext):
        return None
    name = secrets.token_hex(8) + ext
    file.save(os.path.join(_upload_dir(), name))
    return name


def _remove_image(image_file):
    """Remove only server-generated image names contained by the upload dir."""
    if not image_file or not GENERATED_IMAGE_NAME.fullmatch(image_file):
        logger.warning('Refusing to remove unsafe post image name: %r', image_file)
        return False
    upload_dir = Path(_upload_dir()).resolve()
    image_path = upload_dir / image_file
    if image_path.parent != upload_dir or image_path.is_symlink():
        logger.warning('Refusing to remove post image outside upload directory')
        return False
    try:
        image_path.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        logger.warning('Could not remove post image %s: %s', image_file, exc)
        return False


@admin_bp.route('/admin/posts', methods=['GET', 'POST'])
@admin_required
def posts():
    svc = _services()
    if request.method == 'POST':
        title = _form_text('title', maximum=120)
        description = _form_text('description', maximum=500)
        active = request.form.get('visible_in_portal') == '1'
        if not title:
            flash('Please enter a title', 'error')
            return redirect(url_for('admin.posts'))
        image_file = _save_image(request.files.get('image'))
        if not image_file:
            flash('Please attach a JPG, PNG, GIF or WEBP image', 'error')
            return redirect(url_for('admin.posts'))
        if svc.user_manager.create_post(title, description, image_file, active=active):
            flash('Post published', 'success')
        else:
            _remove_image(image_file)
            flash('Error creating post', 'error')
        return redirect(url_for('admin.posts'))

    return render_template('posts.html', posts=svc.user_manager.get_posts())


@admin_bp.route('/admin/posts/toggle', methods=['POST'])
@admin_required
def toggle_post():
    svc = _services()
    post_id = _form_number('post_id', minimum=1)
    active = request.form.get('active') == '1'
    if post_id is None or not svc.user_manager.set_post_active(post_id, active):
        flash('Error updating post', 'error')
    else:
        flash('Post shown in carousel' if active else 'Post hidden', 'success')
    return redirect(url_for('admin.posts'))


@admin_bp.route('/admin/posts/update', methods=['POST'])
@admin_required
def update_post():
    svc = _services()
    post_id = _form_number('post_id', minimum=1)
    description = _form_text('description', maximum=500)
    if post_id is None or not svc.user_manager.update_post_description(
            post_id, description):
        flash('Error updating post description', 'error')
    else:
        flash('Post description updated', 'success')
    return redirect(url_for('admin.posts'))


@admin_bp.route('/admin/posts/delete', methods=['POST'])
@admin_required
def delete_post():
    svc = _services()
    post_id = _form_number('post_id', minimum=1)
    image_file = svc.user_manager.delete_post(post_id) if post_id else None
    if image_file is None:
        flash('Error deleting post', 'error')
    else:
        if _remove_image(image_file):
            flash('Post deleted', 'success')
        else:
            flash('Post deleted, but its image could not be removed', 'warning')
    return redirect(url_for('admin.posts'))


@admin_bp.route('/admin/rates', methods=['GET', 'POST'])
@admin_required
def rates():
    svc = _services()
    if request.method == 'POST':
        pesos = _form_number('pesos', minimum=1, maximum=10000)
        minutes = _form_number('minutes', minimum=1, maximum=525600, cast=float)
        if pesos is None or minutes is None:
            flash('Enter a valid peso amount and minutes', 'error')
        elif svc.user_manager.upsert_rate(pesos, minutes):
            flash(f'Rate saved: ₱{pesos} = {format_duration(minutes)}', 'success')
        else:
            flash('Error saving rate', 'error')
        return redirect(url_for('admin.rates'))

    svc.refresh_runtime_settings()
    rate_rows = [
        {'pesos': pesos, 'minutes': minutes, 'label': format_duration(minutes)}
        for pesos, minutes in svc.user_manager.get_rates().items()
    ]
    return render_template('rates.html', rates=rate_rows,
                           fallback_rate=svc.settings.minutes_per_peso)


@admin_bp.route('/admin/rates/delete', methods=['POST'])
@admin_required
def delete_rate():
    svc = _services()
    pesos = _form_number('pesos', minimum=1)
    if pesos is None or not svc.user_manager.delete_rate(pesos):
        flash('Error deleting rate', 'error')
    else:
        flash(f'Deleted the ₱{pesos} tier', 'success')
    return redirect(url_for('admin.rates'))


def _apply_content_filter(svc):
    """Re-render and push the dnsmasq drop-in file for the current
    blocklist. Skipped entirely (no-op success) when the feature is off."""
    if not svc.settings.content_filter_enabled:
        return True
    return content_filter.apply_blocklist(
        svc.user_manager.get_blocklist(),
        manage_hardware=getattr(svc.settings, 'manage_hardware', True))


@admin_bp.route('/admin/content-filter', methods=['GET', 'POST'])
@admin_required
def content_filter_page():
    svc = _services()
    if request.method == 'POST':
        pattern = _form_text('pattern', maximum=253).lower()
        category = _form_text('category', default='custom', maximum=40)
        if not pattern:
            flash('Enter a domain to block (e.g. example.com)', 'error')
            return redirect(url_for('admin.content_filter_page'))
        svc.user_manager.add_blocklist_entry(pattern, category)
        _apply_content_filter(svc)
        svc.user_manager.log_audit(
            'blocklist_entry_added', target=pattern,
            actor_ip=request.remote_addr or '', detail=category)
        flash(f'Blocked {pattern}', 'success')
        return redirect(url_for('admin.content_filter_page'))

    svc.refresh_runtime_settings()
    return render_template(
        'content_filter.html', entries=svc.user_manager.get_blocklist(),
        app_settings=svc.settings)


@admin_bp.route('/admin/content-filter/toggle', methods=['POST'])
@admin_required
def toggle_content_filter_entry():
    svc = _services()
    entry_id = _form_number('entry_id', minimum=1)
    enabled = request.form.get('enabled') == '1'
    if entry_id is None or not svc.user_manager.set_blocklist_entry_enabled(
            entry_id, enabled):
        flash('Error updating blocklist entry', 'error')
    else:
        _apply_content_filter(svc)
        flash('Blocklist entry updated', 'success')
    return redirect(url_for('admin.content_filter_page'))


@admin_bp.route('/admin/content-filter/delete', methods=['POST'])
@admin_required
def delete_content_filter_entry():
    svc = _services()
    entry_id = _form_number('entry_id', minimum=1)
    if entry_id is None or not svc.user_manager.delete_blocklist_entry(entry_id):
        flash('Error deleting blocklist entry', 'error')
    else:
        _apply_content_filter(svc)
        flash('Blocklist entry removed', 'success')
    return redirect(url_for('admin.content_filter_page'))


@admin_bp.route('/admin/content-filter/master-toggle', methods=['POST'])
@admin_required
def toggle_content_filter_master():
    """Turn the whole feature on/off without clearing the saved list."""
    svc = _services()
    enabled = request.form.get('content_filter_enabled') == '1'
    if svc.user_manager.update_app_settings(
            {'content_filter_enabled': '1' if enabled else '0'}):
        svc.refresh_runtime_settings()
        if enabled:
            _apply_content_filter(svc)
        else:
            # Clear the drop-in file so previously-blocked domains resolve
            # again immediately, rather than leaving stale rules in place.
            content_filter.apply_blocklist(
                [], manage_hardware=getattr(svc.settings, 'manage_hardware', True))
        flash(f'Content filter {"enabled" if enabled else "disabled"}', 'success')
    else:
        flash('Error updating content filter setting', 'error')
    return redirect(url_for('admin.content_filter_page'))


@admin_bp.route('/vouchers', methods=['GET', 'POST'])
@admin_required
def vouchers():
    svc = _services()
    if request.method == 'POST':
        if request.form.get('mode') == 'duration':
            days = _form_number('duration_days', minimum=1, maximum=365,
                                cast=float)
            price = _form_number('duration_price', minimum=0, cast=float)
            if days is None or price is None:
                flash('Enter a valid number of days (1-365) and a price',
                      'error')
                return redirect(url_for('admin.vouchers'))
            pausable = bool(request.form.get('duration_pausable'))
            # Minutes mirror the pass so every balance display keeps working;
            # the deadline stamped at redemption is what actually governs it.
            code = svc.user_manager.create_voucher(
                round(days * 1440, 2), price=price,
                duration_days=days, pausable=pausable)
            if code:
                flash(f'{days:g}-day pass created: {code} (₱{price:g}, '
                      f'{"pausable" if pausable else "not pausable"}). '
                      f'The {days:g} days start when it is redeemed.', 'success')
            else:
                flash('Error creating voucher', 'error')
            return redirect(url_for('admin.vouchers'))
        if request.form.get('mode') == 'custom':
            price = _form_number('custom_price', minimum=1, cast=float)
            hours = _form_number('custom_hours', minimum=0, cast=float)
            if price is None or not hours or hours <= 0:
                flash('Enter a valid price and duration in hours', 'error')
                return redirect(url_for('admin.vouchers'))
            minutes = round(hours * 60, 2)
            code = svc.user_manager.create_voucher(minutes, price=price)
            if code:
                flash(f'Custom voucher created: {code} '
                      f'(₱{price:g} = {hours:g} hour(s), {minutes:g} min)', 'success')
            else:
                flash('Error creating voucher', 'error')
            return redirect(url_for('admin.vouchers'))
        if request.form.get('mode') == 'price':
            price = _form_number('price', minimum=1)
            if price is None:
                flash('Please enter a valid price in pesos', 'error')
                return redirect(url_for('admin.vouchers'))
            svc.refresh_runtime_settings()
            minutes = compute_minutes(price, svc.user_manager.get_rates(),
                                      svc.settings.minutes_per_peso)
            if minutes <= 0:
                flash('That price converts to 0 minutes - check the rate table', 'error')
                return redirect(url_for('admin.vouchers'))
            code = svc.user_manager.create_voucher(minutes, price=price)
            if code:
                flash(f'Paid voucher created: {code} '
                      f'(₱{price:g} = {minutes:g} minutes)', 'success')
            else:
                flash('Error creating voucher', 'error')
            return redirect(url_for('admin.vouchers'))
        minutes = _form_number('minutes', minimum=1, cast=float)
        if minutes is None:
            flash('Please enter a valid number of minutes', 'error')
        else:
            code = svc.user_manager.create_voucher(minutes)
            if code:
                flash(f'Voucher created: {code} ({minutes:g} minutes)', 'success')
            else:
                flash('Error creating voucher', 'error')
        return redirect(url_for('admin.vouchers'))

    show_all = request.args.get('all') == '1'
    voucher_list = svc.user_manager.get_vouchers(include_redeemed=show_all)
    return render_template('vouchers.html', vouchers=voucher_list, show_all=show_all)


@admin_bp.route('/transactions', methods=['GET', 'POST'])
@admin_required
def transactions():
    svc = _services()
    if request.method == 'POST':
        amount = _form_number('amount', minimum=1, cast=float)
        if amount is None:
            flash('Enter a valid amount to deduct from revenue', 'error')
        elif svc.user_manager.record_revenue_adjustment(amount):
            flash(f'Revenue adjusted down by ₱{amount:g}', 'success')
        else:
            flash('Error recording revenue adjustment', 'error')
        return redirect(url_for('admin.transactions'))
    return render_template(
        'transactions.html',
        transactions=svc.user_manager.get_transactions(limit=100),
        revenue=svc.user_manager.get_revenue_summary())


AUDIT_LOG_ROW_LIMIT = 2000


@admin_bp.route('/admin/audit-log')
@admin_required
def audit_log():
    svc = _services()
    action = request.args.get('action') or None
    start, end, preset, _ = _report_range()
    entries = svc.user_manager.get_audit_log(
        limit=AUDIT_LOG_ROW_LIMIT, action=action,
        start_date=start.isoformat(), end_date=end.isoformat())
    return render_template(
        'audit_log.html', entries=entries, action=action,
        start=start, end=end, preset=preset, presets=list(REPORT_PRESETS))


@admin_bp.route('/admin/audit-log/export.csv')
@admin_required
def export_audit_log_csv():
    svc = _services()
    action = request.args.get('action') or None
    start, end, _, _ = _report_range()
    entries = svc.user_manager.get_audit_log(
        limit=AUDIT_LOG_ROW_LIMIT, action=action,
        start_date=start.isoformat(), end_date=end.isoformat())

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['ts', 'action', 'actor_ip', 'target', 'detail'])
    for row in entries:
        writer.writerow([row['ts'], row['action'], row['actor_ip'],
                         row['target'], row['detail']])

    filename = f"audit-log-{start.isoformat()}-to-{end.isoformat()}.csv"
    return Response(
        buffer.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@admin_bp.route('/debug/connections')
@admin_required
def debug_connections():
    svc = _services()
    try:
        from network.command import run_cmd
        nc = svc.network_controller
        return jsonify({
            'connected_devices': nc.get_connected_devices(),
            'ap_interface_status': run_cmd(['ip', 'addr', 'show', nc.ap_interface],
                                           ignore_errors=True),
            'internet_interface_status': run_cmd(['ip', 'addr', 'show',
                                                  nc.internet_interface],
                                                 ignore_errors=True),
            'hostapd_running': nc.ap.is_hostapd_running(),
            'iptables_rules': run_cmd(['iptables', '-L', '-n', '-v'],
                                      ignore_errors=True),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
