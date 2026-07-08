"""Admin dashboard and management actions. Every route requires an admin
session and every submitted MAC address is validated before use."""
import logging

from flask import (Blueprint, current_app, flash, jsonify, redirect,
                   render_template, request, url_for)

from auth import admin_required
from network.ap_manager import is_valid_mac

admin_bp = Blueprint('admin', __name__)
logger = logging.getLogger(__name__)


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
    for device in devices:
        mac = device['mac_address']
        info = svc.user_manager.get_device_info(mac)
        if info:
            device.update(info)
        else:
            device.update({
                'time_balance': 0,
                'download_limit': default_download,
                'upload_limit': default_upload,
                'plan': 'default',
                'upgrade_requested': False,
            })
    return devices


def _form_text(name, default='', maximum=120):
    value = (request.form.get(name) or default).strip()
    return value[:maximum]


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
        return render_template('admin.html', devices=devices, plans=plans,
                               minutes_per_peso=svc.settings.minutes_per_peso,
                               revenue=revenue,
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
    active_devices = [device for device in devices if device.get('time_balance', 0) > 0]
    return jsonify({
        'revenue': revenue,
        'device_count': len(devices),
        'active_device_count': len(active_devices),
        'minutes_per_peso': svc.settings.minutes_per_peso,
    })


@admin_bp.route('/admin/settings', methods=['POST'])
@admin_required
def update_settings():
    svc = _services()
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
        return redirect(url_for('admin.dashboard'))

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
    }

    settings_saved = svc.user_manager.update_app_settings(values)
    plan_saved = svc.user_manager.upsert_plan('default', default_download, default_upload)
    if settings_saved and plan_saved:
        svc.refresh_runtime_settings()
        flash('System settings updated', 'success')
    else:
        flash('Error updating system settings', 'error')
    return redirect(url_for('admin.dashboard'))


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

    minutes = amount * svc.settings.minutes_per_peso
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


@admin_bp.route('/vouchers', methods=['GET', 'POST'])
@admin_required
def vouchers():
    svc = _services()
    if request.method == 'POST':
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


@admin_bp.route('/transactions')
@admin_required
def transactions():
    svc = _services()
    return render_template('transactions.html',
                           transactions=svc.user_manager.get_transactions(limit=100))


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
