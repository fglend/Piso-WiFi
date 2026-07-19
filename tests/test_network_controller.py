from threading import Event, Thread
from unittest.mock import mock_open, patch

import pytest

from network.ap_manager import APManager, is_valid_mac
from network.firewall import Firewall
from network.qos import QoSManager
from network.wired import WiredGateway
from network_controller import NetworkController
from tests.conftest import MAC, OTHER_MAC

POE_AP_MAC = "AA:BB:CC:DD:EE:01"
POE_AP_IP = "192.168.4.2"


@pytest.fixture
def network_controller(settings):
    return NetworkController(settings, manage_hardware=False)


def test_is_valid_mac():
    assert is_valid_mac("00:11:22:33:44:55")
    assert is_valid_mac("aa:bb:cc:dd:ee:ff")
    assert not is_valid_mac("not-a-mac")
    assert not is_valid_mac("00:11:22:33:44")
    assert not is_valid_mac("00:11:22:33:44:55; rm -rf /")
    assert not is_valid_mac("")


def test_wired_dns_config_maps_portal_hostname(settings, tmp_path):
    settings.portal_hostname = 'glend-pisowifi'
    gateway = WiredGateway(settings)
    gateway.dnsmasq_conf = str(tmp_path / 'dnsmasq.conf')

    gateway.write_configs()

    config = (tmp_path / 'dnsmasq.conf').read_text()
    assert 'host-record=glend-pisowifi,192.168.4.1' in config


def test_hostapd_dns_config_maps_portal_hostname(settings, tmp_path):
    settings.portal_hostname = 'glend-pisowifi'
    manager = APManager(settings)
    manager.hostapd_conf = str(tmp_path / 'hostapd.conf')
    manager.dnsmasq_conf = str(tmp_path / 'dnsmasq.conf')

    with patch('network.ap_manager.os.makedirs'):
        manager.write_configs()

    config = (tmp_path / 'dnsmasq.conf').read_text()
    assert 'host-record=glend-pisowifi,192.168.4.1' in config


def test_portal_hostname_rejects_dnsmasq_config_injection(settings):
    settings.portal_hostname = 'glend-pisowifi\nserver=attacker.example'

    with pytest.raises(RuntimeError, match='PORTAL_HOSTNAME'):
        settings.validate()


def test_block_mac(network_controller):
    with patch('network.firewall.run_cmd') as mock_run:
        assert network_controller.block_mac(MAC) is True
        # last call inserts the DROP rule as an argument list (no shell)
        args = mock_run.call_args_list[-1][0][0]
        assert args[0] == 'iptables'
        assert 'DROP' in args
        assert MAC in args


def test_unblock_mac(network_controller):
    with patch('network.firewall.run_cmd') as mock_run:
        assert network_controller.unblock_mac(MAC) is True
    commands = [call.args[0] for call in mock_run.call_args_list]
    assert any(MAC in command and 'ACCEPT' in command for command in commands)
    assert any(MAC in command and 'RETURN' in command for command in commands)


def test_firewall_allows_established_return_traffic_from_uplink():
    firewall = Firewall('eth0', 'eth1', '192.168.4.1')

    with patch('network.firewall.open', mock_open()), \
            patch('network.firewall.run_cmd') as mock_run:
        firewall.setup()

    commands = [call.args[0] for call in mock_run.call_args_list]
    assert [
        'iptables', '-I', 'FORWARD', '1',
        '-i', 'eth1', '-o', 'eth0',
        '-m', 'state', '--state', 'ESTABLISHED,RELATED', '-j', 'ACCEPT',
    ] in commands
    assert [
        'iptables', '-I', 'INPUT', '1',
        '-i', 'eth1', '-p', 'tcp', '--dport', '5000', '-j', 'DROP',
    ] in commands


