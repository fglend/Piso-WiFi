import os
import stat

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

    minutes = user_manager.redeem_voucher(code, MAC)
    assert minutes == 30
    assert user_manager.check_balance(MAC) == 30

    # Second redemption must fail
    assert user_manager.redeem_voucher(code, OTHER_MAC) is None
    assert user_manager.redeem_voucher('BOGUS-CODE', MAC) is None


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
    assert user_manager.redeem_voucher(code, "00:11:22:33:44:55") == 150
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
