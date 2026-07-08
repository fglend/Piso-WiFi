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
