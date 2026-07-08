from tests.conftest import MAC


def post(client, url, token, **data):
    return client.post(url, data={'csrf_token': token, **data})


# --- auth ---------------------------------------------------------------

def test_admin_routes_require_login(client, csrf_token):
    for url in ('/add_time', '/deduct_time', '/set_bandwidth', '/manage_plan'):
        resp = post(client, url, csrf_token, mac_address=MAC, amount=5,
                    minutes=5, download=1024, upload=512, plan='premium')
        # redirected away, not executed
        assert resp.status_code == 302

    assert client.get('/admin').status_code == 302
    assert client.get('/vouchers').status_code == 302
    assert client.get('/debug/connections').status_code == 302


def test_anonymous_cannot_add_time(client, csrf_token, services):
    post(client, '/add_time', csrf_token, mac_address=MAC, amount=100)
    assert services.user_manager.check_balance(MAC) == 0


def test_login_logout(client, csrf_token, services):
    resp = post(client, '/login', csrf_token,
                username=services.settings.admin_username,
                password=services.settings.admin_password)
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert sess.get('is_admin') is True

    client.get('/logout')
    with client.session_transaction() as sess:
        assert not sess.get('is_admin')


def test_login_rejects_bad_credentials(client, csrf_token):
    post(client, '/login', csrf_token, username='admin', password='wrong')
    with client.session_transaction() as sess:
        assert not sess.get('is_admin')


# --- CSRF ---------------------------------------------------------------

def test_post_without_csrf_rejected(admin_client):
    resp = admin_client.post('/add_time', data={'mac_address': MAC, 'amount': 5})
    assert resp.status_code == 400


# --- admin actions --------------------------------------------------------

def test_add_time_uses_configured_rate(admin_client, csrf_token, services):
    resp = post(admin_client, '/add_time', csrf_token, mac_address=MAC, amount=2)
    assert resp.status_code == 302
    # 2 pesos * 5 minutes/peso
    assert services.user_manager.check_balance(MAC) == 10
    services.network_controller.unblock_mac.assert_called_with(MAC)


def test_add_time_rejects_invalid_mac(admin_client, csrf_token, services):
    post(admin_client, '/add_time', csrf_token,
         mac_address='00:11; rm -rf /', amount=2)
    services.network_controller.unblock_mac.assert_not_called()
    assert services.user_manager.get_transactions() == []


def test_deduct_time_blocks_at_zero(admin_client, csrf_token, services):
    services.user_manager.add_time(MAC, 1, 5)
    post(admin_client, '/deduct_time', csrf_token, mac_address=MAC, minutes=10)
    assert services.user_manager.check_balance(MAC) == 0
    services.network_controller.block_mac.assert_called_with(MAC)


def test_set_bandwidth_validates_range(admin_client, csrf_token, services):
    services.user_manager.add_time(MAC, 1, 5)
    post(admin_client, '/set_bandwidth', csrf_token,
         mac_address=MAC, download=8, upload=512)
    services.network_controller.set_bandwidth_limit.assert_not_called()


def test_manage_plan(admin_client, csrf_token, services):
    services.user_manager.add_time(MAC, 1, 5)
    post(admin_client, '/manage_plan', csrf_token, mac_address=MAC, plan='premium')
    assert services.user_manager.get_device_info(MAC)['plan'] == 'premium'
    services.network_controller.set_bandwidth_limit.assert_called()


# --- portal ---------------------------------------------------------------

def test_portal_shows_own_device(client, services):
    services.user_manager.add_time(MAC, 5, 25)
    resp = client.get('/')
    assert resp.status_code == 200
    assert MAC.encode() in resp.data


def test_redeem_voucher_via_portal(client, csrf_token, services):
    code = services.user_manager.create_voucher(15)
    resp = post(client, '/redeem', csrf_token, code=code)
    assert resp.status_code == 302
    assert services.user_manager.check_balance(MAC) == 15
    services.network_controller.unblock_mac.assert_called_with(MAC)


def test_redeem_invalid_voucher(client, csrf_token, services):
    post(client, '/redeem', csrf_token, code='NOPE-NOPE')
    assert services.user_manager.check_balance(MAC) == 0


def test_request_upgrade_uses_requester_mac(client, csrf_token, services):
    services.user_manager.add_time(MAC, 5, 25)
    post(client, '/request_upgrade', csrf_token)
    assert services.user_manager.get_device_info(MAC)['upgrade_requested'] == 1
