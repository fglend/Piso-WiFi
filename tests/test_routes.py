import datetime as dt
from io import BytesIO

from post_formatting import render_post_description
from tests.conftest import MAC, OTHER_MAC


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


def test_transfer_balance_moves_time_to_new_mac(admin_client, csrf_token, services):
    services.user_manager.add_time(MAC, 1, 5)
    resp = post(admin_client, '/admin/transfer_balance', csrf_token,
                mac_address=MAC, to_mac=OTHER_MAC)
    assert resp.status_code == 302
    assert services.user_manager.check_balance(OTHER_MAC) == 5
    assert services.user_manager.check_balance(MAC) == 0
    services.network_controller.unblock_mac.assert_called_with(OTHER_MAC)
    services.network_controller.block_mac.assert_called_with(MAC)


def test_transfer_balance_rejects_invalid_destination(admin_client, csrf_token, services):
    services.user_manager.add_time(MAC, 1, 5)
    post(admin_client, '/admin/transfer_balance', csrf_token,
         mac_address=MAC, to_mac='not-a-mac')
    assert services.user_manager.check_balance(MAC) == 5
    services.network_controller.unblock_mac.assert_not_called()


def _settings_form(**overrides):
    form = {
        'minutes_per_peso': 10, 'coinslot_claim_timeout': 60,
        'coinslot_pulses_per_peso': 1, 'portal_title': 'T',
        'portal_subtitle': 'S', 'dashboard_refresh_seconds': 5,
        'default_download_kbps': 2048, 'default_upload_kbps': 1024,
    }
    form.update(overrides)
    return form


def test_settings_toggle_persists_pause_on_disconnect(admin_client, csrf_token, services):
    post(admin_client, '/admin/settings', csrf_token,
         **_settings_form(pause_on_disconnect='1'))
    assert services.settings.pause_on_disconnect is True
    assert b'checked' in admin_client.get('/admin/settings').data

    # Unchecked boxes are absent from the POST body entirely
    post(admin_client, '/admin/settings', csrf_token, **_settings_form())
    assert services.settings.pause_on_disconnect is False


def test_settings_toggle_survives_refresh(admin_client, csrf_token, services):
    """The stored value must win over the .env default on every refresh."""
    services.settings.pause_on_disconnect = True
    post(admin_client, '/admin/settings', csrf_token, **_settings_form())
    services.settings.pause_on_disconnect = True   # simulate an env-based reset
    services.refresh_runtime_settings()
    assert services.settings.pause_on_disconnect is False


def test_balance_tab_flags_paused_devices(admin_client, services):
    services.user_manager.add_time(MAC, 5, 25)
    assert b'Paused' not in admin_client.get('/admin').data

    services.user_manager.set_paused(MAC, True)
    body = admin_client.get('/admin').data
    assert b'Paused' in body
    assert b'clock stopped by the customer' in body


def test_balance_tab_shows_duration_pass_expiry(admin_client, services):
    code = services.user_manager.create_voucher(30 * 1440, duration_days=30)
    services.user_manager.redeem_voucher(code, MAC)
    assert b'pass until' in admin_client.get('/admin').data


def test_create_duration_pass_voucher(admin_client, csrf_token, services):
    resp = post(admin_client, '/vouchers', csrf_token, mode='duration',
                duration_days=30, duration_price=500, duration_pausable='1')
    assert resp.status_code == 302
    voucher = services.user_manager.get_vouchers()[0]
    assert voucher['duration_days'] == 30
    assert voucher['pausable'] == 1
    assert voucher['price'] == 500
    # Sold up front, so it lands in revenue at creation like other paid codes
    assert services.user_manager.get_revenue_summary()['day'] == 500


def test_create_non_pausable_duration_pass(admin_client, csrf_token, services):
    post(admin_client, '/vouchers', csrf_token, mode='duration',
         duration_days=15, duration_price=300)   # checkbox absent = unchecked
    assert services.user_manager.get_vouchers()[0]['pausable'] == 0


def test_non_pausable_pass_hides_button_despite_global_setting(
        client, csrf_token, services):
    services.settings.allow_manual_pause = True
    code = services.user_manager.create_voucher(
        15 * 1440, duration_days=15, pausable=False)
    services.user_manager.redeem_voucher(code, MAC)

    assert b'Pause my time' not in client.get('/').data
    post(client, '/pause', csrf_token)
    assert services.user_manager.is_paused(MAC) is False


