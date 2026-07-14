"""User-facing captive portal: view own balance, redeem vouchers, request
upgrades. The device is always identified by the requesting IP - clients can
never act on another device's MAC."""
import logging

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, session, url_for)

from auth import request_is_loopback, verify_admin
from pricing import format_duration

portal_bp = Blueprint('portal', __name__)
logger = logging.getLogger(__name__)


def _services():
    return current_app.extensions['piso']


def _client_mac():
    """Resolve the requesting device's MAC from its IP."""
    ip = request.remote_addr
    try:
        services = _services()
        mac = services.network_controller.resolve_mac(ip)
        if mac:
            return mac
        settings = services.settings
        if not settings.is_production and not settings.manage_hardware:
            fake_mac = settings.dev_fake_mac.upper()
            if services.network_controller.is_valid_mac(fake_mac):
                return fake_mac
    except Exception as e:
        logger.debug(f"Could not resolve MAC for {ip}: {e}")
    return None


@portal_bp.route('/')
def index():
    if session.get('is_admin'):
        return redirect(url_for('admin.dashboard'))

    svc = _services()
    svc.refresh_runtime_settings()
    mac = _client_mac()
    device = None
    if mac:
        info = svc.user_manager.get_device_info(mac) or {
            'time_balance': 0, 'status': 'inactive',
            'download_limit': svc.settings.default_download_kbps,
            'upload_limit': svc.settings.default_upload_kbps,
            'plan': 'default', 'upgrade_requested': False,
        }
        device = {'mac_address': mac, **info}
    rates = [
        {'pesos': pesos, 'label': format_duration(minutes)}
        for pesos, minutes in svc.user_manager.get_rates().items()
    ]
    posts = svc.user_manager.get_posts(active_only=True)
    return render_template(
        'portal.html',
        device=device,
        rates=rates,
        posts=posts,
        coinslot_enabled=svc.coinslot is not None,
        coin_minutes_per_peso=svc.settings.minutes_per_peso,
        coin_claim_timeout=svc.settings.coinslot_claim_timeout,
        portal_title=svc.settings.portal_title,
        portal_subtitle=svc.settings.portal_subtitle,
    )


@portal_bp.route('/<path:requested_path>', methods=['GET', 'HEAD'])
def captive_redirect(requested_path):
    """Send HTTP connectivity probes and unknown paths to the portal root."""
    settings = _services().settings
    return redirect(
        f'http://{settings.portal_hostname}:{settings.port}/')


@portal_bp.route('/login', methods=['GET', 'POST'])
def login():
    if not request_is_loopback():
        abort(403)
    if request.method == 'POST':
        settings = _services().settings
        if verify_admin(settings, request.form.get('username'),
                        request.form.get('password')):
            session['is_admin'] = True
            flash('Logged in successfully', 'success')
            return redirect(url_for('admin.dashboard'))
        flash('Invalid credentials', 'error')
    return render_template('login.html')


@portal_bp.route('/logout')
def logout():
    session.pop('is_admin', None)
    flash('Logged out successfully', 'success')
    return redirect(url_for('portal.index'))


@portal_bp.route('/redeem', methods=['POST'])
def redeem():
    svc = _services()
    mac = _client_mac()
    if not mac:
        flash('Could not identify your device. Reconnect to the WiFi and try again.', 'error')
        return redirect(url_for('portal.index'))

    code = (request.form.get('code') or '').strip()
    if not code:
        flash('Please enter a voucher code', 'error')
        return redirect(url_for('portal.index'))

    minutes = svc.user_manager.redeem_voucher(code, mac)
    if minutes is None:
        flash('Invalid or already used voucher code', 'error')
    else:
        svc.network_controller.unblock_mac(mac)
        info = svc.user_manager.get_device_info(mac)
        if info:
            svc.network_controller.set_bandwidth_limit(
                mac, info['download_limit'], info['upload_limit'])
        flash(f'Voucher accepted: {minutes:g} minutes added', 'success')
    return redirect(url_for('portal.index'))


@portal_bp.route('/insert_coin', methods=['POST'])
def insert_coin():
    svc = _services()
    svc.refresh_runtime_settings()
    if not svc.coinslot:
        flash('Coinslot is not available', 'error')
        return redirect(url_for('portal.index'))
    mac = _client_mac()
    if not mac:
        flash('Could not identify your device. Reconnect to the WiFi and try again.', 'error')
        return redirect(url_for('portal.index'))

    window = svc.coinslot.claim(mac)
    if window is None:
        flash('The coinslot is in use by another device. Try again shortly.', 'error')
    else:
        flash(f'Coinslot is yours for {window} seconds - insert coins now!', 'success')
    return redirect(url_for('portal.index'))


@portal_bp.route('/coin_status')
def coin_status():
    svc = _services()
    if not svc.coinslot:
        return jsonify({'enabled': False})
    mac = _client_mac()
    balance = svc.user_manager.check_balance(mac) if mac else 0
    return jsonify({'enabled': True, 'balance': balance,
                    **svc.coinslot.status(mac)})


@portal_bp.route('/request_upgrade', methods=['POST'])
def request_upgrade():
    svc = _services()
    mac = _client_mac()
    if not mac:
        flash('Could not identify your device. Reconnect to the WiFi and try again.', 'error')
        return redirect(url_for('portal.index'))

    if svc.user_manager.request_upgrade(mac):
        flash('Premium upgrade requested. Please wait for admin approval.', 'success')
    else:
        flash('Error requesting upgrade', 'error')
    return redirect(url_for('portal.index'))
