from unittest.mock import patch

from coinslot import CoinslotService
from network.wired import WiredGateway
from tests.conftest import MAC, OTHER_MAC


class FakeReader:
    def open(self):
        pass

    def close(self):
        pass

    def wait_pulse(self, timeout=1.0):
        return False


def make_service(user_manager, mock_network, settings):
    return CoinslotService(user_manager, mock_network, settings, reader=FakeReader())


def test_pulse_without_claim_is_ignored(user_manager, mock_network, settings):
    svc = make_service(user_manager, mock_network, settings)
    svc._on_pulse()
    assert user_manager.check_balance(MAC) == 0


def test_claim_and_credit(user_manager, mock_network, settings):
    svc = make_service(user_manager, mock_network, settings)
    assert svc.claim(MAC) == settings.coinslot_claim_timeout

    svc._on_pulse()  # 1 pulse = 1 peso by default
    # 1 peso * 5 minutes/peso
    assert user_manager.check_balance(MAC) == 5
    mock_network.unblock_mac.assert_called_with(MAC)

    status = svc.status(MAC)
    assert status['active'] and status['yours']
    assert status['pesos_inserted'] == 1

    tx = user_manager.get_transactions()[0]
    assert tx['source'] == 'coin'


def test_claim_is_exclusive(user_manager, mock_network, settings):
    svc = make_service(user_manager, mock_network, settings)
    assert svc.claim(MAC) is not None
    assert svc.claim(OTHER_MAC) is None       # busy
    assert svc.claim(MAC) is not None         # same device can re-claim


def test_expired_claim_ignores_pulses(user_manager, mock_network, settings):
    svc = make_service(user_manager, mock_network, settings)
    svc.claim(MAC)
    svc._claim['expires'] = 0  # force expiry
    svc._on_pulse()
    assert user_manager.check_balance(MAC) == 0
    assert svc.status(MAC) == {'active': False}


def test_pulses_per_peso(user_manager, mock_network, settings):
    settings.coinslot_pulses_per_peso = 2
    svc = make_service(user_manager, mock_network, settings)
    svc.claim(MAC)
    svc._on_pulse()
    assert user_manager.check_balance(MAC) == 0  # half a peso: no credit yet
    svc._on_pulse()
    assert user_manager.check_balance(MAC) == 5


def test_wired_gateway_station_discovery(settings):
    settings.network_mode = 'wired'
    gw = WiredGateway(settings)
    leases = {
        MAC: {'ip': '192.168.4.2', 'hostname': 'phone', 'lease_expiry': 9999999999},
        OTHER_MAC: {'ip': '192.168.4.3', 'hostname': 'laptop', 'lease_expiry': 9999999999},
    }
    neigh_output = (
        f"192.168.4.2 lladdr {MAC.lower()} REACHABLE\n"
        f"192.168.4.3 lladdr {OTHER_MAC.lower()} FAILED\n"
    )
    with patch.object(gw, 'get_dhcp_leases', return_value=leases), \
         patch('network.wired.run_cmd', return_value=neigh_output):
        stations = gw.get_stations()
    # only the reachable device counts as connected
    assert [s['mac_address'] for s in stations] == [MAC]
    assert stations[0]['ip'] == '192.168.4.2'


def test_facade_selects_wired_backend(settings):
    from network_controller import NetworkController
    settings.network_mode = 'wired'
    nc = NetworkController(settings, manage_hardware=False)
    assert isinstance(nc.ap, WiredGateway)