def test_pause_button_shown_and_works_when_enabled(client, csrf_token, services):
    services.settings.allow_manual_pause = True
    services.user_manager.add_time(MAC, 5, 25)

    assert b'Pause my time' in client.get('/').data
    post(client, '/pause', csrf_token)
    assert services.user_manager.is_paused(MAC) is True


def test_pause_hidden_and_rejected_when_disabled(client, csrf_token, services):
    services.settings.allow_manual_pause = False
    services.user_manager.add_time(MAC, 5, 25)

    assert b'Pause my time' not in client.get('/').data
    post(client, '/pause', csrf_token)
    assert services.user_manager.is_paused(MAC) is False


def test_manual_pause_available_while_continuation_is_on(client, csrf_token, services):
    """The headline combination: the clock runs while a device is away, but a
    customer who pauses before leaving stops their own meter."""
    services.settings.pause_on_disconnect = False   # continuation
    services.settings.allow_manual_pause = True     # but pausing is offered
    services.user_manager.add_time(MAC, 5, 25)

    assert b'Pause my time' in client.get('/').data
    post(client, '/pause', csrf_token)
    assert services.user_manager.is_paused(MAC) is True


def test_resume_still_available_when_pausing_disabled(client, csrf_token, services):
    """A device paused before the operator flipped the switch must not be
    stranded with a frozen balance and no internet."""
    services.settings.allow_manual_pause = True
    services.user_manager.add_time(MAC, 5, 25)
    post(client, '/pause', csrf_token)

    services.settings.allow_manual_pause = False
    assert b'Resume my time' in client.get('/').data
    post(client, '/resume', csrf_token)
    assert services.user_manager.is_paused(MAC) is False


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
    # Rendered by the hero card's Speed fact as "<down> / <up> Mbps".
    assert b'2.048 / 1.024 Mbps' in resp.data
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


# --- system health panel -----------------------------------------------------

def test_dashboard_renders_the_health_card(admin_client):
    resp = admin_client.get('/admin')

    assert resp.status_code == 200
    assert b'System Health' in resp.data
    assert b'SoC temperature' in resp.data


def test_dashboard_survives_a_host_with_no_health_sources(admin_client, monkeypatch):
    import system_info
    monkeypatch.setattr(system_info, 'THERMAL_PATHS', ('/nonexistent/temp',))
    monkeypatch.setattr(system_info, 'LOADAVG_PATH', '/nonexistent/loadavg')
    monkeypatch.setattr(system_info, 'MEMINFO_PATH', '/nonexistent/meminfo')
    monkeypatch.setattr(system_info, 'UPTIME_PATH', '/nonexistent/uptime')

    resp = admin_client.get('/admin')

    # A dashboard must never 500 because a board lacks a thermal zone.
    assert resp.status_code == 200
    assert b'no thermal sensor exposed' in resp.data


def test_dashboard_live_includes_health(admin_client):
    payload = admin_client.get('/admin/live').get_json()

    assert 'health' in payload
    assert 'alerts' in payload['health']
    assert 'services' in payload['health']


# --- sales report ------------------------------------------------------------

def test_sales_report_renders(admin_client):
    resp = admin_client.get('/admin/reports')

    assert resp.status_code == 200
    assert b'Sales Report' in resp.data
    assert b'Export CSV' in resp.data


def test_sales_report_defaults_to_seven_days(admin_client):
    resp = admin_client.get('/admin/reports')
    today = dt.date.today()

    assert resp.status_code == 200
    assert str(today).encode() in resp.data
    assert str(today - dt.timedelta(days=6)).encode() in resp.data


def test_sales_report_accepts_an_explicit_range(admin_client):
    resp = admin_client.get('/admin/reports?start=2026-01-01&end=2026-01-31')

    assert resp.status_code == 200
    assert b'2026-01-01 to 2026-01-31' in resp.data


def test_sales_report_swaps_a_reversed_range(admin_client):
    resp = admin_client.get('/admin/reports?start=2026-01-31&end=2026-01-01')

    assert resp.status_code == 200
    assert b'2026-01-01 to 2026-01-31' in resp.data