def test_firewall_redirects_unpaid_http_clients_to_portal():
    firewall = Firewall('eth0', 'eth1', '192.168.4.1')

    with patch('network.firewall.open', mock_open()), \
            patch('network.firewall.run_cmd') as run_cmd:
        firewall.setup()

    commands = [call.args[0] for call in run_cmd.call_args_list]
    assert ['iptables', '-t', 'nat', '-N', 'PISOWIFI_PORTAL'] in commands
    assert [
        'iptables', '-t', 'nat', '-I', 'PREROUTING', '1',
        '-i', 'eth0', '-p', 'tcp', '--dport', '80',
        '-j', 'PISOWIFI_PORTAL',
    ] in commands
    assert [
        'iptables', '-t', 'nat', '-A', 'PISOWIFI_PORTAL',
        '-p', 'tcp', '-j', 'REDIRECT', '--to-ports', '5000',
    ] in commands
    assert ['iptables', '-A', 'PISOWIFI_INPUT', '-j', 'DROP'] in commands
    assert not any(
        all(value in command for value in ('PISOWIFI', '--dport', '53'))
        for command in commands
        if 'PISOWIFI_INPUT' not in command)


def test_allowed_client_bypasses_captive_portal_redirect():
    firewall = Firewall('eth0', 'eth1', '192.168.4.1')

    with patch('network.firewall.run_cmd') as run_cmd:
        assert firewall.allow_mac(MAC) is True

    commands = [call.args[0] for call in run_cmd.call_args_list]
    assert [
        'iptables', '-t', 'nat', '-I', 'PISOWIFI_PORTAL', '1',
        '-m', 'mac', '--mac-source', MAC, '-j', 'RETURN',
    ] in commands
    assert any(
        all(value in command for value in (MAC, '-o', 'eth1', 'ACCEPT'))
        for command in commands)


def test_blocked_client_loses_captive_portal_bypass():
    firewall = Firewall('eth0', 'eth1', '192.168.4.1')

    with patch('network.firewall.run_cmd') as run_cmd:
        assert firewall.block_mac(MAC) is True

    commands = [call.args[0] for call in run_cmd.call_args_list]
    assert [
        'iptables', '-t', 'nat', '-D', 'PISOWIFI_PORTAL',
        '-m', 'mac', '--mac-source', MAC, '-j', 'RETURN',
    ] in commands
    assert any(MAC in command and 'DROP' in command for command in commands)


def test_captive_redirect_does_not_intercept_https():
    firewall = Firewall('eth0', 'eth1', '192.168.4.1')

    with patch('network.firewall.open', mock_open()), \
            patch('network.firewall.run_cmd') as run_cmd:
        firewall.setup()

    commands = [call.args[0] for call in run_cmd.call_args_list]
    redirect_commands = [
        command for command in commands if 'REDIRECT' in command
    ]
    assert redirect_commands
    assert not any('443' in command for command in commands)


def test_block_rejects_invalid_mac(network_controller):
    with patch('network.firewall.run_cmd') as mock_run:
        assert network_controller.block_mac("evil; reboot") is False
        assert network_controller.unblock_mac("") is False
        mock_run.assert_not_called()


def test_set_bandwidth_requires_known_ip(network_controller):
    with patch.object(network_controller.ap, 'resolve_ip', return_value=None):
        assert network_controller.set_bandwidth_limit(MAC, 1024, 512) is False


def test_qos_class_ids_never_collide():
    qos = QoSManager('wlan0', 2048, 1024)
    with patch('network.qos.run_cmd'):
        assert qos.set_limit("00:11:22:33:44:55", "192.168.4.2", 1024, 512)
        assert qos.set_limit("AA:BB:CC:DD:EE:FF", "192.168.4.3", 1024, 512)
    ids = [c['class_id'] for c in qos._clients.values()]
    assert len(ids) == len(set(ids))


def test_qos_remove_only_targets_own_filters():
    qos = QoSManager('wlan0', 2048, 1024)
    with patch('network.qos.run_cmd'):
        qos.set_limit(MAC, "192.168.4.2")
        class_id = qos._clients[MAC]['class_id']
    with patch('network.qos.run_cmd') as mock_run:
        qos.remove_limit(MAC)
        for call in mock_run.call_args_list:
            args = call[0][0]
            if 'filter' in args and 'del' in args:
                # deletion is scoped by the client's own prio
                assert str(class_id) in args
    assert MAC not in qos._clients


def test_new_device_triggers_policy_callback(network_controller):
    seen = []
    network_controller.on_new_device = seen.append
    with patch.object(network_controller.ap, 'get_stations',
                      return_value=[{'mac_address': MAC, 'ip': '192.168.4.2',
                                     'hostname': 'phone', 'connected': True}]):
        devices = network_controller.get_connected_devices()
    assert seen == [MAC]
    assert devices[0]['mac_address'] == MAC
    # Second sighting is not "new" anymore
    with patch.object(network_controller.ap, 'get_stations',
                      return_value=[{'mac_address': MAC, 'ip': '192.168.4.2',
                                     'hostname': 'phone', 'connected': True}]):
        network_controller.get_connected_devices()
    assert seen == [MAC]


