from unittest.mock import MagicMock

from time_manager import TimeManager
from tests.conftest import MAC


def make_tm(user_manager, mock_network, settings):
    return TimeManager(user_manager, mock_network, settings)


def connect(mock_network, *macs):
    mock_network.get_connected_devices.return_value = [
        {'mac_address': m, 'ip': '192.168.4.2', 'hostname': 'x', 'connected': True}
        for m in macs
    ]


def test_first_sighting_starts_clock_without_charging(user_manager, mock_network, settings):
    tm = make_tm(user_manager, mock_network, settings)
    user_manager.add_time(MAC, 5, 25)
    tm._process_device(MAC, now=1000.0)
    assert user_manager.check_balance(MAC) == 25
    assert user_manager.get_last_deduction(MAC) == 1000.0


def test_deducts_exact_elapsed_time(user_manager, mock_network, settings):
    tm = make_tm(user_manager, mock_network, settings)
    user_manager.add_time(MAC, 5, 25)
    user_manager.set_last_deduction(MAC, 1000.0)
    # 90 seconds later -> 1.5 minutes charged, not truncated to 1
    tm._process_device(MAC, now=1090.0)
    assert user_manager.check_balance(MAC) == 23.5
    assert user_manager.get_last_deduction(MAC) == 1090.0


def test_no_charge_under_a_minute(user_manager, mock_network, settings):
    tm = make_tm(user_manager, mock_network, settings)
    user_manager.add_time(MAC, 5, 25)
    user_manager.set_last_deduction(MAC, 1000.0)
    tm._process_device(MAC, now=1030.0)
    assert user_manager.check_balance(MAC) == 25
    # clock not advanced, so the 30s still counts toward the next minute
    assert user_manager.get_last_deduction(MAC) == 1000.0


def test_positive_balance_self_heals_stale_firewall_block(
        user_manager, mock_network, settings):
    tm = make_tm(user_manager, mock_network, settings)
    user_manager.add_time(MAC, 5, 25)
    mock_network.is_access_allowed.return_value = False

    tm._process_device(MAC, now=1000.0)

    mock_network.unblock_mac.assert_called_once_with(MAC)
    mock_network.set_bandwidth_limit.assert_called_once()


def test_blocks_on_depletion(user_manager, mock_network, settings):
    tm = make_tm(user_manager, mock_network, settings)
    user_manager.add_time(MAC, 1, 1)
    user_manager.set_last_deduction(MAC, 1000.0)
    tm._process_device(MAC, now=1120.0)  # 2 minutes elapsed, balance 1
    assert user_manager.check_balance(MAC) == 0
    mock_network.block_mac.assert_called_with(MAC)
    assert user_manager.get_last_deduction(MAC) is None


def test_blocks_zero_balance_device(user_manager, mock_network, settings):
    tm = make_tm(user_manager, mock_network, settings)
    user_manager.add_time(MAC, 1, 5)
    user_manager.deduct_time(MAC, 5)
    tm._process_device(MAC, now=1000.0)
    mock_network.block_mac.assert_called_with(MAC)


def test_pause_on_disconnect_clears_clock(user_manager, mock_network, settings):
    tm = make_tm(user_manager, mock_network, settings)
    user_manager.add_time(MAC, 5, 25)
    user_manager.set_last_deduction(MAC, 1000.0)
    connect(mock_network)  # device gone
    tm._check_and_deduct_time()
    assert user_manager.get_last_deduction(MAC) is None
    assert user_manager.check_balance(MAC) == 25


def test_reset_session_clocks_on_start(user_manager, mock_network, settings):
    tm = make_tm(user_manager, mock_network, settings)
    user_manager.add_time(MAC, 5, 25)
    user_manager.set_last_deduction(MAC, 1.0)  # ancient clock from before restart
    connect(mock_network, MAC)
    tm._reset_session_clocks()
    # clock restarted to "now", so downtime is not billed
    assert user_manager.get_last_deduction(MAC) > 1.0
    assert user_manager.check_balance(MAC) == 25


def test_stop_uses_bounded_wait(user_manager, mock_network, settings):
    tm = make_tm(user_manager, mock_network, settings)
    tm.thread = MagicMock()

    tm.stop()

    tm.thread.join.assert_called_once_with(timeout=3)
