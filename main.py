import atexit
import logging
import os

from flask import Flask

from auth import init_csrf
from config import load_settings

logging.basicConfig(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def create_app(services=None, start_time_manager=True, manage_hardware=True, settings=None):
    """Application factory.

    services: pre-built Services container (tests pass one with
    manage_hardware=False); when omitted, real services are constructed and
    the time manager starts.
    """
    from routes import admin_bp, portal_bp

    owns_services = services is None
    if owns_services:
        from services import Services
        services = Services(settings=settings, manage_hardware=manage_hardware)
        if start_time_manager:
            logger.info("Starting time manager...")
            services.time_manager.start()
            if services.coinslot:
                logger.info("Starting coinslot service...")
                services.coinslot.start()
        atexit.register(services.shutdown)

    app = Flask(__name__)
    app.secret_key = services.settings.secret_key
    app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024
    app.extensions['piso'] = services

    @app.after_request
    def set_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    init_csrf(app)
    app.register_blueprint(portal_bp)
    app.register_blueprint(admin_bp)

    return app


if __name__ == '__main__':
    try:
        logger.info("Starting PISO WIFI application...")
        settings = load_settings()
        app = create_app(
            start_time_manager=settings.manage_hardware,
            manage_hardware=settings.manage_hardware,
            settings=settings,
        )
        # Dev fallback only - production runs gunicorn via wsgi.py (see
        # install_ubuntu.sh). threaded=True keeps the portal responsive
        # while an admin page renders.
        app.run(host=settings.host, port=settings.port,
                debug=not settings.is_production, use_reloader=False,
                threaded=True)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise
