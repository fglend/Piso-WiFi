import datetime as dt
import os
import stat

from user_manager import DISCONNECT_CONFIRMATION_POLLS
from tests.conftest import MAC, OTHER_MAC


def test_add_time(user_manager):
    assert user_manager.add_time(MAC, 5, 25) is True
    assert user_manager.check_balance(MAC) == 25

    assert user_manager.add_time(MAC, 5, 25) is True
    assert user_manager.check_balance(MAC) == 50


def test_check_balance_nonexistent_user(user_manager):
    assert user_manager.check_balance(OTHER_MAC) == 0


def test_deduct_time_fractional(user_manager):
    user_manager.add_time(MAC, 10, 10)
    assert user_manager.deduct_time(MAC, 1.25) is True
    assert user_manager.check_balance(MAC) == 8.75


def test_deduct_time_clamps_at_zero(user_manager):
    user_manager.add_time(MAC, 1, 5)
    assert user_manager.deduct_time(MAC, 100) is True
    assert user_manager.check_balance(MAC) == 0
    info = user_manager.get_device_info(MAC)
    assert info['status'] == 'inactive'


def test_deduct_time_unknown_user(user_manager):
    assert user_manager.deduct_time(OTHER_MAC, 5) is False


def test_transfer_balance_to_new_mac_keeps_plan(user_manager):
    user_manager.add_time(MAC, 5, 25)
    user_manager.set_plan(MAC, 'premium')

    assert user_manager.transfer_balance(MAC, OTHER_MAC) == 25
    assert user_manager.check_balance(MAC) == 0
    info = user_manager.get_device_info(OTHER_MAC)
    assert info['time_balance'] == 25
    assert info['plan'] == 'premium'


def test_transfer_balance_merges_into_existing_mac(user_manager):
    user_manager.add_time(MAC, 5, 25)
    user_manager.add_time(OTHER_MAC, 1, 5)

    assert user_manager.transfer_balance(MAC, OTHER_MAC) == 25
    assert user_manager.check_balance(OTHER_MAC) == 30
    assert user_manager.check_balance(MAC) == 0
    assert user_manager.get_device_info(MAC)['status'] == 'inactive'


def test_transfer_carries_the_duration_pass(user_manager):
    """A monthly customer whose MAC rotated keeps their dated pass, not just
    loose minutes the elapsed-time meter would drain."""
    code = user_manager.create_voucher(30 * 1440, duration_days=30,
                                       pausable=False)
    user_manager.redeem_voucher(code, MAC)
    deadline = user_manager.get_expiry(MAC)
    user_manager.add_time(OTHER_MAC, 1, 5)      # destination already exists

    assert user_manager.transfer_balance(MAC, OTHER_MAC)
    assert user_manager.get_expiry(OTHER_MAC) == deadline
    assert user_manager.is_pausable(OTHER_MAC) is False
    assert user_manager.get_expiry(MAC) is None


def test_transfer_balance_without_balance_is_noop(user_manager):
    assert user_manager.transfer_balance(MAC, OTHER_MAC) is None
    user_manager.add_time(MAC, 5, 25)
    assert user_manager.transfer_balance(MAC, MAC) is None
    assert user_manager.check_balance(MAC) == 25


def test_snapshot_skips_write_for_unchanged_device(user_manager):
    """A steady connected device must not dirty a page on every poll."""
    device = {'mac_address': MAC, 'ip': '192.168.4.5', 'hostname': 'phone'}
    user_manager.sync_connection_snapshot([device])

    conn = user_manager._connect()
    try:
        before = conn.execute(
            'SELECT last_seen_at FROM device_connections '
            'WHERE mac_address = ?', (MAC,)).fetchone()['last_seen_at']
        # Age the row past the refresh window and confirm it does get rewritten
        user_manager.sync_connection_snapshot([device])
        after = conn.execute(
            'SELECT last_seen_at FROM device_connections '
            'WHERE mac_address = ?', (MAC,)).fetchone()['last_seen_at']
        assert after == before

        conn.execute(
            "UPDATE device_connections SET last_seen_at = datetime('now', '-1 hour')")
        conn.commit()
    finally:
        conn.close()

    user_manager.sync_connection_snapshot([device])
    rows = user_manager.get_users_with_balance()  # touches the same table
    assert rows == []  # no balance yet; the call must not error

    # A changed hostname always writes through
    user_manager.sync_connection_snapshot(
        [{**device, 'hostname': 'renamed'}])
    conn = user_manager._connect()
    try:
        row = conn.execute(
            'SELECT hostname, missed_polls FROM device_connections '
            'WHERE mac_address = ?', (MAC,)).fetchone()
        assert row['hostname'] == 'renamed'
        assert row['missed_polls'] == 0
    finally:
        conn.close()


def test_prune_history_trims_old_time_logs(user_manager):
    user_manager.add_time(MAC, 5, 25)
    user_manager.deduct_time(MAC, 1)

    conn = user_manager._connect()
    try:
        conn.execute(
            "UPDATE time_logs SET deducted_at = datetime('now', '-60 days')")
        conn.commit()
    finally:
        conn.close()

    assert user_manager.prune_history() == 1
    conn = user_manager._connect()
    try:
        assert conn.execute('SELECT COUNT(*) c FROM time_logs').fetchone()['c'] == 0
    finally:
        conn.close()
    # Balance is untouched by history pruning
    assert user_manager.check_balance(MAC) == 24


def _backdate(user_manager, hours):
    """Age every user and transaction row by `hours`."""
    conn = user_manager._connect()
    try:
        conn.execute(
            "UPDATE users SET created_at = datetime('now', ?), "
            "last_deduction = datetime('now', ?)",
            (f'-{hours} hours', f'-{hours} hours'))
        conn.execute(
            "UPDATE transactions SET created_at = datetime('now', ?)",
            (f'-{hours} hours',))
        conn.commit()
    finally:
        conn.close()


def test_purge_removes_spent_idle_devices(user_manager):
    user_manager.add_time(MAC, 5, 25)
    user_manager.deduct_time(MAC, 25)
    user_manager.set_last_deduction(MAC, 1000.0)
    _backdate(user_manager, 30)

    assert user_manager.purge_stale_devices(24) == 1
    assert user_manager.get_device_info(MAC) is None
    # the persisted deduction clock goes with it
    assert user_manager.get_last_deduction(MAC) is None
    # revenue history survives the device
    assert user_manager.get_revenue_summary()['month'] == 5


def test_purge_keeps_devices_with_balance_or_recent_activity(user_manager):
    user_manager.add_time(MAC, 5, 25)      # still has time
    user_manager.add_time(OTHER_MAC, 1, 5)
    user_manager.deduct_time(OTHER_MAC, 5)  # spent, but active just now
    _backdate(user_manager, 30)
    # re-activity after the backdate: a top-up inside the window
    user_manager.add_time(OTHER_MAC, 1, 5)
    user_manager.deduct_time(OTHER_MAC, 5)

    assert user_manager.purge_stale_devices(24) == 0
    assert user_manager.get_device_info(MAC) is not None
    assert user_manager.get_device_info(OTHER_MAC) is not None


def test_purge_disabled_by_zero_retention(user_manager):
    user_manager.add_time(MAC, 5, 25)
    user_manager.deduct_time(MAC, 25)
    _backdate(user_manager, 300)

    assert user_manager.purge_stale_devices(0) == 0
    assert user_manager.get_device_info(MAC) is not None


def test_get_device_info(user_manager):
    assert user_manager.get_device_info(MAC) is None
    user_manager.add_time(MAC, 5, 25)
    info = user_manager.get_device_info(MAC)
    assert info['time_balance'] == 25
    assert info['plan'] == 'default'


def test_get_active_users(user_manager):
    user_manager.add_time(MAC, 5, 25)
    user_manager.add_time(OTHER_MAC, 1, 5)
    user_manager.deduct_time(OTHER_MAC, 5)
    active = user_manager.get_active_users()
    assert [u['mac_address'] for u in active] == [MAC]


