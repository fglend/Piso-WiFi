from threading import Event, Thread
from unittest.mock import mock_open, patch

import pytest

from network.ap_manager import is_valid_mac
from network.firewall import Firewall
from network.qos import QoSManager
from network_controller import NetworkController
from tests.conftest import MAC

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
        '-j', 'REDIRECT', '--to-ports', '5000',
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