def test_device_snapshot_callback_receives_connect_and_disconnect(network_controller):
    snapshots = []
    network_controller.on_device_snapshot = snapshots.append
    network_controller.on_new_device = lambda mac: None
    station = {'mac_address': MAC, 'ip': '192.168.4.20',
               'hostname': 'phone', 'connected': True}

    with patch.object(network_controller.ap, 'get_stations',
                      side_effect=[[station], [], []]):
        network_controller.get_connected_devices()
        network_controller.get_connected_devices()
        network_controller.get_connected_devices()

    assert snapshots == [(station,), (), ()]


def test_single_empty_snapshot_does_not_disconnect_device(network_controller):
    network_controller.on_new_device = lambda mac: None
    station = {'mac_address': MAC, 'ip': '192.168.4.20',
               'hostname': 'phone', 'connected': True}

    with patch.object(network_controller.ap, 'get_stations',
                      side_effect=[[station], [], [station]]):
        first = network_controller.get_connected_devices()
        transient_empty = network_controller.get_connected_devices()
        recovered = network_controller.get_connected_devices()

    assert first == [station]
    assert transient_empty == [station]
    assert recovered == [station]


def test_discovery_error_preserves_last_known_snapshot(network_controller):
    network_controller.on_new_device = lambda mac: None
    station = {'mac_address': MAC, 'ip': '192.168.4.20',
               'hostname': 'phone', 'connected': True}

    with patch.object(network_controller.ap, 'get_stations',
                      side_effect=[[station], RuntimeError('temporary failure')]):
        network_controller.get_connected_devices()
        after_error = network_controller.get_connected_devices()

    assert after_error == [station]


def test_poe_ap_is_not_treated_as_a_customer_device(settings):
    settings.network_mode = 'wired'
    settings.poe_ap_mac_address = POE_AP_MAC.lower()
    settings.poe_ap_ip_address = POE_AP_IP
    controller = NetworkController(settings, manage_hardware=False)
    seen = []
    controller.on_new_device = seen.append

    stations = [
        {'mac_address': POE_AP_MAC, 'ip': '192.168.4.2',
         'hostname': 'poe-ap', 'connected': True},
        {'mac_address': MAC, 'ip': '192.168.4.3',
         'hostname': 'phone', 'connected': True},
    ]
    with patch.object(controller.ap, 'get_stations', return_value=stations):
        devices = controller.get_connected_devices()

    assert [device['mac_address'] for device in devices] == [MAC]
    assert seen == [MAC]


def test_access_state_and_firewall_transition_are_serialized(network_controller):
    allow_entered = Event()
    release_allow = Event()
    block_attempted = Event()
    block_entered = Event()

    def delayed_allow(_mac):
        allow_entered.set()
        assert release_allow.wait(timeout=1)
        return True

    def tracked_block(_mac):
        block_entered.set()
        return True

    def request_block():
        block_attempted.set()
        network_controller.block_mac(MAC)

    with patch.object(network_controller.firewall, 'allow_mac', delayed_allow), \
            patch.object(network_controller.firewall, 'block_mac', tracked_block):
        allow_thread = Thread(target=network_controller.unblock_mac, args=(MAC,))
        block_thread = Thread(target=request_block)
        allow_thread.start()
        assert allow_entered.wait(timeout=1)
        block_thread.start()
        assert block_attempted.wait(timeout=1)
        assert not block_entered.wait(timeout=0.05)
        release_allow.set()
        allow_thread.join(timeout=1)
        block_thread.join(timeout=1)

    assert block_entered.is_set()
    assert not network_controller.is_access_allowed(MAC)


def test_failed_unblock_marks_access_for_retry(network_controller):
    network_controller.allowed_macs = frozenset({MAC})

    with patch.object(
            network_controller.firewall, 'allow_mac', return_value=False):
        assert network_controller.unblock_mac(MAC) is False

    assert not network_controller.is_access_allowed(MAC)