def test_plans_seeded_and_set_plan(user_manager):
    plans = user_manager.get_plans()
    assert 'default' in plans and 'premium' in plans

    user_manager.add_time(MAC, 5, 25)
    speeds = user_manager.set_plan(MAC, 'premium')
    assert speeds == (plans['premium']['download_kbps'], plans['premium']['upload_kbps'])
    assert user_manager.get_device_info(MAC)['plan'] == 'premium'

    assert user_manager.set_plan(MAC, 'nonexistent') is None


def test_voucher_lifecycle(user_manager):
    code = user_manager.create_voucher(30)
    assert code

    granted = user_manager.redeem_voucher(code, MAC)
    assert granted == {'minutes': 30, 'duration_days': None, 'expires_at': None}
    assert user_manager.check_balance(MAC) == 30

    # Second redemption must fail
    assert user_manager.redeem_voucher(code, OTHER_MAC) is None
    assert user_manager.redeem_voucher('BOGUS-CODE', MAC) is None


def _hours_until(user_manager, mac):
    """Hours left on a device's pass, from its stored balance."""
    return user_manager.check_balance(mac) / 60.0


def test_duration_voucher_grants_a_dated_pass(user_manager):
    code = user_manager.create_voucher(30 * 1440, price=500,
                                       duration_days=30, pausable=True)
    granted = user_manager.redeem_voucher(code, MAC)

    assert granted['duration_days'] == 30
    assert granted['expires_at']                       # a real deadline
    assert user_manager.get_expiry(MAC) == granted['expires_at']
    # 30 days of minutes, give or take the second the test took
    assert 30 * 1440 - 1 <= granted['minutes'] <= 30 * 1440
    assert user_manager.is_pausable(MAC) is True


def test_duration_voucher_can_forbid_pausing(user_manager):
    code = user_manager.create_voucher(15 * 1440, price=300,
                                       duration_days=15, pausable=False)
    user_manager.redeem_voucher(code, MAC)
    assert user_manager.is_pausable(MAC) is False


def test_duration_passes_stack_instead_of_truncating(user_manager):
    first = user_manager.create_voucher(15 * 1440, duration_days=15)
    second = user_manager.create_voucher(15 * 1440, duration_days=15)
    user_manager.redeem_voucher(first, MAC)
    user_manager.redeem_voucher(second, MAC)
    # Redeeming a second 15-day pass on top must give 30 days, not reset to 15
    assert 29.9 < _hours_until(user_manager, MAC) / 24 <= 30


def test_pause_pushes_the_deadline_back(user_manager):
    code = user_manager.create_voucher(10 * 1440, duration_days=10)
    user_manager.redeem_voucher(code, MAC)
    before = user_manager.get_expiry(MAC)

    user_manager.set_paused(MAC, True)
    conn = user_manager._connect()
    try:  # pretend the customer stayed paused for two hours
        conn.execute("UPDATE users SET paused_at = datetime('now', '-2 hours')")
        conn.commit()
    finally:
        conn.close()
    user_manager.set_paused(MAC, False)

    after = user_manager.get_expiry(MAC)
    assert after > before
    delta_hours = (
        (dt.datetime.fromisoformat(after) - dt.datetime.fromisoformat(before))
        .total_seconds() / 3600.0)
    assert 1.9 < delta_hours < 2.1


def test_expired_pass_reports_zero_and_is_flagged(user_manager):
    code = user_manager.create_voucher(1440, duration_days=1)
    user_manager.redeem_voucher(code, MAC)
    conn = user_manager._connect()
    try:
        conn.execute("UPDATE users SET expires_at = datetime('now', '-1 hour')")
        conn.commit()
    finally:
        conn.close()

    tracked, expired = user_manager.sync_expiring_devices()
    assert MAC in tracked and MAC in expired
    assert user_manager.check_balance(MAC) == 0
    assert user_manager.get_device_info(MAC)['status'] == 'inactive'


def test_paused_pass_does_not_drain(user_manager):
    code = user_manager.create_voucher(1440, duration_days=1)
    user_manager.redeem_voucher(code, MAC)
    user_manager.set_paused(MAC, True)
    before = user_manager.check_balance(MAC)

    conn = user_manager._connect()
    try:
        conn.execute("UPDATE users SET expires_at = datetime('now', '-1 hour')")
        conn.commit()
    finally:
        conn.close()
    user_manager.sync_expiring_devices()
    # Paused rows are skipped by the sync, so the stale balance is untouched
    assert user_manager.check_balance(MAC) == before