def test_sales_report_falls_back_on_an_unparseable_date(admin_client):
    # A mistyped URL should still show a usable report, not a 400.
    resp = admin_client.get('/admin/reports?start=not-a-date&end=also-bad')

    assert resp.status_code == 200
    assert str(dt.date.today()).encode() in resp.data


def test_sales_report_rejects_an_unknown_grouping(admin_client):
    resp = admin_client.get('/admin/reports?group_by=hour')

    assert resp.status_code == 200
    assert b'grouped by day' in resp.data


def test_csv_export_has_a_header_and_attachment_name(admin_client):
    resp = admin_client.get('/admin/reports/export.csv?start=2026-01-01&end=2026-01-31')

    assert resp.status_code == 200
    assert resp.mimetype == 'text/csv'
    assert 'attachment' in resp.headers['Content-Disposition']
    assert 'sales-2026-01-01-to-2026-01-31.csv' in resp.headers['Content-Disposition']
    assert resp.data.splitlines()[0] == b'created_at,mac_address,source,amount,minutes'


def test_csv_export_rows_reconcile_with_the_report(admin_client, services):
    services.user_manager.add_time(MAC, 5, 25)
    services.user_manager.add_time(OTHER_MAC, 10, 50)
    today = dt.date.today().isoformat()

    resp = admin_client.get(f'/admin/reports/export.csv?start={today}&end={today}')
    rows = [line for line in resp.data.decode().splitlines()[1:] if line]
    exported_total = sum(float(line.split(',')[3]) for line in rows)
    report = services.user_manager.get_sales_report(today, today, 'day')

    assert len(rows) == report['totals']['count']
    assert exported_total == report['totals']['net']


def test_reports_require_admin(client):
    assert client.get('/admin/reports').status_code in (302, 401, 403)
    assert client.get('/admin/reports/export.csv').status_code in (302, 401, 403)


# --- theming -----------------------------------------------------------------

def test_theme_colours_are_applied_to_the_portal(client, services):
    services.user_manager.update_app_settings({'theme_accent': '#ff0000'})
    services.refresh_runtime_settings()

    # Plain client: an admin session at / is redirected to the dashboard.
    resp = client.get('/')

    assert b'--accent: #ff0000' in resp.data


def test_default_theme_emits_no_override_block(client):
    resp = client.get('/')

    # The shipped palette already lives in app.css; re-declaring it would be
    # dead weight on every captive-portal page load.
    assert b'--accent: #0f766e' not in resp.data


def test_invalid_stored_colour_is_ignored(services):
    services.user_manager.update_app_settings({'theme_accent': 'red; }'})
    services.refresh_runtime_settings()

    # Anything that is not #rrggbb must never reach the <style> block.
    assert services.settings.theme_accent == '#0f766e'


def test_settings_page_exposes_branding_controls(admin_client):
    resp = admin_client.get('/admin/settings')

    assert resp.status_code == 200
    assert b'Branding' in resp.data
    assert b'name="theme_accent"' in resp.data
    assert b'multipart/form-data' in resp.data


# --- portal app shell --------------------------------------------------------

def test_portal_is_a_single_screen_shell(client):
    body = client.get('/').data

    assert b'class="portal-app"' in body
    assert b'class="portal-shell"' in body
    # Secondary content moved into sheets so the shell itself never scrolls.
    assert b'id="sheet-rates"' in body
    assert b'id="sheet-sessions"' in body
    assert b'id="sheet-device"' in body


def test_portal_shows_disconnected_pill_with_no_balance(client):
    body = client.get('/').data

    assert b'Disconnected' in body
    assert b'Connected' not in body.replace(b'Disconnected', b'')


def test_portal_shows_connected_pill_with_balance(client, services):
    services.user_manager.add_time(MAC, 5, 25)

    body = client.get('/').data

    assert b'>Connected' in body
    assert b'Time Remaining' in body


def test_sessions_endpoint_returns_this_devices_history(client, services):
    services.user_manager.sync_connection_snapshot(
        [{'mac_address': MAC, 'ip': '192.168.4.7', 'hostname': 'phone'}])

    payload = client.get('/sessions').get_json()

    assert len(payload['sessions']) == 1
    assert payload['sessions'][0]['ip_address'] == '192.168.4.7'
    # An open session has no end yet.
    assert payload['sessions'][0]['disconnected_at'] is None


def test_sessions_endpoint_is_empty_without_history(client):
    assert client.get('/sessions').get_json() == {'sessions': []}


