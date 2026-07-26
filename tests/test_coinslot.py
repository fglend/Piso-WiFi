from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from coinslot import CoinslotService, GPIO_ROOT, SysfsGpioRelay
from network.wired import WiredGateway
from services import Services
from tests.conftest import MAC, OTHER_MAC


class FakeReader:
    def open(self):
        pass

    def close(self):
        pass

    def wait_pulse(self, timeout=1.0):
        return False


class FakeRelay:
    def __init__(self):
        self.active = None  # None until open() runs, then True/False

    def open(self):
        self.active = False

    def set(self, active):
        self.active = active

    def close(self):
        self.active = False


def make_service(user_manager, mock_network, settings, relay=None):
    return CoinslotService(user_manager, mock_network, settings,
                           reader=FakeReader(), relay=relay or FakeRelay())


def _settle(svc):
    """Fast-forward past the inter-coin gap so pending pulses get credited."""
    if svc._claim:
        svc._claim['last_pulse'] = 0
    svc._flush_credit_if_settled()


def test_pulse_without_claim_is_ignored(user_manager, mock_network, settings):
    svc = make_service(user_manager, mock_network, settings)
    svc._count_pulse()
    assert user_manager.check_balance(MAC) == 0


def test_stray_pulses_are_throttled_and_counted(user_manager, mock_network,
                                                 settings, caplog):
    """A stuck SIG pin must not flood the log: one summary per interval."""
    svc = make_service(user_manager, mock_network, settings)
    import logging
    with caplog.at_level(logging.WARNING, logger='coinslot'):
        for _ in range(50):
            svc._count_pulse()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    # One summary line for the burst, not one per pulse
    assert len(warnings) == 1
    assert svc._stray_pulses == 49  # counted since the last summary
    assert user_manager.check_balance(MAC) == 0


def test_claim_and_credit(user_manager, mock_network, settings):
    svc = make_service(user_manager, mock_network, settings)
    assert svc.claim(MAC) == settings.coinslot_claim_timeout

    svc._count_pulse()  # 1 pulse = 1 peso by default
    _settle(svc)
    # ₱1 tier of the seeded rate table = 10 minutes
    assert user_manager.check_balance(MAC) == 10
    mock_network.unblock_mac.assert_called_with(MAC)

    status = svc.status(MAC)
    assert status['active'] and status['yours']
    assert status['pesos_inserted'] == 1

    tx = user_manager.get_transactions()[0]
    assert tx['source'] == 'coin'


def test_multi_pulse_coin_credits_once_after_burst(user_manager, mock_network,
                                                   settings):
    """A ₱5 coin arrives as 5 fast pulses; it must credit ₱5, not ₱1."""
    svc = make_service(user_manager, mock_network, settings)
    svc.claim(MAC)
    for _ in range(5):
        svc._count_pulse()

    # Mid-burst (no settle gap yet): nothing credited.
    svc._flush_credit_if_settled()
    assert svc.status(MAC)['pesos_inserted'] == 0

    # Burst goes quiet -> the whole coin is credited exactly once.
    _settle(svc)
    assert svc.status(MAC)['pesos_inserted'] == 5


def test_claim_is_exclusive(user_manager, mock_network, settings):
    svc = make_service(user_manager, mock_network, settings)
    assert svc.claim(MAC) is not None
    assert svc.claim(OTHER_MAC) is None       # busy
    assert svc.claim(MAC) is not None         # same device can re-claim


def test_expired_claim_ignores_pulses(user_manager, mock_network, settings):
    svc = make_service(user_manager, mock_network, settings)
    svc.claim(MAC)
    svc._claim['expires'] = 0  # force expiry
    svc._count_pulse()
    assert user_manager.check_balance(MAC) == 0
    assert svc.status(MAC) == {'active': False}


def test_pulses_per_peso(user_manager, mock_network, settings):
    settings.coinslot_pulses_per_peso = 2
    svc = make_service(user_manager, mock_network, settings)
    svc.claim(MAC)
    svc._count_pulse()
    _settle(svc)
    assert user_manager.check_balance(MAC) == 0  # half a peso: no credit yet
    svc._count_pulse()
    _settle(svc)
    assert user_manager.check_balance(MAC) == 10  # ₱1 tier


def test_relay_energizes_on_claim(user_manager, mock_network, settings):
    relay = FakeRelay()
    svc = make_service(user_manager, mock_network, settings, relay=relay)
    assert relay.active is None  # not yet opened
    svc.claim(MAC)
    assert relay.active is True