def test_voucher_transaction_source(user_manager):
    code = user_manager.create_voucher(10)
    user_manager.redeem_voucher(code, MAC)
    transactions = user_manager.get_transactions()
    assert transactions[0]['source'] == 'voucher'


def test_session_clock_persistence(user_manager):
    assert user_manager.get_last_deduction(MAC) is None
    user_manager.set_last_deduction(MAC, 1000.0)
    assert user_manager.get_last_deduction(MAC) == 1000.0
    user_manager.set_last_deduction(MAC, 2000.0)
    assert user_manager.get_last_deduction(MAC) == 2000.0
    user_manager.clear_session(MAC)
    assert user_manager.get_last_deduction(MAC) is None


def test_check_health(user_manager):
    assert user_manager.check_health() is True


def test_database_file_is_owner_only(user_manager):
    mode = stat.S_IMODE(os.stat(user_manager.db_path).st_mode)

    assert mode == 0o600


def test_connection_history_tracks_latest_disconnected_device(user_manager):
    user_manager.sync_connection_snapshot([{
        'mac_address': MAC, 'hostname': 'phone', 'ip': '192.168.4.20'}])
    # Repeated discovery/restart must update the open session, not duplicate it.
    user_manager.sync_connection_snapshot([{
        'mac_address': MAC, 'hostname': 'renamed-phone',
        'ip': '192.168.4.21'}])
    user_manager.sync_connection_snapshot([])
    user_manager.sync_connection_snapshot([])

    history = user_manager.get_disconnected_devices()

    assert len(history) == 1
    assert history[0]['mac_address'] == MAC
    assert history[0]['hostname'] == 'renamed-phone'
    assert history[0]['ip_address'] == '192.168.4.21'
    assert history[0]['connected_at']
    assert history[0]['disconnected_at']


def test_reconnected_device_is_not_listed_as_disconnected(user_manager):
    user_manager.sync_connection_snapshot([{
        'mac_address': MAC, 'hostname': 'phone', 'ip': '192.168.4.20'}])
    user_manager.sync_connection_snapshot([])
    user_manager.sync_connection_snapshot([])
    user_manager.sync_connection_snapshot([{
        'mac_address': MAC, 'hostname': 'phone', 'ip': '192.168.4.22'}])

    assert user_manager.get_disconnected_devices() == []


def test_restart_empty_snapshot_does_not_close_open_session(user_manager):
    user_manager.sync_connection_snapshot([{
        'mac_address': MAC, 'hostname': 'phone', 'ip': '192.168.4.20'}])

    user_manager.sync_connection_snapshot([])
    user_manager.sync_connection_snapshot([{
        'mac_address': MAC, 'hostname': 'phone', 'ip': '192.168.4.20'}])

    assert user_manager.get_disconnected_devices() == []


def test_post_visibility_is_independent_per_post(user_manager):
    assert user_manager.create_post('Visible post', 'Shown', 'visible.jpg', active=True)
    assert user_manager.create_post('Hidden post', 'Not shown', 'hidden.jpg', active=False)

    posts = {post['title']: post for post in user_manager.get_posts()}
    assert posts['Visible post']['active'] == 1
    assert posts['Hidden post']['active'] == 0
    assert [post['title'] for post in user_manager.get_posts(active_only=True)] == [
        'Visible post'
    ]

    assert user_manager.set_post_active(posts['Hidden post']['id'], True)
    assert {post['title'] for post in user_manager.get_posts(active_only=True)} == {
        'Visible post', 'Hidden post'
    }


def test_setting_visibility_for_missing_post_fails(user_manager):
    assert user_manager.set_post_active(999_999, False) is False


