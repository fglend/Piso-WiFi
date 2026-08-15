from threading import Event, Thread
from unittest.mock import MagicMock, mock_open, patch

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


def test_reassociation_flagged_when_connected_seconds_resets(network_controller):
    """The core duplicate-MAC/IP signal: a still-connected MAC's Wi-Fi
    association age going backwards means some radio just (re)associated
    presenting that MAC - see NetworkController._check_reassociation."""
    events = []
    network_controller.on_new_device = lambda mac: None
    network_controller.on_reassociation = (
        lambda mac, seconds, prior: events.append((mac, seconds, prior)))
    stations_seq = [
        [{'mac_address': MAC, 'ip': '192.168.4.20', 'hostname': 'phone',
          'connected': True, 'connected_seconds': 600}],
        [{'mac_address': MAC, 'ip': '192.168.4.20', 'hostname': 'phone',
          'connected': True, 'connected_seconds': 3}],
    ]
    with patch.object(network_controller.ap, 'get_stations',
                      side_effect=stations_seq):
        network_controller.get_connected_devices()
        network_controller.get_connected_devices()

    assert events == [(MAC, 3, 600)]


def test_reassociation_not_flagged_when_connected_seconds_grows(network_controller):
    events = []
    network_controller.on_new_device = lambda mac: None
    network_controller.on_reassociation = (
        lambda mac, seconds, prior: events.append((mac, seconds, prior)))
    stations_seq = [
        [{'mac_address': MAC, 'ip': '192.168.4.20', 'hostname': 'phone',
          'connected': True, 'connected_seconds': 15}],
        [{'mac_address': MAC, 'ip': '192.168.4.20', 'hostname': 'phone',
          'connected': True, 'connected_seconds': 30}],
    ]
    with patch.object(network_controller.ap, 'get_stations',
                      side_effect=stations_seq):
        network_controller.get_connected_devices()
        network_controller.get_connected_devices()

    assert events == []


def test_reassociation_not_flagged_without_connected_seconds_data(network_controller):
    """WiredGateway stations carry no Wi-Fi association age - must not crash
    or false-positive on missing data."""
    events = []
    network_controller.on_new_device = lambda mac: None
    network_controller.on_reassociation = (
        lambda mac, seconds, prior: events.append((mac, seconds, prior)))
    station = {'mac_address': MAC, 'ip': '192.168.4.20',
               'hostname': 'phone', 'connected': True}
    with patch.object(network_controller.ap, 'get_stations',
                      side_effect=[[station], [station]]):
        network_controller.get_connected_devices()
        network_controller.get_connected_devices()

    assert events == []


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


def test_get_stations_parses_connected_time(settings):
    """connected_seconds drives duplicate-address detection in
    NetworkController._check_reassociation - it must survive real `iw`
    formatting (tab-separated, trailing "seconds")."""
    manager = APManager(settings)
    dump = (
        "Station 00:11:22:33:44:55 (on wlan0)\n"
        "\tinactive time:\t10 ms\n"
        "\tsignal:  \t-55 dBm\n"
        "\tconnected time:\t3600 seconds\n"
        "Station 11:22:33:44:55:66 (on wlan0)\n"
        "\tsignal:  \t-71 dBm\n"
    )
    with patch.object(manager, 'get_dhcp_leases', return_value={}), \
            patch('network.ap_manager.run_cmd', return_value=dump):
        stations = manager.get_stations()

    assert stations[0]['connected_seconds'] == 3600
    # No "connected time" line for the second station - stays None, not KeyError.
    assert stations[1]['connected_seconds'] is None


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


def test_firewall_rejects_bad_game_ports_and_chunks_rules():
    firewall = Firewall('eth0', 'eth1', '192.168.4.1',
                        game_udp_ports='5000:5221,nope,70000,'
                        + ','.join(str(p) for p in range(1000, 1020)))
    assert 'nope' not in firewall.game_udp_ports
    assert '70000' not in firewall.game_udp_ports
    # Nothing valid is dropped; oversized lists become multiple rules
    assert len(firewall.game_udp_ports) == 21

    with patch('network.firewall.open', mock_open()), \
            patch('network.firewall.run_cmd') as run_cmd:
        firewall.setup()
    mark_rules = [call.args[0] for call in run_cmd.call_args_list
                  if 'MARK' in call.args[0]]
    assert len(mark_rules) == 2
    # A range costs two multiport slots; every rule must fit the limit of 15
    for rule in mark_rules:
        ports = rule[rule.index('--sports') + 1].split(',')
        assert sum(2 if ':' in p else 1 for p in ports) <= 15


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


# --- multi-AP readiness (PoE switch feeding several APs) ---------------------

