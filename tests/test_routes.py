from io import BytesIO

from post_formatting import render_post_description
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
    # 2 x the ₱1 tier (10 minutes) from the seeded rate table
    assert services.user_manager.check_balance(MAC) == 20
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


def test_portal_displays_network_speed_in_mbps(client):
    resp = client.get('/')

    assert resp.status_code == 200
    assert b'2.048 Mbps down / 1.024 Mbps up' in resp.data
    assert b'kbps down' not in resp.data


def test_captive_probe_redirects_to_canonical_portal(client, services):
    response = client.get(
        '/generate_204', headers={'Host': 'connectivitycheck.gstatic.com'})

    assert response.status_code == 302
    assert response.location == (
        f'http://glend-pisowifi:{services.settings.port}/')


def test_captive_redirect_never_uses_untrusted_host(client, services):
    response = client.get(
        '/arbitrary/path', headers={'Host': 'attacker.example'})

    assert response.status_code == 302
    assert response.location == (
        f'http://glend-pisowifi:{services.settings.port}/')
    assert 'attacker.example' not in response.location


def test_customer_lan_cannot_open_admin_login(client):
    response = client.get('/login', environ_base={'REMOTE_ADDR': '192.168.4.20'})

    assert response.status_code == 403


def test_unknown_post_is_not_redirected(client):
    response = client.post('/arbitrary/path')

    assert not 300 <= response.status_code < 400


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


def test_settings_page(client, admin_client):
    # anonymous is redirected; admin sees the settings form
    resp = admin_client.get('/admin/settings')
    assert resp.status_code == 200
    assert b'System Settings' in resp.data
    assert b'name="minutes_per_peso"' in resp.data
    assert b'visible_in_portal' not in resp.data


def test_dashboard_has_connected_and_disconnected_device_tabs(
        admin_client, services):
    services.user_manager.sync_connection_snapshot([{
        'mac_address': MAC, 'hostname': 'old-phone', 'ip': '192.168.4.20'}])
    services.user_manager.sync_connection_snapshot([])
    services.user_manager.sync_connection_snapshot([])

    response = admin_client.get('/admin')

    assert response.status_code == 200
    assert b'Connected' in response.data
    assert b'Disconnected' in response.data
    assert b'old-phone' in response.data
    assert MAC.encode() in response.data
    assert b'role="tablist"' in response.data
    assert b'aria-controls="connected-panel"' in response.data
    assert b'id="disconnected-panel"' in response.data
    assert b'device_state_signature' in response.data


# --- carousel posts -------------------------------------------------------


def test_post_description_renderer_supports_safe_basic_formatting():
    rendered = str(render_post_description(
        'First line\nSecond **bold** and *italic*\n\n- One\n- Two\n<script>'))

    assert ('<p>First line<br>Second <strong>bold</strong> and '
            '<em>italic</em></p>') in rendered
    assert '<ul><li>One</li><li>Two</li></ul>' in rendered
    assert '&lt;script&gt;' in rendered
    assert '<script>' not in rendered


def test_post_description_renderer_escapes_html_inside_formatting():
    rendered = str(render_post_description(
        '**<img src=x onerror=alert(1)>**\n'
        '- <svg onload=alert(2)>\n'
        'Unmatched **bold'))

    assert '<img' not in rendered
    assert '<svg' not in rendered
    assert '&lt;img src=x onerror=alert(1)&gt;' in rendered
    assert '&lt;svg onload=alert(2)&gt;' in rendered
    assert 'Unmatched **bold' in rendered


def test_post_description_renderer_preserves_literal_math_asterisks():
    rendered = str(render_post_description('2 * 3 and 4 * 5'))

    assert rendered == '<p>2 * 3 and 4 * 5</p>'


def test_portal_renders_formatted_post_description(client, services):
    services.user_manager.create_post(
        'Formatted post',
        'Welcome\nThis is **bold** and *italic*.\n\n- Fast\n- Safe',
        'formatted.jpg', active=True)

    response = client.get('/')

    assert b'Welcome<br>This is <strong>bold</strong> and <em>italic</em>.' in response.data
    assert b'<ul><li>Fast</li><li>Safe</li></ul>' in response.data


def test_portal_only_renders_posts_marked_visible(client, services):
    services.user_manager.create_post(
        'Visible promotion', 'Shown in the carousel', 'visible.jpg', active=True)
    services.user_manager.create_post(
        'Hidden promotion', 'Kept out of the carousel', 'hidden.jpg', active=False)

    resp = client.get('/')

    assert resp.status_code == 200
    assert b'Visible promotion' in resp.data
    assert b'visible.jpg' in resp.data
    assert b'Hidden promotion' not in resp.data
    assert b'hidden.jpg' not in resp.data


def test_admin_toggles_visibility_for_only_the_selected_post(
        admin_client, csrf_token, services):
    services.user_manager.create_post('First post', '', 'first.jpg', active=True)
    services.user_manager.create_post('Second post', '', 'second.jpg', active=True)
    posts = {item['title']: item for item in services.user_manager.get_posts()}

    resp = post(admin_client, '/admin/posts/toggle', csrf_token,
                post_id=posts['First post']['id'], active=0)

    assert resp.status_code == 302
    states = {item['title']: item['active']
              for item in services.user_manager.get_posts()}
    assert states == {'First post': 0, 'Second post': 1}


