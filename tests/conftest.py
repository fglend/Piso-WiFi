import os
import sys
from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Settings  # noqa: E402
from services import Services  # noqa: E402
from user_manager import UserManager  # noqa: E402

MAC = "00:11:22:33:44:55"
OTHER_MAC = "11:22:33:44:55:66"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "unit: marks fast unit tests")


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=str(tmp_path / 'test_piso_wifi.db'),
                    check_interval=5, pause_on_disconnect=True)


@pytest.fixture
def user_manager(settings):
    return UserManager(settings.db_path)


@pytest.fixture
def mock_network(settings):
    """A NetworkController stand-in for route/time-manager tests."""
    nc = MagicMock()
    nc.DEFAULT_DOWNLOAD_SPEED = 2048
    nc.DEFAULT_UPLOAD_SPEED = 1024
    nc.PREMIUM_DOWNLOAD_SPEED = 8096
    nc.PREMIUM_UPLOAD_SPEED = 8096
    nc.ap_interface = settings.ap_interface
    nc.internet_interface = settings.internet_interface
    nc.get_connected_devices.return_value = []
    nc.resolve_mac.return_value = MAC
    nc.block_mac.return_value = True
    nc.unblock_mac.return_value = True
    nc.set_bandwidth_limit.return_value = True
    nc.remove_bandwidth_limit.return_value = True
    return nc


@pytest.fixture
def services(settings, user_manager, mock_network):
    svc = SimpleNamespace(settings=settings, user_manager=user_manager,
                          network_controller=mock_network,
                          time_manager=MagicMock(), coinslot=None)
    # Routes call svc.refresh_runtime_settings() on every request; bind the
    # real Services implementation instead of duplicating its logic here.
    svc.app_setting_defaults = MethodType(Services.app_setting_defaults, svc)
    svc.refresh_runtime_settings = MethodType(Services.refresh_runtime_settings, svc)
    return svc


@pytest.fixture
def app(services):
    from main import create_app
    flask_app = create_app(services=services)
    flask_app.config['TESTING'] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def csrf_token(client):
    """Prime a session and return its CSRF token."""
    client.get('/')
    with client.session_transaction() as sess:
        return sess['csrf_token']


@pytest.fixture
def admin_client(client):
    with client.session_transaction() as sess:
        sess['is_admin'] = True
    return client