SECOND_AP_MAC = "AA:BB:CC:DD:EE:02"
SECOND_AP_IP = "192.168.4.3"
SWITCH_MAC = "AA:BB:CC:DD:EE:03"
SWITCH_IP = "192.168.4.4"


def test_legacy_single_poe_ap_still_works(settings):
    """An existing single-AP .env must keep working with no edit."""
    settings.network_mode = 'wired'
    settings.poe_ap_mac_address = POE_AP_MAC
    settings.poe_ap_ip_address = POE_AP_IP

    assert settings.protected_device_map() == {POE_AP_MAC: POE_AP_IP}


def test_protected_devices_spec_adds_more_infrastructure(settings):
    settings.poe_ap_mac_address = POE_AP_MAC
    settings.poe_ap_ip_address = POE_AP_IP
    settings.protected_devices_spec = (
        f'{SECOND_AP_MAC}={SECOND_AP_IP},{SWITCH_MAC}={SWITCH_IP}')

    assert settings.protected_device_map() == {
        POE_AP_MAC: POE_AP_IP,
        SECOND_AP_MAC: SECOND_AP_IP,
        SWITCH_MAC: SWITCH_IP,
    }


def test_protected_devices_works_without_the_legacy_pair(settings):
    settings.protected_devices_spec = f'{SECOND_AP_MAC}={SECOND_AP_IP}'

    assert settings.protected_device_map() == {SECOND_AP_MAC: SECOND_AP_IP}


def test_protected_devices_is_case_insensitive_and_tolerates_spacing(settings):
    settings.protected_devices_spec = (
        f'  {SECOND_AP_MAC.lower()} = {SECOND_AP_IP} , ')

    assert settings.protected_device_map() == {SECOND_AP_MAC: SECOND_AP_IP}


@pytest.mark.parametrize('spec', [
    'not-a-mac=192.168.4.3',
    f'{SECOND_AP_MAC}',                      # missing =IP
    f'{SECOND_AP_MAC}=',                     # missing IP
    f'{SECOND_AP_MAC}={SECOND_AP_IP},{SECOND_AP_MAC}={SWITCH_IP}',  # duplicate
])
def test_malformed_protected_devices_refuse_to_start(settings, spec):
    settings.protected_devices_spec = spec

    # Silently dropping an AP would leave it blocked by the terminal DROP,
    # which looks like a hardware fault. Fail loudly instead.
    with pytest.raises(RuntimeError):
        settings.validate()


@pytest.mark.parametrize('bad_ip', [
    '10.0.0.5',        # off the client LAN
    '192.168.4.1',     # AP_IP itself
    '192.168.4.10',    # inside the DHCP range
])
def test_protected_device_ip_gets_the_same_checks_as_the_poe_ap(settings, bad_ip):
    settings.ap_ip = '192.168.4.1'
    settings.network_mask = '255.255.255.0'
    settings.dhcp_range_start = '192.168.4.10'
    settings.dhcp_range_end = '192.168.4.200'
    settings.protected_devices_spec = f'{SECOND_AP_MAC}={bad_ip}'

    with pytest.raises(RuntimeError):
        settings.validate()


def test_controller_protects_every_configured_device(settings):
    settings.network_mode = 'wired'
    settings.poe_ap_mac_address = POE_AP_MAC
    settings.poe_ap_ip_address = POE_AP_IP
    settings.protected_devices_spec = (
        f'{SECOND_AP_MAC}={SECOND_AP_IP},{SWITCH_MAC}={SWITCH_IP}')

    controller = NetworkController(settings, manage_hardware=False)

    assert controller.trusted_macs == frozenset(
        {POE_AP_MAC, SECOND_AP_MAC, SWITCH_MAC})
    assert controller.firewall.protected_devices == {
        POE_AP_MAC: POE_AP_IP,
        SECOND_AP_MAC: SECOND_AP_IP,
        SWITCH_MAC: SWITCH_IP,
    }


def test_protected_devices_ignored_when_the_pi_runs_its_own_radio(settings):
    settings.network_mode = 'ap'
    settings.protected_devices_spec = f'{SECOND_AP_MAC}={SECOND_AP_IP}'

    controller = NetworkController(settings, manage_hardware=False)

    # No external AP to protect when hostapd owns the radio.
    assert controller.trusted_macs == frozenset()


def test_controller_drops_a_bad_entry_without_taking_the_gateway_down(settings):
    settings.network_mode = 'wired'
    settings.protected_devices_spec = f'{SECOND_AP_MAC}={SECOND_AP_IP}'
    controller = NetworkController(settings, manage_hardware=False)
    assert controller.trusted_macs == frozenset({SECOND_AP_MAC})

    settings.protected_devices_spec = 'garbage'
    controller = NetworkController(settings, manage_hardware=False)

    # validate() would have caught this at startup; a caller that skipped it
    # still gets a running gateway rather than an exception.
    assert controller.trusted_macs == frozenset()


