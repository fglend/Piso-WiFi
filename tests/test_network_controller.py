from unittest.mock import mock_open, patch

import pytest

from network.ap_manager import is_valid_mac
from network.firewall import Firewall
from network.qos import QoSManager
from network_controller import NetworkController
from tests.conftest import MAC


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
        args = mock_run.call_args_list[-1][0][0]
        assert 'ACCEPT' in args
    assert MAC in args


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
