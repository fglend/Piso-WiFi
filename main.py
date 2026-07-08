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


def create_app(services=None, start_time_manager=True):
    """Application factory.

    services: pre-built Services container (tests pass one with
    manage_hardware=False); when omitted, real services are constructed and
    the time manager starts.
    """
    from routes import admin_bp, portal_bp

    if services is None:
        from services import Services
        services = Services()
        if start_time_manager:
            logger.info("Starting time manager...")
            services.time_manager.start()
            if services.coinslot:
                logger.info("Starting coinslot service...")
                services.coinslot.start()

    app = Flask(__name__)
    app.secret_key = services.settings.secret_key
    app.extensions['piso'] = services

    init_csrf(app)
    app.register_blueprint(portal_bp)
    app.register_blueprint(admin_bp)

    return app


if __name__ == '__main__':
    try:
        logger.info("Starting PISO WIFI application...")
        settings = load_settings()
        app = create_app()
        app.run(host=settings.host, port=settings.port,
                debug=not settings.is_production, use_reloader=False)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise
