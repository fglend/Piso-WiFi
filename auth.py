import hmac
import secrets
from functools import wraps

from flask import flash, redirect, request, session, url_for, abort
from werkzeug.security import check_password_hash


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