def test_poe_ap_cannot_be_blocked(settings):
    settings.network_mode = 'wired'
    settings.poe_ap_mac_address = POE_AP_MAC
    settings.poe_ap_ip_address = POE_AP_IP
    controller = NetworkController(settings, manage_hardware=False)

    with patch.object(
            controller.firewall, 'block_mac', return_value=True) as block_mac:
        assert controller.block_mac(POE_AP_MAC.lower()) is True

    block_mac.assert_called_once_with(POE_AP_MAC)


def test_reconcile_does_not_treat_poe_ap_as_a_paying_user(settings):
    settings.network_mode = 'wired'
    settings.poe_ap_mac_address = POE_AP_MAC
    settings.poe_ap_ip_address = POE_AP_IP
    controller = NetworkController(settings, manage_hardware=False)
    active_users = [{
        'mac_address': POE_AP_MAC,
        'download_limit': 2048,
        'upload_limit': 1024,
    }]

    with patch.object(controller.firewall, 'sync') as sync, \
            patch.object(controller.ap, 'resolve_ip') as resolve_ip, \
            patch.object(controller.qos, 'set_limit') as set_limit:
        controller.reconcile(active_users)

    sync.assert_called_once_with([])
    resolve_ip.assert_not_called()
    set_limit.assert_not_called()


def test_poe_ap_exemption_is_disabled_in_hostapd_mode(settings):
    settings.network_mode = 'ap'
    settings.poe_ap_mac_address = POE_AP_MAC
    controller = NetworkController(settings, manage_hardware=False)

    with patch.object(
            controller.firewall, 'block_mac', return_value=True) as block_mac:
        assert controller.block_mac(POE_AP_MAC) is True

    block_mac.assert_called_once_with(POE_AP_MAC)


def test_invalid_poe_ap_mac_fails_configuration_validation(settings):
    settings.poe_ap_mac_address = 'not-a-mac'
    settings.poe_ap_ip_address = POE_AP_IP

    with pytest.raises(RuntimeError, match='POE_AP_MAC_ADDRESS'):
        settings.validate()


def test_poe_ap_mac_requires_reserved_ip(settings):
    settings.poe_ap_mac_address = POE_AP_MAC
    settings.poe_ap_ip_address = ''

    with pytest.raises(RuntimeError, match='POE_AP_IP_ADDRESS'):
        settings.validate()


def test_valid_poe_ap_mac_and_reserved_ip_pass_validation(settings):
    settings.poe_ap_mac_address = POE_AP_MAC
    settings.poe_ap_ip_address = POE_AP_IP
    settings.dhcp_range_start = '192.168.4.20'

    assert settings.validate() is settings


def test_firewall_never_drops_protected_poe_ap():
    firewall = Firewall(
        'eth0', 'eth1', '192.168.4.1', {POE_AP_MAC: POE_AP_IP})

    with patch('network.firewall.run_cmd') as run_cmd:
        assert firewall.block_mac(POE_AP_MAC.lower()) is True

    commands = [call.args[0] for call in run_cmd.call_args_list]
    assert not any(
        '-I' in command and 'DROP' in command for command in commands)
    accept_commands = [command for command in commands if 'ACCEPT' in command]
    assert any(POE_AP_MAC in command and POE_AP_IP in command
               for command in accept_commands)


def test_firewall_sync_keeps_protected_poe_ap_allowed():
    firewall = Firewall(
        'eth0', 'eth1', '192.168.4.1', {POE_AP_MAC: POE_AP_IP})

    with patch('network.firewall.run_cmd') as run_cmd:
        assert firewall.sync([]) is True

    commands = [call.args[0] for call in run_cmd.call_args_list]
    assert any(all(value in command for value in
                   (POE_AP_MAC, POE_AP_IP, 'ACCEPT'))
               for command in commands)
    assert any(all(value in command for value in
                   (POE_AP_MAC, POE_AP_IP, 'RETURN'))
               for command in commands)


def test_dhcp_config_is_authoritative_with_short_leases(settings, tmp_path):
    """MAC-randomization toggles must re-DHCP in seconds, not minutes."""
    for backend in (APManager, WiredGateway):
        manager = backend(settings)
        manager.hostapd_conf = str(tmp_path / 'hostapd.conf')
        manager.dnsmasq_conf = str(tmp_path / 'dnsmasq.conf')
        with patch('network.ap_manager.os.makedirs'):
            manager.write_configs()
        config = (tmp_path / 'dnsmasq.conf').read_text()
        assert 'dhcp-authoritative' in config
        assert 'dhcp-rapid-commit' in config
        assert ',2h' in config
        assert ',24h' not in config