def test_posts_admin_page_has_per_post_visibility_controls(
        admin_client, services):
    services.user_manager.create_post('Visible post', '', 'visible.jpg', active=True)
    services.user_manager.create_post('Hidden post', '', 'hidden.jpg', active=False)

    resp = admin_client.get('/admin/posts')

    assert resp.status_code == 200
    assert b'Visible in portal carousel' in resp.data
    assert b'Visible post' in resp.data
    assert b'Hidden post' in resp.data
    assert b'Visible' in resp.data
    assert b'Hidden' in resp.data
    assert b'data-post-format="bold"' in resp.data
    assert b'data-post-format="italic"' in resp.data
    assert b'data-post-format="bullets"' in resp.data


def test_admin_updates_post_description(
        admin_client, csrf_token, services):
    services.user_manager.create_post(
        'Editable post', 'Original description', 'editable.jpg', active=True)
    post_item = services.user_manager.get_posts()[0]

    response = post(
        admin_client, '/admin/posts/update', csrf_token,
        post_id=post_item['id'], description='Updated description')

    assert response.status_code == 302
    assert services.user_manager.get_posts()[0]['description'] == (
        'Updated description')

    page = admin_client.get('/admin/posts')
    assert b'action="/admin/posts/update"' in page.data
    assert b'Updated description' in page.data
    assert b'>Update<' in page.data


def test_post_description_update_is_limited_to_500_characters(
        admin_client, csrf_token, services):
    services.user_manager.create_post('Post', '', 'post.jpg', active=True)
    post_item = services.user_manager.get_posts()[0]

    post(admin_client, '/admin/posts/update', csrf_token,
         post_id=post_item['id'], description='x' * 600)

    assert len(services.user_manager.get_posts()[0]['description']) == 500


def test_post_description_update_requires_csrf(admin_client, services):
    services.user_manager.create_post(
        'Protected post', 'Original', 'protected.jpg', active=True)
    post_item = services.user_manager.get_posts()[0]

    response = admin_client.post('/admin/posts/update', data={
        'post_id': post_item['id'], 'description': 'Unauthorized change'})

    assert response.status_code == 400
    assert services.user_manager.get_posts()[0]['description'] == 'Original'


def test_post_description_is_escaped_in_update_form(admin_client, services):
    services.user_manager.create_post(
        'Safe post', '<script>alert(1)</script>', 'safe.jpg', active=True)

    response = admin_client.get('/admin/posts')

    assert b'<script>alert(1)</script>' not in response.data
    assert b'&lt;script&gt;alert(1)&lt;/script&gt;' in response.data


def test_admin_chooses_initial_visibility_for_each_post(
        admin_client, csrf_token, services, monkeypatch):
    filenames = iter(('visible.jpg', 'hidden.jpg'))
    monkeypatch.setattr('routes.admin._save_image', lambda _file: next(filenames))

    visible_response = post(
        admin_client, '/admin/posts', csrf_token,
        title='Visible from creation', description='',
        visible_in_portal='1')
    hidden_response = post(
        admin_client, '/admin/posts', csrf_token,
        title='Hidden from creation', description='')

    assert visible_response.status_code == 302
    assert hidden_response.status_code == 302
    states = {item['title']: item['active']
              for item in services.user_manager.get_posts()}
    assert states == {
        'Visible from creation': 1,
        'Hidden from creation': 0,
    }


def test_post_upload_rejects_spoofed_image_extension(
        admin_client, csrf_token, services):
    resp = admin_client.post('/admin/posts', data={
        'csrf_token': csrf_token,
        'title': 'Not really an image',
        'visible_in_portal': '1',
        'image': (BytesIO(b'<script>alert(1)</script>'), 'spoofed.jpg'),
    })

    assert resp.status_code == 302
    assert services.user_manager.get_posts() == []


def test_responses_disable_content_type_sniffing(client, app):
    assert app.config['MAX_CONTENT_LENGTH'] == 5 * 1024 * 1024
    assert client.get('/').headers['X-Content-Type-Options'] == 'nosniff'


def test_create_paid_voucher_records_revenue(admin_client, csrf_token, services):
    resp = post(admin_client, '/vouchers', csrf_token, mode='price', price=5)
    assert resp.status_code == 302
    vouchers = services.user_manager.get_vouchers()
    assert len(vouchers) == 1
    assert vouchers[0]['price'] == 5
    assert vouchers[0]['minutes'] > 0
    assert services.user_manager.get_revenue_summary()['day'] == 5


def test_create_free_voucher_records_no_revenue(admin_client, csrf_token, services):
    resp = post(admin_client, '/vouchers', csrf_token, mode='minutes', minutes=30)
    assert resp.status_code == 302
    vouchers = services.user_manager.get_vouchers()
    assert len(vouchers) == 1
    assert not vouchers[0]['price']
    assert services.user_manager.get_revenue_summary()['day'] == 0


def test_dashboard_shows_devices_with_balance(admin_client, csrf_token, services):
    services.user_manager.add_time(MAC, 5, 60)
    resp = admin_client.get('/admin')
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert 'With Balance' in page
    assert MAC in page


def test_admin_can_deduct_revenue(admin_client, csrf_token, services):
    services.user_manager.add_time(MAC, 30, 300, source='coin')
    resp = post(admin_client, '/transactions', csrf_token, amount=10)
    assert resp.status_code == 302
    assert services.user_manager.get_revenue_summary()['day'] == 20


def test_deduct_revenue_requires_login(client, csrf_token):
    resp = client.post('/transactions', data={'csrf_token': csrf_token, 'amount': 10})
    assert resp.status_code == 302