# --- tethering block (hotspot / USB tether sharing one paid MAC) -------------

def _setup_commands(firewall, have_ip6tables=True):
    with patch('network.firewall.open', mock_open()), \
            patch('network.firewall.command_exists',
                  lambda name: have_ip6tables or name != 'ip6tables'), \
            patch('network.firewall.run_cmd') as mock_run:
        firewall.setup()
    return [call.args[0] for call in mock_run.call_args_list]


def test_tethering_block_is_off_by_default():
    commands = _setup_commands(Firewall('eth0', 'eth1', '192.168.4.1'))

    # The chain is still created and flushed so a previous run is undone,
    # but nothing is dropped and no jump is installed.
    assert ['iptables', '-t', 'mangle', '-F', 'PISOWIFI_TETHER'] in commands
    assert not [c for c in commands if '--ttl-eq' in c]
    assert ['iptables', '-t', 'mangle', '-I', 'PREROUTING', '1',
            '-i', 'eth0', '-j', 'PISOWIFI_TETHER'] not in commands


def test_tethering_block_drops_decremented_ttls_when_enabled():
    firewall = Firewall('eth0', 'eth1', '192.168.4.1', block_tethering=True)

    commands = _setup_commands(firewall)

    for ttl in (63, 127):
        assert ['iptables', '-t', 'mangle', '-A', 'PISOWIFI_TETHER', '-m', 'ttl',
                '--ttl-eq', str(ttl), '-j', 'DROP'] in commands


def test_tether_check_runs_in_mangle_prerouting_not_filter_forward():
    """The whole feature depends on running before the kernel's own TTL hit.

    ip_forward() decrements a packet's TTL before the filter/FORWARD hook
    ever sees it, so a filter/FORWARD match on 63/127 would catch every
    forwarded packet - direct customers included, not just tethered ones.
    mangle/PREROUTING runs before the routing decision (and that decrement),
    so it sees the TTL exactly as the client's OS sent it.
    """
    firewall = Firewall('eth0', 'eth1', '192.168.4.1', block_tethering=True)

    commands = _setup_commands(firewall)
    tether_jump = ['iptables', '-t', 'mangle', '-I', 'PREROUTING', '1',
                   '-i', 'eth0', '-j', 'PISOWIFI_TETHER']

    assert tether_jump in commands
    assert not [c for c in commands
                if 'PISOWIFI_TETHER' in c and 'FORWARD' in c]


def test_tether_rules_are_added_after_the_chain_jump():
    """Rules must be appended only once the jump exists, and the chain flushed
    first so setup() stays idempotent."""
    firewall = Firewall('eth0', 'eth1', '192.168.4.1', block_tethering=True)

    commands = _setup_commands(firewall)
    flush = commands.index(['iptables', '-t', 'mangle', '-F', 'PISOWIFI_TETHER'])
    first_drop = min(i for i, c in enumerate(commands) if '--ttl-eq' in c)

    assert flush < first_drop


def test_custom_ttl_list_is_honoured():
    firewall = Firewall('eth0', 'eth1', '192.168.4.1',
                        block_tethering=True, tethering_ttls='63, 254')

    commands = _setup_commands(firewall)

    assert firewall.tethering_ttls == [63, 254]
    assert ['iptables', '-t', 'mangle', '-A', 'PISOWIFI_TETHER', '-m', 'ttl',
            '--ttl-eq', '254', '-j', 'DROP'] in commands
    assert not [c for c in commands if '127' in c and '--ttl-eq' in c]


@pytest.mark.parametrize('spec,expected', [
    ('', [63, 127]),               # blank means defaults, not "block nothing"
    (None, [63, 127]),
    ('63,63', [63]),               # de-duplicated
    ('63,garbage,127', [63, 127]),  # bad entries skipped, good ones kept
    ('0,256,-1', [63, 127]),       # all invalid -> fall back to defaults
])
def test_ttl_spec_parsing(spec, expected):
    firewall = Firewall('eth0', 'eth1', '192.168.4.1', tethering_ttls=spec)
    assert firewall.tethering_ttls == expected


def test_ipv6_hop_limit_is_blocked_too():
    firewall = Firewall('eth0', 'eth1', '192.168.4.1', block_tethering=True)

    commands = _setup_commands(firewall, have_ip6tables=True)

    # Only v4 forwarding is configured today, but a v4-only rule would leave
    # the entire bypass open the moment the operator enables IPv6.
    assert ['ip6tables', '-t', 'mangle', '-A', 'PISOWIFI_TETHER', '-m', 'hl',
            '--hl-eq', '63', '-j', 'DROP'] in commands