def test_resolve_mac_prefers_live_neighbor_over_stale_lease(settings):
    """A stale lease from a discarded random MAC must not shadow the
    device's current MAC in the kernel neighbor table."""
    manager = APManager(settings)
    stale = {'AA:AA:AA:AA:AA:01': {'ip': '192.168.4.20',
                                   'hostname': 'old-random-id',
                                   'lease_expiry': 9999999999}}
    neigh = '192.168.4.20 dev wlan0 lladdr bb:bb:bb:bb:bb:02 REACHABLE\n'
    with patch.object(manager, 'get_dhcp_leases', return_value=stale), \
            patch('network.ap_manager.run_cmd', return_value=neigh):
        assert manager.resolve_mac('192.168.4.20') == 'BB:BB:BB:BB:BB:02'


def test_resolve_mac_falls_back_to_lease_without_neighbor_entry(settings):
    manager = APManager(settings)
    lease = {'AA:AA:AA:AA:AA:01': {'ip': '192.168.4.20',
                                   'hostname': 'phone',
                                   'lease_expiry': 9999999999}}
    with patch.object(manager, 'get_dhcp_leases', return_value=lease), \
            patch('network.ap_manager.run_cmd', return_value=''):
        assert manager.resolve_mac('192.168.4.20') == 'AA:AA:AA:AA:AA:01'


def test_flush_device_state_clears_neighbor_and_conntrack():
    firewall = Firewall('wlan0', 'eth0', '192.168.4.1')

    with patch('network.firewall.run_cmd') as run_cmd, \
            patch('network.firewall.command_exists', return_value=True):
        firewall.flush_device_state('192.168.4.20')

    commands = [call.args[0] for call in run_cmd.call_args_list]
    assert ['ip', 'neigh', 'flush', 'dev', 'wlan0',
            'to', '192.168.4.20'] in commands
    assert ['conntrack', '-D', '-s', '192.168.4.20'] in commands
    assert ['conntrack', '-D', '-d', '192.168.4.20'] in commands


def test_flush_device_state_survives_missing_conntrack():
    firewall = Firewall('wlan0', 'eth0', '192.168.4.1')

    with patch('network.firewall.run_cmd',
               side_effect=FileNotFoundError('ip')), \
            patch('network.firewall.command_exists', return_value=False):
        firewall.flush_device_state('192.168.4.20')  # must not raise


def test_mac_change_flushes_stale_state_for_reused_ip(network_controller):
    """New MAC appearing on a known IP (randomization toggled off) triggers
    a neighbor/conntrack flush so the portal is reachable immediately."""
    network_controller.on_new_device = lambda mac: None
    old = {'mac_address': MAC, 'ip': '192.168.4.20',
           'hostname': 'phone', 'connected': True}
    new = {'mac_address': 'BB:BB:BB:BB:BB:02', 'ip': '192.168.4.20',
           'hostname': 'phone', 'connected': True}

    with patch.object(network_controller.firewall,
                      'flush_device_state') as flush, \
            patch.object(network_controller.ap, 'get_stations',
                         side_effect=[[old], [new], [new]]):
        network_controller.get_connected_devices()
        network_controller.get_connected_devices()
        network_controller.get_connected_devices()

    flushed = [call.args[0] for call in flush.call_args_list]
    assert '192.168.4.20' in flushed


def test_get_stations_parses_signal_from_single_dump(settings):
    """Signal comes from the one `station dump` call - no per-station subprocess."""
    manager = APManager(settings)
    dump = (
        "Station 00:11:22:33:44:55 (on wlan0)\n"
        "\tinactive time:\t10 ms\n"
        "\tsignal:  \t-55 [-58, -60] dBm\n"
        "\tsignal avg:\t-56 dBm\n"
        "Station 11:22:33:44:55:66 (on wlan0)\n"
        "\tsignal:  \t-71 dBm\n"
    )
    with patch.object(manager, 'get_dhcp_leases', return_value={}), \
            patch('network.ap_manager.run_cmd',
                  return_value=dump) as run_cmd:
        stations = manager.get_stations()

    assert run_cmd.call_count == 1
    assert [s['mac_address'] for s in stations] == [MAC, '11:22:33:44:55:66']
    assert stations[0]['signal'] == '-55 dBm'
    assert stations[1]['signal'] == '-71 dBm'


