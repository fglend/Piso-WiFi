"""User-facing captive portal: view own balance, redeem vouchers, request
upgrades. The device is always identified by the requesting IP - clients can
never act on another device's MAC."""
import logging
import time

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, session, url_for)

from auth import (is_locked_out, record_login_failure, record_login_success,
                  request_is_loopback, verify_admin)
from post_formatting import render_post_description
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
            'plan': 'default', 'upgrade_requested': False, 'paused': False,
        }
        device = {'mac_address': mac, **info,
                  'expires_at': svc.user_manager.get_expiry(mac)}
    rates = [
        {'pesos': pesos, 'label': format_duration(minutes)}
        for pesos, minutes in svc.user_manager.get_rates().items()
    ]
    posts = [
        {**post, 'description_html': render_post_description(
            post.get('description', ''))}
        for post in svc.user_manager.get_posts(active_only=True)
    ]
    return render_template(
        'portal.html',
        device=device,
        rates=rates,
        posts=posts,
        coinslot_enabled=svc.coinslot is not None,
        # A pass sold as non-pausable overrides the shop-wide setting
        pause_enabled=(svc.settings.allow_manual_pause
                       and (mac is None or svc.user_manager.is_pausable(mac))),
        coin_minutes_per_peso=svc.settings.minutes_per_peso,
        coin_claim_timeout=svc.settings.coinslot_claim_timeout,
        portal_title=svc.settings.portal_title,
        portal_subtitle=svc.settings.portal_subtitle,
    )


@portal_bp.route('/sessions')
def sessions():
    """This device's recent connection history, fetched when the sheet opens.

    Deliberately not part of the portal render: it cost ~11% of every page
    load to build markup that most customers never open, on a box that also
    has to serve every other phone on the AP.

    The MAC comes from the requesting IP, never from the client, so a device
    can only ever read its own history.
    """
    svc = _services()
    mac = _client_mac()
    if not mac:
        return jsonify({'sessions': []})
    return jsonify({'sessions': svc.user_manager.get_device_sessions(mac)})


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
        svc = _services()
        settings = svc.settings
        remote_addr = request.remote_addr or 'unknown'
        if is_locked_out(settings, remote_addr):
            flash('Too many failed login attempts. Try again in a few '
                  'minutes.', 'error')
            svc.user_manager.log_audit(
                'login_locked_out', actor_ip=remote_addr)
            return render_template('login.html')
        if verify_admin(settings, request.form.get('username'),
                        request.form.get('password')):
            record_login_success(remote_addr)
            session['is_admin'] = True
            flash('Logged in successfully', 'success')
            svc.user_manager.log_audit('login_success', actor_ip=remote_addr)
            return redirect(url_for('admin.dashboard'))
        record_login_failure(settings, remote_addr)
        svc.user_manager.log_audit('login_failed', actor_ip=remote_addr)
        flash('Invalid credentials', 'error')
    return render_template('login.html')


@portal_bp.route('/logout')
def logout():
    if session.get('is_admin'):
        _services().user_manager.log_audit(
            'logout', actor_ip=request.remote_addr or 'unknown')
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

    granted = svc.user_manager.redeem_voucher(code, mac)
    if granted is None:
        flash('Invalid or already used voucher code', 'error')
    else:
        svc.network_controller.unblock_mac(mac)
        info = svc.user_manager.get_device_info(mac)
        if info:
            svc.network_controller.set_bandwidth_limit(
                mac, info['download_limit'], info['upload_limit'])
        if granted['duration_days']:
            flash(f"Voucher accepted: {granted['duration_days']:g} day pass, "
                  f"valid until {granted['expires_at']}", 'success')
        else:
            flash(f"Voucher accepted: {granted['minutes']:g} minutes added",
                  'success')
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


@portal_bp.route('/coin_done', methods=['POST'])
def coin_done():
    svc = _services()
    if not svc.coinslot:
        return redirect(url_for('portal.index'))
    mac = _client_mac()
    if not mac:
        flash('Could not identify your device. Reconnect to the WiFi and try again.', 'error')
        return redirect(url_for('portal.index'))
    if svc.coinslot.release(mac):
        flash('Coin session ended. Enjoy your time!', 'success')
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


@portal_bp.route('/pause', methods=['POST'])
def pause():
    svc = _services()
    # Hiding the button is not enough: the endpoint stays reachable, so the
    # switch has to be enforced here too. Resume is left open on purpose - an
    # already-paused device must be able to come back.
    if not svc.settings.allow_manual_pause:
        flash('Pausing is not available on this network.', 'error')
        return redirect(url_for('portal.index'))
    mac = _client_mac()
    if not mac:
        flash('Could not identify your device. Reconnect to the WiFi and try again.', 'error')
        return redirect(url_for('portal.index'))
    if not svc.user_manager.is_pausable(mac):
        flash('This pass cannot be paused.', 'error')
        return redirect(url_for('portal.index'))
    svc.user_manager.set_paused(mac, True)
    svc.user_manager.clear_session(mac)      # freeze the deduction clock
    svc.network_controller.block_mac(mac)    # cut internet while paused
    flash('Your time is paused. Tap Resume when you are ready to continue.', 'success')
    return redirect(url_for('portal.index'))


@portal_bp.route('/resume', methods=['POST'])
def resume():
    svc = _services()
    mac = _client_mac()
    if not mac:
        flash('Could not identify your device. Reconnect to the WiFi and try again.', 'error')
        return redirect(url_for('portal.index'))
    info = svc.user_manager.get_device_info(mac)
    if not info or info['time_balance'] <= 0:
        flash('You have no time left to resume. Insert coins or redeem a voucher.', 'error')
        return redirect(url_for('portal.index'))
    svc.user_manager.set_paused(mac, False)
    svc.user_manager.set_last_deduction(mac, time.time())  # restart the clock now
    svc.network_controller.unblock_mac(mac)
    svc.network_controller.set_bandwidth_limit(
        mac, info['download_limit'], info['upload_limit'])
    flash('Welcome back! Your time is running again.', 'success')
    return redirect(url_for('portal.index'))