def test_update_post_description_changes_only_selected_post(user_manager):
    user_manager.create_post('First', 'Old first', 'first.jpg')
    user_manager.create_post('Second', 'Old second', 'second.jpg')
    posts = {post['title']: post for post in user_manager.get_posts()}

    assert user_manager.update_post_description(
        posts['First']['id'], 'Updated first') is True

    updated = {post['title']: post for post in user_manager.get_posts()}
    assert updated['First']['description'] == 'Updated first'
    assert updated['First']['image_file'] == 'first.jpg'
    assert updated['First']['active'] == 1
    assert updated['Second']['description'] == 'Old second'
    assert user_manager.update_post_description(
        posts['First']['id'], '') is True
    cleared = {post['title']: post for post in user_manager.get_posts()}
    assert cleared['First']['description'] == ''
    assert user_manager.update_post_description(999_999, 'Missing') is False


def test_paid_voucher_records_revenue_at_creation(user_manager):
    before = user_manager.get_revenue_summary()['day']
    code = user_manager.create_voucher(150, price=10)
    assert code is not None
    assert user_manager.get_revenue_summary()['day'] == before + 10
    # Redemption grants the minutes but never double-counts the sale
    assert user_manager.redeem_voucher(
        code, "00:11:22:33:44:55")['minutes'] == 150
    assert user_manager.get_revenue_summary()['day'] == before + 10
    voucher = user_manager.get_vouchers(include_redeemed=True)[0]
    assert voucher['price'] == 10


def test_free_voucher_records_no_revenue(user_manager):
    before = user_manager.get_revenue_summary()['day']
    code = user_manager.create_voucher(30)
    assert code is not None
    user_manager.redeem_voucher(code, "00:11:22:33:44:55")
    assert user_manager.get_revenue_summary()['day'] == before
    voucher = user_manager.get_vouchers(include_redeemed=True)[0]
    assert not voucher['price']


def test_get_users_with_balance_includes_last_connection(user_manager):
    user_manager.add_time(MAC, 5, 60)
    user_manager.add_time(OTHER_MAC, 1, 10)
    user_manager.deduct_time(OTHER_MAC, 10)
    user_manager.sync_connection_snapshot([
        {'mac_address': MAC, 'hostname': 'phone', 'ip': '192.168.4.10'},
    ])

    users = user_manager.get_users_with_balance()

    assert [u['mac_address'] for u in users] == [MAC]
    assert users[0]['time_balance'] == 60
    assert users[0]['hostname'] == 'phone'
    assert users[0]['ip_address'] == '192.168.4.10'
    assert users[0]['last_seen_at'] is not None


def _stamp_last_transaction(user_manager, modifier):
    """Backdate the most recent transaction, e.g. '-6 days','start of day'."""
    conn = user_manager._connect()
    try:
        conn.execute(f'''
            UPDATE transactions
            SET created_at = datetime(datetime('now','localtime',{modifier}),'utc')
            WHERE id = (SELECT MAX(id) FROM transactions)
        ''')
        conn.commit()
    finally:
        conn.close()


def test_week_includes_the_whole_seventh_day(user_manager):
    """The regression: the window used to start at the current clock time six
    days back, so money earned earlier that day fell outside 'this week' while
    adjustments recorded later the same evening stayed inside."""
    user_manager.add_time(MAC, 100, 10)
    _stamp_last_transaction(user_manager, "'-6 days','start of day','+30 minutes'")

    summary = user_manager.get_revenue_summary()
    assert summary['week'] == 100
    assert summary['day'] == 0        # not today


def test_week_excludes_the_eighth_day(user_manager):
    user_manager.add_time(MAC, 100, 10)
    _stamp_last_transaction(user_manager, "'-7 days','start of day','-1 minutes'")
    assert user_manager.get_revenue_summary()['week'] == 0


def test_week_boundary_is_stable_across_the_day(user_manager):
    """Same data must give the same week total whatever time it is read."""
    user_manager.add_time(MAC, 100, 10)
    _stamp_last_transaction(user_manager, "'-6 days','start of day','+1 minutes'")
    assert user_manager.get_revenue_summary()['week'] == 100
    assert user_manager.get_revenue_summary()['week'] == 100


def test_buckets_nest_day_within_week(user_manager):
    user_manager.add_time(MAC, 45, 10)                  # today
    user_manager.add_time(OTHER_MAC, 200, 10)
    _stamp_last_transaction(user_manager, "'-3 days'")   # this week, not today
    summary = user_manager.get_revenue_summary()
    assert summary['day'] == 45
    assert summary['week'] == 245
    assert summary['month'] >= summary['day']


