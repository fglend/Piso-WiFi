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


def test_offline_device_keeps_burning_time_when_continuation_on(
        user_manager, mock_network, settings):
    """Elapsed-time mode: a monthly customer's balance runs down while away."""
    settings.pause_on_disconnect = False
    tm = make_tm(user_manager, mock_network, settings)
    user_manager.add_time(MAC, 5, 25)
    user_manager.set_last_deduction(MAC, 1000.0)
    connect(mock_network)  # nobody associated

    tm._meter_offline_devices(set(), 1000.0 + 600)  # 10 minutes later
    assert user_manager.check_balance(MAC) == 15
    # An absent device is never unblocked or shaped - resolving its IP fails
    mock_network.unblock_mac.assert_not_called()
    mock_network.set_bandwidth_limit.assert_not_called()


def test_duration_pass_is_not_metered_by_elapsed_time(
        user_manager, mock_network, settings):
    """A dated pass must not also be charged per connected minute."""
    tm = make_tm(user_manager, mock_network, settings)
    code = user_manager.create_voucher(10 * 1440, duration_days=10)
    user_manager.redeem_voucher(code, MAC)
    tm._sync_duration_passes(1000.0)
    before = user_manager.check_balance(MAC)

    user_manager.set_last_deduction(MAC, 1000.0)
    tm._process_device(MAC, now=1000.0 + 3600)   # an hour connected
    assert user_manager.check_balance(MAC) == before


def test_expired_pass_is_blocked_by_the_meter(user_manager, mock_network, settings):
    tm = make_tm(user_manager, mock_network, settings)
    code = user_manager.create_voucher(1440, duration_days=1)
    user_manager.redeem_voucher(code, MAC)
    conn = user_manager._connect()
    try:
        conn.execute("UPDATE users SET expires_at = datetime('now', '-1 hour')")
        conn.commit()
    finally:
        conn.close()

    tm._sync_duration_passes(1000.0)
    mock_network.block_mac.assert_called_with(MAC)
    assert user_manager.check_balance(MAC) == 0


def test_duration_sync_is_throttled(user_manager, mock_network, settings):
    tm = make_tm(user_manager, mock_network, settings)
    tm.user_manager = MagicMock()
    tm.user_manager.sync_expiring_devices.return_value = ([], [])

    tm._sync_duration_passes(1000.0)
    tm._sync_duration_passes(1030.0)
    assert tm.user_manager.sync_expiring_devices.call_count == 1
    tm._sync_duration_passes(1000.0 + tm.OFFLINE_SWEEP_SECONDS)
    assert tm.user_manager.sync_expiring_devices.call_count == 2


def test_manual_pause_beats_continuation(user_manager, mock_network, settings):
    """Paused before leaving: the clock must stay frozen while away, even
    though continuation is metering every other absent device."""
    settings.pause_on_disconnect = False
    tm = make_tm(user_manager, mock_network, settings)
    user_manager.add_time(MAC, 5, 25)
    user_manager.set_last_deduction(MAC, 1000.0)
    user_manager.set_paused(MAC, True)

    tm._meter_offline_devices(set(), 1000.0 + 7200)  # two hours away
    assert user_manager.check_balance(MAC) == 25

    # ...and resuming restarts the clock from now, with no back-charge
    user_manager.set_paused(MAC, False)
    user_manager.set_last_deduction(MAC, 8200.0)
    tm._next_offline_sweep_at = 0.0
    tm._meter_offline_devices(set(), 8200.0 + 120)
    assert user_manager.check_balance(MAC) == 23


def test_offline_device_blocked_when_balance_runs_out(
        user_manager, mock_network, settings):
    settings.pause_on_disconnect = False
    tm = make_tm(user_manager, mock_network, settings)
    user_manager.add_time(MAC, 1, 5)
    user_manager.set_last_deduction(MAC, 1000.0)

    tm._meter_offline_devices(set(), 1000.0 + 3600)
    assert user_manager.check_balance(MAC) == 0
    mock_network.block_mac.assert_called_with(MAC)


def test_offline_sweep_throttled_to_a_minute(user_manager, mock_network, settings):
    settings.pause_on_disconnect = False
    tm = make_tm(user_manager, mock_network, settings)
    user_manager.add_time(MAC, 5, 25)
    user_manager.set_last_deduction(MAC, 1000.0)

    tm._meter_offline_devices(set(), 1000.0 + 120)   # charges 2 minutes
    tm._meter_offline_devices(set(), 1000.0 + 150)   # inside the window: skipped
    assert user_manager.check_balance(MAC) == 23


def test_connected_device_unaffected_by_continuation(
        user_manager, mock_network, settings):
    """A device that is present must not be charged twice per pass."""
    settings.pause_on_disconnect = False
    tm = make_tm(user_manager, mock_network, settings)
    user_manager.add_time(MAC, 5, 25)
    user_manager.set_last_deduction(MAC, 1000.0)

    tm._meter_offline_devices({MAC}, 1000.0 + 600)
    assert user_manager.check_balance(MAC) == 25


def test_pause_setting_is_read_live(user_manager, mock_network, settings):
    """Flipping the admin toggle must not need a service restart."""
    settings.pause_on_disconnect = True
    tm = make_tm(user_manager, mock_network, settings)
    assert tm.pause_on_disconnect is True
    settings.pause_on_disconnect = False
    assert tm.pause_on_disconnect is False


def test_purge_runs_once_per_hour(user_manager, mock_network, settings):
    tm = make_tm(user_manager, mock_network, settings)
    tm.user_manager = MagicMock()
    tm.user_manager.purge_stale_devices.return_value = 0

    tm._purge_stale_devices()
    tm._purge_stale_devices()
    assert tm.user_manager.purge_stale_devices.call_count == 1
    tm.user_manager.purge_stale_devices.assert_called_with(
        settings.device_retention_hours)

    tm._next_purge_at = 0.0
    tm._purge_stale_devices()
    assert tm.user_manager.purge_stale_devices.call_count == 2


def test_purge_skipped_when_retention_disabled(user_manager, mock_network, settings):
    tm = make_tm(user_manager, mock_network, settings)
    tm.device_retention_hours = 0
    tm.user_manager = MagicMock()

    tm._purge_stale_devices()
    tm.user_manager.purge_stale_devices.assert_not_called()


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


def test_zero_balance_blocks_once_not_every_poll(user_manager, mock_network, settings):
    tm = make_tm(user_manager, mock_network, settings)
    connect(mock_network, MAC)
    # First poll: device is still allowed -> block and log the transition
    mock_network.is_access_allowed.return_value = True
    tm._check_and_deduct_time()
    assert mock_network.block_mac.call_count == 1
    # Subsequent polls: already blocked -> no repeated block/log churn
    mock_network.is_access_allowed.return_value = False
    tm._check_and_deduct_time()
    tm._check_and_deduct_time()
    assert mock_network.block_mac.call_count == 1


def test_deducted_balance_has_no_float_dust(user_manager, mock_network, settings):
    user_manager.add_time(MAC, 5, 48.57)
    user_manager.deduct_time(MAC, 1.03)
    balance = user_manager.check_balance(MAC)
    assert balance == round(balance, 2)
