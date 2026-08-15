import hmac
import secrets
import time
from functools import wraps
from ipaddress import ip_address
from threading import Lock

from flask import flash, redirect, request, session, url_for, abort
from werkzeug.security import check_password_hash


# Failed-login tracker, keyed by remote_addr. In-memory rather than
# DB-backed: the admin panel is already loopback-gated (see
# request_is_loopback below), so the realistic attacker here is a single
# low-volume local process, not a distributed brute force that would need to
# survive a restart. Cleared on process restart, which is an acceptable
# trade-off for that threat model.
_login_failures = {}
_login_lock = Lock()


def _prune_failures(key, window_start):
    _login_failures[key] = [t for t in _login_failures.get(key, []) if t >= window_start]
    if not _login_failures[key]:
        _login_failures.pop(key, None)


def is_locked_out(settings, key):
    """True if `key` (typically remote_addr) has hit the failure threshold
    within the lockout window."""
    with _login_lock:
        window_start = time.monotonic() - settings.login_lockout_seconds
        _prune_failures(key, window_start)
        return len(_login_failures.get(key, [])) >= settings.login_max_attempts


def record_login_failure(settings, key):
    with _login_lock:
        window_start = time.monotonic() - settings.login_lockout_seconds
        _prune_failures(key, window_start)
        _login_failures.setdefault(key, []).append(time.monotonic())


def record_login_success(key):
    """Successful login clears any prior failures for this key."""
    with _login_lock:
        _login_failures.pop(key, None)


def request_is_loopback():
    """Administration is available only through the local/SSH-tunnel path."""
    try:
        return ip_address(request.remote_addr).is_loopback
    except ValueError:
        return False


def verify_admin(settings, username, password):
    """Check admin credentials. Prefers ADMIN_PASSWORD_HASH; falls back to a
    constant-time comparison against the plaintext env password."""
    if not username or not password:
        return False
    if not hmac.compare_digest(username, settings.admin_username):
        return False
    if settings.admin_password_hash:
        return check_password_hash(settings.admin_password_hash, password)
    return hmac.compare_digest(password, settings.admin_password)


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('is_admin'):
            flash('Admin access required', 'error')
            return redirect(url_for('portal.index'))
        return view(*args, **kwargs)
    return wrapped


def init_csrf(app):
    """Session-token CSRF protection for all non-GET requests."""

    @app.before_request
    def check_csrf():
        if 'csrf_token' not in session:
            session['csrf_token'] = secrets.token_hex(16)
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return
        sent = request.form.get('csrf_token', '')
        if not hmac.compare_digest(session['csrf_token'], sent):
            abort(400, description='Invalid or missing CSRF token')

    @app.context_processor
    def inject_csrf_token():
        return {'csrf_token': session.get('csrf_token', '')}