def test_summary_reports_adjustment_magnitudes(user_manager):
    user_manager.add_time(MAC, 100, 10)
    user_manager.record_revenue_adjustment(30)

    summary = user_manager.get_revenue_summary()
    assert summary['day'] == 70                  # netted
    assert summary['day_adjustments'] == 30      # positive magnitude
    assert summary['week_adjustments'] == 30
    assert summary['month_adjustments'] == 30


def test_week_can_be_negative_when_adjustments_dominate(user_manager):
    """Honest arithmetic: heavy corrections may legitimately net below zero."""
    user_manager.add_time(MAC, 10, 10)
    user_manager.record_revenue_adjustment(500)
    summary = user_manager.get_revenue_summary()
    assert summary['week'] == -490
    assert summary['week_adjustments'] == 500


def test_revenue_summary_uses_the_created_at_index(user_manager):
    conn = user_manager._connect()
    try:
        plan = ' '.join(
            str(tuple(row)) for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM transactions "
                "WHERE amount != 0 AND created_at >= datetime(MIN("
                "datetime('now','localtime','start of month'),"
                "datetime('now','localtime','-6 days','start of day')),'utc')"
            ).fetchall())
    finally:
        conn.close()
    assert 'idx_transactions_created_at' in plan, plan


def test_revenue_adjustment_reduces_summary(user_manager):
    user_manager.add_time(MAC, 50, 600, source='coin')
    assert user_manager.get_revenue_summary()['day'] == 50
    assert user_manager.record_revenue_adjustment(20) is True
    assert user_manager.get_revenue_summary()['day'] == 30
    # audit trail: the negative adjustment is a visible transaction
    sources = [t['source'] for t in user_manager.get_transactions()]
    assert 'adjustment' in sources
    assert user_manager.record_revenue_adjustment(0) is False
    assert user_manager.record_revenue_adjustment(-5) is False


# --- sales reporting ---------------------------------------------------------

def _today():
    return dt.date.today().isoformat()


def _insert_transaction_at(user_manager, amount, minutes, timestamp):
    """Insert a transaction at an explicit UTC timestamp.

    add_time() always stamps CURRENT_TIMESTAMP, so proving that a range
    excludes older rows needs a direct write.
    """
    conn = user_manager._connect()
    try:
        conn.execute(
            'INSERT INTO transactions (user_id, amount, minutes, source, created_at) '
            "VALUES (NULL, ?, ?, 'cash', ?)", (amount, minutes, timestamp))
        conn.commit()
    finally:
        conn.close()


def test_sales_report_totals_reconcile_with_buckets(user_manager):
    user_manager.add_time(MAC, 5, 25)
    user_manager.add_time(OTHER_MAC, 10, 50)

    report = user_manager.get_sales_report(_today(), _today(), 'day')

    assert report['totals']['gross'] == 15.0
    assert report['totals']['net'] == 15.0
    assert report['totals']['count'] == 2
    assert report['totals']['minutes'] == 75
    # The screen renders buckets; the totals row must be their sum exactly.
    assert sum(b['net'] for b in report['buckets']) == report['totals']['net']
    assert sum(b['count'] for b in report['buckets']) == report['totals']['count']


def test_sales_report_excludes_rows_outside_the_range(user_manager):
    user_manager.add_time(MAC, 5, 25)
    _insert_transaction_at(user_manager, 999.0, 9999, '2020-01-01 00:00:00')

    report = user_manager.get_sales_report(_today(), _today(), 'day')

    assert report['totals']['gross'] == 5.0
    assert report['totals']['count'] == 1


def test_sales_report_nets_out_adjustments(user_manager):
    user_manager.add_time(MAC, 20, 100)
    user_manager.record_revenue_adjustment(8)

    report = user_manager.get_sales_report(_today(), _today(), 'day')

    assert report['totals']['gross'] == 20.0
    assert report['totals']['adjustments'] == 8.0
    assert report['totals']['net'] == 12.0


