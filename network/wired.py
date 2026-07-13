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
        s = self.settings
        dnsmasq_config = f"""
# Interface configuration
interface={self.ap_interface}
no-dhcp-interface=lo
bind-interfaces

# DHCP server configuration
dhcp-range={s.dhcp_range_start},{s.dhcp_range_end},{s.network_mask},24h
dhcp-option=option:router,{self.ip}
dhcp-option=option:dns-server,{self.ip}
dhcp-option=option:netmask,{s.network_mask}

# DNS configuration
no-resolv
no-poll
server=8.8.8.8
server=8.8.4.4

# Logging
log-dhcp
"""
        with open(self.dnsmasq_conf, 'w') as f:
            f.write(dnsmasq_config.strip())
        os.chmod(self.dnsmasq_conf, 0o644)
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
        return states

    def get_stations(self):
        stations = []
        neigh = self._neighbor_states()
        for mac, lease in self.get_dhcp_leases().items():
            state = neigh.get(mac)
            if state in ACTIVE_NEIGH_STATES:
                stations.append({
                    'mac_address': mac,
                    'ip': lease['ip'],
                    'hostname': lease['hostname'],
                    'connected': True,
                })
        return stations