def test_get_devices_info_batches_macs(user_manager):
    user_manager.add_time(MAC, 5, 30)
    infos = user_manager.get_devices_info([MAC.lower(), OTHER_MAC])
    assert infos[MAC]['time_balance'] == 30
    assert OTHER_MAC not in infos
    assert user_manager.get_devices_info([]) == {}


def test_lookup_cache_is_invalidated_by_flush(settings):
    """Cached identity lookups must not survive a MAC-change flush."""
    nc = NetworkController(settings, manage_hardware=False)
    old_neigh = '192.168.4.20 dev wlan0 lladdr aa:aa:aa:aa:aa:01 REACHABLE'
    new_neigh = '192.168.4.20 dev wlan0 lladdr bb:bb:bb:bb:bb:02 REACHABLE'
    with patch.object(nc.ap, 'get_dhcp_leases', return_value={}):
        with patch('network.ap_manager.run_cmd', return_value=old_neigh):
            assert nc.resolve_mac('192.168.4.20') == 'AA:AA:AA:AA:AA:01'
        with patch('network.ap_manager.run_cmd', return_value=new_neigh):
            # Within the TTL the cached (old) answer is served...
            assert nc.resolve_mac('192.168.4.20') == 'AA:AA:AA:AA:AA:01'
            # ...until a device-change flush invalidates it.
            with patch.object(nc.firewall, 'flush_device_state'):
                nc._flush_stale_state('192.168.4.20')
            assert nc.resolve_mac('192.168.4.20') == 'BB:BB:BB:BB:BB:02'


def test_wired_write_configs_reuses_shared_dnsmasq_template(settings, tmp_path):
    gateway = WiredGateway(settings)
    gateway.dnsmasq_conf = str(tmp_path / 'dnsmasq.conf')
    gateway.write_configs()
    config = (tmp_path / 'dnsmasq.conf').read_text()
    assert 'dhcp-authoritative' in config
    directives = {line.strip() for line in config.splitlines()
                  if not line.strip().startswith('#')}
    assert 'log-queries' not in directives
    assert 'log-dhcp' in directives


def test_firewall_marks_game_udp_for_low_latency_lane():
    firewall = Firewall('eth0', 'eth1', '192.168.4.1',
                        game_udp_ports='5000:5221,20561')

    with patch('network.firewall.open', mock_open()), \
            patch('network.firewall.run_cmd') as run_cmd:
        firewall.setup()

    commands = [call.args[0] for call in run_cmd.call_args_list]
    mark_rules = [c for c in commands if 'MARK' in c]
    assert any('--sports' in c and '5000:5221,20561' in c and '-o' in c
               and 'eth0' in c for c in mark_rules)
    dscp_rules = [c for c in commands if 'DSCP' in c]
    assert any('--dports' in c and 'eth1' in c for c in dscp_rules)


def test_firewall_rejects_bad_game_ports_and_caps_list():
    firewall = Firewall('eth0', 'eth1', '192.168.4.1',
                        game_udp_ports='5000:5221,nope,70000,'
                        + ','.join(str(p) for p in range(1000, 1020)))
    assert 'nope' not in firewall.game_udp_ports
    assert '70000' not in firewall.game_udp_ports
    assert len(firewall.game_udp_ports) == 15


def test_firewall_without_game_ports_adds_no_marking():
    firewall = Firewall('eth0', 'eth1', '192.168.4.1', game_udp_ports='')

    with patch('network.firewall.open', mock_open()), \
            patch('network.firewall.run_cmd') as run_cmd:
        firewall.setup()

    commands = [call.args[0] for call in run_cmd.call_args_list]
    assert not any('MARK' in c or 'DSCP' in c for c in commands)


def test_qos_setup_creates_game_lane():
    from network.qos import GAME_CLASS_ID
    qos = QoSManager('eth0', 2048, 1024)
    with patch('network.qos.run_cmd') as run_cmd:
        qos.setup()

    commands = [call.args[0] for call in run_cmd.call_args_list]
    assert any(f'1:{GAME_CLASS_ID}' in c and 'htb' in c and 'prio' in c
               for c in commands)
    assert any('fw' in c and '0x67' in c and f'1:{GAME_CLASS_ID}' in c
               for c in commands)