def test_sessions_are_not_built_into_every_portal_render(client, services):
    """The list is fetched when the sheet opens, not on every page load.

    Rendering it inline cost ~11% of the portal render for something most
    customers never open, on a box also serving every other phone.
    """
    services.user_manager.sync_connection_snapshot(
        [{'mac_address': MAC, 'ip': '192.168.4.7', 'hostname': 'phone'}])

    body = client.get('/').data

    assert b'Recent Sessions' in body          # the sheet still exists
    assert b'192.168.4.7' not in body          # but its rows do not
    assert b'data-sessions-list' in body


def test_sessions_endpoint_only_ever_returns_the_callers_own_history(
        client, services):
    services.user_manager.sync_connection_snapshot([
        {'mac_address': MAC, 'ip': '192.168.4.7', 'hostname': 'mine'},
        {'mac_address': OTHER_MAC, 'ip': '192.168.4.9', 'hostname': 'theirs'},
    ])

    payload = client.get('/sessions').get_json()

    # The MAC comes from the requesting IP, never from the client.
    addresses = [row['ip_address'] for row in payload['sessions']]
    assert addresses == ['192.168.4.7']


# --- coin slot markup (fixtures default to coinslot=None, so force it on) ----

def _with_coinslot(services):
    from unittest.mock import MagicMock
    services.coinslot = MagicMock()
    return services


def test_coin_modal_survives_with_every_id_the_poller_binds(client, services):
    _with_coinslot(services)

    body = client.get('/').data.decode()

    # The coin poller resolves each of these by id and writes to it without a
    # null check; a restructure that drops one breaks the whole session view.
    for element_id in (
        'coin-modal', 'coin-modal-title', 'coin-modal-detail',
        'coin-modal-seconds', 'coin-modal-pesos', 'coin-modal-minutes',
        'coin-modal-alert', 'coin-modal-close',
        'claim-coin-button', 'coin-session-title', 'coin-session-detail',
        'coin-session-badge', 'coin-seconds-left', 'coin-pesos-inserted',
        'coin-minutes-added', 'coin-countdown-bar', 'coin-detected-alert',
        'balance-minutes',
    ):
        assert f'id="{element_id}"' in body, f'missing #{element_id}'

    # setStep() walks these; all five must still exist.
    for step in range(1, 6):
        assert f'data-step="{step}"' in body


def test_coin_form_actions_are_unchanged(client, services):
    _with_coinslot(services)

    body = client.get('/').data

    assert b'action="/insert_coin"' in body
    assert b'action="/coin_done"' in body
    assert b'>Insert Coin<' in body


def test_portal_forms_keep_their_contracts(client):
    body = client.get('/').data

    assert b'action="/redeem"' in body
    assert b'name="code"' in body
    assert b'name="csrf_token"' in body


# --- portal announcements ----------------------------------------------------

def test_post_banner_links_to_a_full_size_sheet(client, services):
    services.user_manager.create_post(
        'Summer Promo', 'Buy 20 pesos get 5 minutes free.', 'promo.jpg',
        active=True)

    body = client.get('/').data

    # The banner is a cropped preview; the sheet carries the uncropped image.
    assert b'data-sheet="sheet-post-0"' in body
    assert b'id="sheet-post-0"' in body
    assert b'class="pa-post-full"' in body
    assert b'Tap to read more' in body


def test_post_description_appears_in_full_in_the_sheet(client, services):
    long_text = 'Mahaba ito. ' * 20
    services.user_manager.create_post('Notice', long_text, 'notice.jpg',
                                      active=True)

    body = client.get('/').data.decode()

    # Clamped to two lines in the banner by CSS, but never truncated server
    # side - the sheet must hold the whole thing.
    assert body.count('Mahaba ito.') >= 20


def test_each_post_gets_its_own_sheet(client, services):
    services.user_manager.create_post('One', 'a', 'one.jpg', active=True)
    services.user_manager.create_post('Two', 'b', 'two.jpg', active=True)

    body = client.get('/').data

    assert b'id="sheet-post-0"' in body
    assert b'id="sheet-post-1"' in body


def test_portal_without_posts_shows_the_plain_banner(client):
    body = client.get('/').data

    assert b'pa-banner is-plain' in body
    assert b'sheet-post-0' not in body


# --- per-device pause permission ---------------------------------------------