def test_sysfs_relay_initializes_at_inactive_level():
    for active_high, inactive_direction, inactive_value in (
            (False, 'high', '1'),
            (True, 'low', '0')):
        relay = SysfsGpioRelay(7, active_high=active_high)
        with patch('coinslot.os.path.isdir', return_value=True), \
                patch.object(relay, '_write') as write:
            relay.open()

        assert write.call_args_list == [
            ((f'{GPIO_ROOT}/gpio7/direction', inactive_direction),),
            ((f'{GPIO_ROOT}/gpio7/value', inactive_value),),
        ]


def test_services_shutdown_releases_relay_before_stopping_meter():
    service = object.__new__(Services)
    events = []
    service.coinslot = MagicMock()
    service.time_manager = MagicMock()
    service.coinslot.stop.side_effect = lambda: events.append('coinslot')
    service.time_manager.stop.side_effect = lambda: events.append('meter')
    service._shutdown = False

    service.shutdown()
    service.shutdown()

    assert events == ['coinslot', 'meter']


def test_services_shutdown_can_retry_after_coinslot_cleanup_failure():
    service = object.__new__(Services)
    service.coinslot = MagicMock()
    service.time_manager = MagicMock()
    service.coinslot.stop.side_effect = [RuntimeError('GPIO write failed'), None]
    service._shutdown = False

    service.shutdown()
    service.shutdown()

    assert service.coinslot.stop.call_count == 2
    assert service._shutdown is True


def test_app_registers_shutdown_for_owned_hardware_services():
    fake_services = SimpleNamespace(
        settings=SimpleNamespace(secret_key='test-secret'),
        time_manager=MagicMock(),
        coinslot=None,
        shutdown=MagicMock(),
    )
    with patch('services.Services', return_value=fake_services), \
            patch('main.atexit.register') as register:
        from main import create_app
        create_app()

    register.assert_called_once_with(fake_services.shutdown)


def test_relay_de_energizes_when_expiry_is_swept(user_manager, mock_network, settings):
    relay = FakeRelay()
    svc = make_service(user_manager, mock_network, settings, relay=relay)
    svc.claim(MAC)
    assert relay.active is True

    svc._claim['expires'] = 0  # force expiry
    svc._expire_claim_if_due()  # what the _run() poll loop calls each tick
    assert relay.active is False
    assert svc.status(MAC) == {'active': False}


def test_relay_de_energizes_on_stop(user_manager, mock_network, settings):
    relay = FakeRelay()
    svc = make_service(user_manager, mock_network, settings, relay=relay)
    svc.claim(MAC)
    assert relay.active is True

    svc.start()
    svc.stop()
    assert relay.active is False


def test_stop_de_energizes_relay_before_waiting_for_thread(
        user_manager, mock_network, settings):
    relay = FakeRelay()
    svc = make_service(user_manager, mock_network, settings, relay=relay)
    relay.active = True
    svc._relay_on = True
    svc.thread = MagicMock()

    def assert_relay_off(timeout):
        assert timeout == 3
        assert relay.active is False

    svc.thread.join.side_effect = assert_relay_off

    svc.stop()

    assert relay.active is False


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


def test_wired_gateway_reports_reachable_device_without_lease(settings):
    """A device reachable on the LAN must be reported even with no DHCP lease
    (expired lease or static IP), else it is neither metered nor shown."""
    settings.network_mode = 'wired'
    gw = WiredGateway(settings)
    neigh_output = f"192.168.4.50 lladdr {MAC.lower()} REACHABLE\n"
    with patch.object(gw, 'get_dhcp_leases', return_value={}), \
         patch('network.wired.run_cmd', return_value=neigh_output):
        stations = gw.get_stations()
    assert [s['mac_address'] for s in stations] == [MAC]
    assert stations[0]['ip'] == '192.168.4.50'
    assert stations[0]['hostname'] == 'Unknown'


def test_wired_gateway_skips_nmcli_when_it_is_not_installed(settings):
    gateway = WiredGateway(settings)
    with patch('network.wired.command_exists', return_value=False), \
            patch('network.wired.run_cmd') as run:
        gateway.start()
        gateway.stop()

    assert all(call.args[0][0] != 'nmcli' for call in run.call_args_list)


def test_facade_selects_wired_backend(settings):
    from network_controller import NetworkController
    settings.network_mode = 'wired'
    nc = NetworkController(settings, manage_hardware=False)
    assert isinstance(nc.ap, WiredGateway)