def test_missing_ip6tables_does_not_break_setup():
    firewall = Firewall('eth0', 'eth1', '192.168.4.1', block_tethering=True)

    commands = _setup_commands(firewall, have_ip6tables=False)

    assert not [c for c in commands if c and c[0] == 'ip6tables']
    # v4 protection still applies.
    assert ['iptables', '-t', 'mangle', '-A', 'PISOWIFI_TETHER', '-m', 'ttl',
            '--ttl-eq', '63', '-j', 'DROP'] in commands


def test_tethering_failure_never_takes_the_gateway_down():
    firewall = Firewall('eth0', 'eth1', '192.168.4.1', block_tethering=True)

    with patch('network.firewall.open', mock_open()), \
            patch('network.firewall.command_exists', lambda name: False), \
            patch('network.firewall.run_cmd') as mock_run:
        # Fail only the tether chain's rule additions.
        def fail_tether(args, **kwargs):
            if 'PISOWIFI_TETHER' in args and '-A' in args:
                raise RuntimeError('iptables build lacks the ttl match')
            return ''
        mock_run.side_effect = fail_tether
        firewall.setup()   # must not raise

    commands = [call.args[0] for call in mock_run.call_args_list]
    assert ['iptables', '-A', 'PISOWIFI', '-j', 'DROP'] in commands


def test_controller_passes_the_tethering_setting_through(settings):
    settings.block_tethering = True
    settings.tethering_blocked_ttls = '63'

    controller = NetworkController(settings, manage_hardware=False)

    assert controller.firewall.block_tethering is True
    assert controller.firewall.tethering_ttls == [63]


# --- clearing unpaid connected devices ---------------------------------------

def _seeded_controller(settings, macs_with_ips):
    controller = NetworkController(settings, manage_hardware=False)
    controller.firewall = MagicMock()
    controller.ap = MagicMock()
    controller._known_devices = {
        mac: {'mac_address': mac, 'ip': ip} for mac, ip in macs_with_ips.items()}
    controller._absence_counts = {mac: 1 for mac in macs_with_ips}
    controller.connected_devices = set(macs_with_ips)
    return controller


def test_forget_devices_blocks_flushes_and_drops_them(settings):
    controller = _seeded_controller(settings, {MAC: '192.168.4.7'})

    assert controller.forget_devices([MAC]) == [MAC]

    controller.firewall.block_mac.assert_called_once_with(MAC)
    controller.firewall.flush_device_state.assert_called_once_with('192.168.4.7')
    # All three pieces of state must go, or the row returns on the next poll.
    assert MAC not in controller._known_devices
    assert MAC not in controller._absence_counts
    assert MAC not in controller.connected_devices


def test_forget_devices_leaves_other_devices_alone(settings):
    controller = _seeded_controller(
        settings, {MAC: '192.168.4.7', OTHER_MAC: '192.168.4.8'})

    controller.forget_devices([MAC])

    assert OTHER_MAC in controller._known_devices
    assert OTHER_MAC in controller.connected_devices


def test_forget_devices_never_touches_protected_infrastructure(settings):
    settings.network_mode = 'wired'
    settings.poe_ap_mac_address = POE_AP_MAC
    settings.poe_ap_ip_address = POE_AP_IP
    controller = _seeded_controller(settings, {POE_AP_MAC: POE_AP_IP})

    # Blocking the AP would take every customer offline at once.
    assert controller.forget_devices([POE_AP_MAC]) == []
    controller.firewall.block_mac.assert_not_called()
    assert POE_AP_MAC in controller._known_devices


def test_forget_devices_normalises_and_deduplicates_macs(settings):
    controller = _seeded_controller(settings, {MAC: '192.168.4.7'})

    assert controller.forget_devices([MAC.lower(), f'  {MAC}  ']) == [MAC]
    assert controller.firewall.block_mac.call_count == 1


def test_forget_devices_continues_when_a_block_fails(settings):
    controller = _seeded_controller(
        settings, {MAC: '192.168.4.7', OTHER_MAC: '192.168.4.8'})
    controller.firewall.block_mac.side_effect = [RuntimeError('iptables busy'), None]

    forgotten = controller.forget_devices([MAC, OTHER_MAC])

    # One bad device must not strand the rest in the list.
    assert set(forgotten) == {MAC, OTHER_MAC}
    assert controller._known_devices == {}


def test_forget_devices_handles_a_device_with_no_known_ip(settings):
    controller = _seeded_controller(settings, {MAC: None})

    assert controller.forget_devices([MAC]) == [MAC]
    # _flush_stale_state short-circuits on a missing IP rather than raising.
    controller.firewall.flush_device_state.assert_not_called()