def _redeem_pausable_pass(services, mac=MAC, days=30):
    """A duration pass sold with pausing allowed - the mistake being corrected."""
    code = services.user_manager.create_voucher(
        days * 1440, duration_days=days, pausable=True)
    services.user_manager.redeem_voucher(code, mac)
    return code


def test_admin_can_revoke_pausing_on_an_already_redeemed_pass(
        admin_client, csrf_token, services):
    _redeem_pausable_pass(services)
    assert services.user_manager.is_pausable(MAC) is True

    resp = post(admin_client, '/admin/set_pausable', csrf_token,
                mac_address=MAC, pausable='0')

    assert resp.status_code == 302
    assert services.user_manager.is_pausable(MAC) is False


def test_revoking_pausing_actually_blocks_the_portal_pause(
        admin_client, client, csrf_token, services):
    services.settings.allow_manual_pause = True
    _redeem_pausable_pass(services)

    post(admin_client, '/admin/set_pausable', csrf_token,
         mac_address=MAC, pausable='0')
    # admin_client is the same client with a session flag; while it is set,
    # GET / redirects to the dashboard and any portal assertion passes for the
    # wrong reason.
    client.get('/logout')

    # Not just a hidden button - the portal route must refuse it too.
    portal = client.get('/')
    assert portal.status_code == 200
    assert b'Pause my time' not in portal.data
    post(client, '/pause', csrf_token)
    assert services.user_manager.is_paused(MAC) is False


def test_admin_can_restore_pausing(admin_client, csrf_token, services):
    _redeem_pausable_pass(services)
    post(admin_client, '/admin/set_pausable', csrf_token,
         mac_address=MAC, pausable='0')

    post(admin_client, '/admin/set_pausable', csrf_token,
         mac_address=MAC, pausable='1')

    assert services.user_manager.is_pausable(MAC) is True


def test_revoking_does_not_resume_a_device_already_paused(
        admin_client, csrf_token, services):
    _redeem_pausable_pass(services)
    services.user_manager.set_paused(MAC, True)

    post(admin_client, '/admin/set_pausable', csrf_token,
         mac_address=MAC, pausable='0')

    # Auto-resuming would restart the customer's clock without warning.
    assert services.user_manager.is_paused(MAC) is True
    assert services.user_manager.is_pausable(MAC) is False


def test_paused_device_can_still_resume_after_pausing_is_revoked(
        admin_client, client, csrf_token, services):
    _redeem_pausable_pass(services)
    services.user_manager.set_paused(MAC, True)
    post(admin_client, '/admin/set_pausable', csrf_token,
         mac_address=MAC, pausable='0')
    client.get('/logout')

    assert b'Resume my time' in client.get('/').data
    post(client, '/resume', csrf_token)

    # Nobody may be stranded with a frozen balance and no internet.
    assert services.user_manager.is_paused(MAC) is False


def test_set_pausable_rejects_an_invalid_mac(admin_client, csrf_token, services):
    _redeem_pausable_pass(services)

    post(admin_client, '/admin/set_pausable', csrf_token,
         mac_address='00:11; not-a-mac', pausable='0')

    assert services.user_manager.is_pausable(MAC) is True


def test_set_pausable_reports_an_unknown_device(admin_client, csrf_token, services):
    resp = post(admin_client, '/admin/set_pausable', csrf_token,
                mac_address=OTHER_MAC, pausable='0')

    assert resp.status_code == 302
    assert services.user_manager.set_pausable(OTHER_MAC, False) is False


def test_set_pausable_requires_login(client, csrf_token, services):
    _redeem_pausable_pass(services)

    post(client, '/admin/set_pausable', csrf_token,
         mac_address=MAC, pausable='0')

    assert services.user_manager.is_pausable(MAC) is True


def test_balance_tab_offers_the_toggle_and_flags_disabled_passes(
        admin_client, csrf_token, services):
    _redeem_pausable_pass(services)
    assert b'Disable Pausing' in admin_client.get('/admin').data

    post(admin_client, '/admin/set_pausable', csrf_token,
         mac_address=MAC, pausable='0')

    body = admin_client.get('/admin').data
    assert b'Allow Pausing' in body
    assert b'pausing disabled' in body


# --- voucher-level pause permission ------------------------------------------