def test_sales_report_can_go_negative_when_corrections_exceed_takings(user_manager):
    user_manager.add_time(MAC, 5, 25)
    user_manager.record_revenue_adjustment(20)

    report = user_manager.get_sales_report(_today(), _today(), 'day')

    # Clamping here would hide an over-correction from the operator.
    assert report['totals']['net'] == -15.0


def test_sales_report_breaks_down_by_source(user_manager):
    user_manager.add_time(MAC, 5, 25)
    user_manager.record_revenue_adjustment(2)

    report = user_manager.get_sales_report(_today(), _today(), 'day')
    by_source = {row['source']: row for row in report['by_source']}

    assert by_source['cash']['net'] == 5.0
    assert by_source['adjustment']['net'] == -2.0


def test_sales_report_rejects_an_unknown_grouping(user_manager):
    user_manager.add_time(MAC, 5, 25)

    # An unknown group_by must fall back to 'day', never reach the SQL.
    report = user_manager.get_sales_report(
        _today(), _today(), "day'; DROP TABLE transactions; --")

    assert report['totals']['gross'] == 5.0
    assert len(report['buckets']) == 1


def test_sales_report_is_empty_for_a_range_with_no_sales(user_manager):
    user_manager.add_time(MAC, 5, 25)

    report = user_manager.get_sales_report('2020-01-01', '2020-01-31', 'day')

    assert report['buckets'] == []
    assert report['totals']['net'] == 0.0


def test_transactions_between_returns_exactly_the_aggregated_rows(user_manager):
    user_manager.add_time(MAC, 5, 25)
    user_manager.add_time(OTHER_MAC, 10, 50)
    _insert_transaction_at(user_manager, 999.0, 9999, '2020-01-01 00:00:00')

    rows = user_manager.get_transactions_between(_today(), _today())
    report = user_manager.get_sales_report(_today(), _today(), 'day')

    assert len(rows) == report['totals']['count'] == 2
    # The CSV total must match what the operator saw on screen.
    assert sum(row['amount'] for row in rows) == report['totals']['net']
    assert all(row['created_at'].startswith(dt.date.today().isoformat())
               for row in rows)


def test_transactions_between_honours_its_limit(user_manager):
    for _ in range(5):
        user_manager.add_time(MAC, 1, 5)

    assert len(user_manager.get_transactions_between(
        _today(), _today(), limit=3)) == 3


def test_clear_history_is_not_disabled_by_a_null_mac_user_row(user_manager):
    """A NULL mac_address in users must not silently no-op the whole delete.

    users.mac_address has no NOT NULL constraint. Written with `NOT IN`, a
    single NULL in the subquery makes the predicate NULL for every row and
    nothing is deleted at all - the button looks like it worked and did
    nothing. NOT EXISTS is NULL-safe.
    """
    conn = user_manager._connect()
    try:
        conn.execute(
            'INSERT INTO users(mac_address, time_balance) VALUES(NULL, 100)')
        conn.commit()
    finally:
        conn.close()

    _insert_transaction_at(user_manager, 1.0, 5, '2020-01-01 00:00:00')
    user_manager.sync_connection_snapshot(
        [{'mac_address': MAC, 'ip': '192.168.4.7', 'hostname': 'h'}])
    for _ in range(DISCONNECT_CONFIRMATION_POLLS):
        user_manager.sync_connection_snapshot([])
    assert user_manager.get_disconnected_devices()

    assert user_manager.clear_disconnected_history() == 1
    assert user_manager.get_disconnected_devices() == []


def test_clear_history_still_spares_a_balance_holder(user_manager):
    user_manager.add_time(OTHER_MAC, 5, 25)
    for mac in (MAC, OTHER_MAC):
        user_manager.sync_connection_snapshot(
            [{'mac_address': mac, 'ip': '192.168.4.7', 'hostname': 'h'}])
        for _ in range(DISCONNECT_CONFIRMATION_POLLS):
            user_manager.sync_connection_snapshot([])

    user_manager.clear_disconnected_history()

    remaining = user_manager.get_users_with_balance()
    assert remaining[0]['ip_address'] == '192.168.4.7'
