"""Wired gateway mode: the Pi has no WiFi radio; an external AP / PoE router
in bridge mode hangs off the LAN interface. We run DHCP, firewall and QoS on
that interface and NAT out the internet interface - no hostapd involved.

Clients are discovered from active DHCP leases confirmed by the kernel
neighbor table (there is no `iw station dump` on Ethernet).
"""
import logging
import os

from network.ap_manager import APManager
from network.command import run_cmd, command_exists

logger = logging.getLogger(__name__)

# Neighbor states that count as "present on the LAN"
ACTIVE_NEIGH_STATES = {'REACHABLE', 'STALE', 'DELAY', 'PROBE'}


class WiredGateway(APManager):
    def verify_requirements(self):
        if os.geteuid() != 0:
            raise Exception("Must run as root")
        if not os.path.exists(f"/sys/class/net/{self.ap_interface}"):
            raise Exception(f"LAN interface {self.ap_interface} does not exist")
        for cmd in ('dnsmasq', 'ip', 'iptables', 'tc'):
            if not command_exists(cmd):
                raise Exception(f"Required command '{cmd}' not found")
        self.logger.info("System requirements verified (wired gateway mode)")

    def write_configs(self):
        # Same dnsmasq policy as AP mode (shared template); no hostapd here.
        self._write_dnsmasq_conf()
        self.logger.info("Wired gateway configuration written")

    def start(self):
        if command_exists('nmcli'):
            run_cmd(['nmcli', 'device', 'set', self.ap_interface, 'managed', 'no'],
                    ignore_errors=True)
        run_cmd(['ip', 'addr', 'flush', 'dev', self.ap_interface])
        run_cmd(['ip', 'addr', 'add', f'{self.ip}/24', 'dev', self.ap_interface])
        run_cmd(['ip', 'link', 'set', self.ap_interface, 'up'])
        run_cmd(['systemctl', 'restart', 'dnsmasq'])
        self.logger.info(f"Wired gateway up on {self.ap_interface} ({self.ip})")

    def stop(self):
        run_cmd(['systemctl', 'stop', 'dnsmasq'], ignore_errors=True)
        if command_exists('nmcli'):
            run_cmd(['nmcli', 'device', 'set', self.ap_interface, 'managed', 'yes'],
                    ignore_errors=True)
        self.logger.info("Wired gateway stopped")

    def is_hostapd_running(self):
        return True  # not applicable in wired mode

    def check_status(self):
        try:
            interface_status = run_cmd(['ip', 'addr', 'show', self.ap_interface])
            if 'UP' not in interface_status or self.ip not in interface_status:
                self.logger.error(
                    f"Interface {self.ap_interface} is down or missing {self.ip}")
                return False
            dnsmasq_status = run_cmd(['systemctl', 'status', 'dnsmasq'],
                                     ignore_errors=True)
            if 'running' not in dnsmasq_status:
                self.logger.error("Dnsmasq is not running")
                return False
            return True
        except Exception as e:
            self.logger.error(f"Error checking gateway status: {e}")
            return False

    def _neighbor_states(self):
        """{MAC: state} for entries on the LAN interface."""
        states = {}
        try:
            output = run_cmd(['ip', 'neigh', 'show', 'dev', self.ap_interface])
            for line in output.splitlines():
                parts = line.split()
                if 'lladdr' in parts:
                    mac = parts[parts.index('lladdr') + 1].upper()
                    states[mac] = parts[-1]
        except Exception as e:
            self.logger.warning(f"Neighbor table read failed: {e}")
            return None
        return states

    def get_stations(self):
        stations = []
        neigh = self._neighbor_states()
        if neigh is None:
            raise RuntimeError("Could not read the LAN neighbor table")
        for mac, lease in self.get_dhcp_leases(strict=True).items():
            state = neigh.get(mac)
            if state in ACTIVE_NEIGH_STATES:
                stations.append({
                    'mac_address': mac,
                    'ip': lease['ip'],
                    'hostname': lease['hostname'],
                    'connected': True,
                })
        return stations