def test_disabling_pause_on_an_unredeemed_voucher_applies_at_redemption(
        admin_client, csrf_token, services):
    code = services.user_manager.create_voucher(
        30 * 1440, duration_days=30, pausable=True)

    post(admin_client, '/vouchers/pausable', csrf_token, code=code, pausable='0')
    services.user_manager.redeem_voucher(code, MAC)

    # grant_duration() copies the voucher flag onto the user at redemption.
    assert services.user_manager.is_pausable(MAC) is False


def test_disabling_pause_on_a_redeemed_voucher_cascades_to_the_device(
        admin_client, csrf_token, services):
    code = services.user_manager.create_voucher(
        30 * 1440, duration_days=30, pausable=True)
    services.user_manager.redeem_voucher(code, MAC)
    assert services.user_manager.is_pausable(MAC) is True

    post(admin_client, '/vouchers/pausable', csrf_token, code=code, pausable='0')

    # Fixing only the voucher row would be useless here: is_pausable() reads
    # users.pausable, not vouchers.pausable.
    assert services.user_manager.is_pausable(MAC) is False


def test_cascade_actually_blocks_the_portal_pause(
        admin_client, client, csrf_token, services):
    services.settings.allow_manual_pause = True
    code = services.user_manager.create_voucher(
        30 * 1440, duration_days=30, pausable=True)
    services.user_manager.redeem_voucher(code, MAC)

    post(admin_client, '/vouchers/pausable', csrf_token, code=code, pausable='0')
    client.get('/logout')

    portal = client.get('/')
    assert portal.status_code == 200
    assert b'Pause my time' not in portal.data
    post(client, '/pause', csrf_token)
    assert services.user_manager.is_paused(MAC) is False


def test_reallowing_pause_on_a_redeemed_voucher_cascades_back(
        admin_client, csrf_token, services):
    code = services.user_manager.create_voucher(
        30 * 1440, duration_days=30, pausable=True)
    services.user_manager.redeem_voucher(code, MAC)
    post(admin_client, '/vouchers/pausable', csrf_token, code=code, pausable='0')

    post(admin_client, '/vouchers/pausable', csrf_token, code=code, pausable='1')

    assert services.user_manager.is_pausable(MAC) is True


def test_voucher_pausable_leaves_other_devices_alone(
        admin_client, csrf_token, services):
    mine = services.user_manager.create_voucher(
        30 * 1440, duration_days=30, pausable=True)
    theirs = services.user_manager.create_voucher(
        30 * 1440, duration_days=30, pausable=True)
    services.user_manager.redeem_voucher(mine, MAC)
    services.user_manager.redeem_voucher(theirs, OTHER_MAC)

    post(admin_client, '/vouchers/pausable', csrf_token, code=mine, pausable='0')

    assert services.user_manager.is_pausable(MAC) is False
    assert services.user_manager.is_pausable(OTHER_MAC) is True


def test_voucher_pausable_reports_an_unknown_code(admin_client, csrf_token, services):
    resp = post(admin_client, '/vouchers/pausable', csrf_token,
                code='NOPE-NOPE', pausable='0')

    assert resp.status_code == 302
    assert services.user_manager.set_voucher_pausable('NOPE-NOPE', False) == {
        'found': False, 'cascaded_to': None}


def test_voucher_pausable_requires_login(client, csrf_token, services):
    code = services.user_manager.create_voucher(
        30 * 1440, duration_days=30, pausable=True)
    services.user_manager.redeem_voucher(code, MAC)

    post(client, '/vouchers/pausable', csrf_token, code=code, pausable='0')

    assert services.user_manager.is_pausable(MAC) is True


def test_voucher_page_offers_the_toggle_for_duration_passes(
        admin_client, csrf_token, services):
    services.user_manager.create_voucher(30 * 1440, duration_days=30,
                                         pausable=True)

    body = admin_client.get('/vouchers').data
    assert b'Disable Pause' in body

    code = services.user_manager.get_vouchers()[0]['code']
    post(admin_client, '/vouchers/pausable', csrf_token, code=code, pausable='0')

    body = admin_client.get('/vouchers').data
    assert b'Allow Pause' in body
    assert b'No pause' in body


def test_voucher_page_shows_no_toggle_for_minute_vouchers(admin_client, services):
    services.user_manager.create_voucher(60)

    body = admin_client.get('/vouchers').data

    # Pause permission is meaningless on a plain minutes top-up.
    assert b'Disable Pause' not in body
    assert b'n/a' in body


def test_redeemed_voucher_toggle_is_reachable_on_the_show_all_page(
        admin_client, services):
    code = services.user_manager.create_voucher(
        30 * 1440, duration_days=30, pausable=True)
    services.user_manager.redeem_voucher(code, MAC)

    # The default view lists unredeemed codes only, so the control for a spent
    # pass is only reachable via the Show all toggle.
    assert code.encode() not in admin_client.get('/vouchers').data

    body = admin_client.get('/vouchers?all=1').data
    assert code.encode() in body
    assert b'Disable Pause' in body


def test_cascade_is_reported_to_the_operator(admin_client, csrf_token, services):
    code = services.user_manager.create_voucher(
        30 * 1440, duration_days=30, pausable=True)
    services.user_manager.redeem_voucher(code, MAC)

    resp = admin_client.post(
        '/vouchers/pausable',
        data={'csrf_token': csrf_token, 'code': code, 'pausable': '0'},
        follow_redirects=True)

    # The operator must be told the change reached a real customer, not just
    # the voucher row.
    assert b'Applied to the device that already redeemed it' in resp.data
    assert MAC.encode() in resp.data


# --- clear unpaid connected devices ------------------------------------------

def _connected(services, *macs):
    services.network_controller.get_connected_devices.return_value = [
        {'mac_address': mac, 'ip': f'192.168.4.{i + 2}',
         'hostname': f'h{i}', 'signal': '-50'}
        for i, mac in enumerate(macs)]


def test_clear_unpaid_forgets_only_devices_without_balance(
        admin_client, csrf_token, services):
    _connected(services, MAC, OTHER_MAC)
    services.user_manager.add_time(OTHER_MAC, 5, 25)   # this one paid

    resp = post(admin_client, '/admin/devices/reset_unpaid', csrf_token)

    assert resp.status_code == 302
    services.network_controller.forget_devices.assert_called_once_with([MAC])


def test_clear_unpaid_never_touches_a_paused_customer(
        admin_client, csrf_token, services):
    _connected(services, MAC)
    services.user_manager.add_time(MAC, 5, 25)
    services.user_manager.set_paused(MAC, True)

    post(admin_client, '/admin/devices/reset_unpaid', csrf_token)

    # A paused device still holds time; clearing it would look like theft.
    services.network_controller.forget_devices.assert_not_called()


def test_clear_unpaid_reports_when_there_is_nothing_to_do(
        admin_client, csrf_token, services):
    _connected(services)

    resp = admin_client.post(
        '/admin/devices/reset_unpaid', data={'csrf_token': csrf_token},
        follow_redirects=True)

    assert b'No connected devices without time balance' in resp.data
    services.network_controller.forget_devices.assert_not_called()


def test_clear_unpaid_reports_how_many_were_cleared(
        admin_client, csrf_token, services):
    _connected(services, MAC, OTHER_MAC)
    services.network_controller.forget_devices.return_value = [MAC, OTHER_MAC]

    resp = admin_client.post(
        '/admin/devices/reset_unpaid', data={'csrf_token': csrf_token},
        follow_redirects=True)

    assert b'Cleared 2 connected device(s)' in resp.data


def test_clear_unpaid_deletes_no_records(admin_client, csrf_token, services):
    _connected(services, MAC)
    services.user_manager.add_time(MAC, 5, 25)
    services.user_manager.deduct_time(MAC, 25)         # spent, balance now 0
    services.network_controller.forget_devices.return_value = [MAC]

    post(admin_client, '/admin/devices/reset_unpaid', csrf_token)

    # The device row and its audit trail must survive: this action is a
    # network reset, not a purge.
    assert services.user_manager.get_device_info(MAC) is not None
    assert services.user_manager.get_transactions() != []


def test_clear_unpaid_requires_login(client, csrf_token, services):
    _connected(services, MAC)

    resp = post(client, '/admin/devices/reset_unpaid', csrf_token)

    assert resp.status_code == 302
    services.network_controller.forget_devices.assert_not_called()


def test_settings_page_offers_the_clear_button(admin_client):
    body = admin_client.get('/admin/settings').data

    assert b'Clear Unpaid Devices' in body
    assert b'/admin/devices/reset_unpaid' in body
