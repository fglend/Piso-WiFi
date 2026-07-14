"""hostapd / dnsmasq lifecycle: config generation, start/stop, status and
connected-station discovery."""
import logging
import os
import re
import time

from network.command import run_cmd, command_exists

logger = logging.getLogger(__name__)

MAC_RE = re.compile(r'^([0-9A-F]{2}:){5}[0-9A-F]{2}$')
DNSMASQ_LEASES = '/var/lib/misc/dnsmasq.leases'


def is_valid_mac(mac):
    try:
        return bool(MAC_RE.match(mac.upper()))
    except AttributeError:
        return False


class APManager:
    def __init__(self, settings):
        self.settings = settings
        self.ap_interface = settings.ap_interface
        self.internet_interface = settings.internet_interface
        self.ssid = settings.ap_ssid
        self.ip = settings.ap_ip
        self.hostapd_conf = '/etc/hostapd/hostapd.conf'
        self.dnsmasq_conf = '/etc/dnsmasq.conf'
        self.logger = logger

    def verify_requirements(self):
        if os.geteuid() != 0:
            raise Exception("Must run as root")
        if not os.path.exists(f"/sys/class/net/{self.ap_interface}"):
            raise Exception(f"Interface {self.ap_interface} does not exist")
        for cmd in ('hostapd', 'dnsmasq', 'iw', 'ip', 'iptables', 'tc'):
            if not command_exists(cmd):
                raise Exception(f"Required command '{cmd}' not found")
        try:
            iw_output = run_cmd(['iw', 'list'])
            if 'AP' not in iw_output:
                raise Exception(f"Interface {self.ap_interface} does not support AP mode")
        except Exception as e:
            self.logger.warning(f"Could not verify AP mode support: {e}")
        self.logger.info("System requirements verified")

    def write_configs(self):
        os.makedirs('/etc/hostapd', exist_ok=True)

        hostapd_config = f"""
# Interface configuration
interface={self.ap_interface}
driver=nl80211
ssid={self.ssid}

# Hardware configuration
hw_mode=g
channel=7
ieee80211n=1
wmm_enabled=0

# Open network configuration
auth_algs=1
ignore_broadcast_ssid=0

# Debugging
logger_syslog=-1
logger_syslog_level=2
logger_stdout=-1
logger_stdout_level=2

# Stability settings
beacon_int=100
dtim_period=2
max_num_sta=10
rts_threshold=2347
fragm_threshold=2346
"""
        with open(self.hostapd_conf, 'w') as f:
            f.write(hostapd_config.strip())
        os.chmod(self.hostapd_conf, 0o644)

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
host-record={s.portal_hostname},{self.ip}
server=8.8.8.8
server=8.8.4.4

# Logging
log-queries
log-dhcp
"""
        with open(self.dnsmasq_conf, 'w') as f:
            f.write(dnsmasq_config.strip())
        os.chmod(self.dnsmasq_conf, 0o644)
        self.logger.info("AP configuration written")

    def start(self):
        run_cmd(['killall', 'hostapd'], ignore_errors=True)
        run_cmd(['killall', 'dnsmasq'], ignore_errors=True)
        time.sleep(1)

        run_cmd(['nmcli', 'device', 'set', self.ap_interface, 'managed', 'no'],
                ignore_errors=True)

        run_cmd(['rfkill', 'unblock', 'wifi'])
        run_cmd(['ip', 'link', 'set', self.ap_interface, 'down'])
        run_cmd(['iw', 'dev', self.ap_interface, 'set', 'type', '__ap'])
        run_cmd(['ip', 'addr', 'flush', 'dev', self.ap_interface])
        run_cmd(['ip', 'addr', 'add', f'{self.ip}/24', 'dev', self.ap_interface])
        run_cmd(['ip', 'link', 'set', self.ap_interface, 'up'])
        time.sleep(1)

        run_cmd(['hostapd', '-B', '-P', '/run/hostapd.pid', self.hostapd_conf])
        time.sleep(2)

        run_cmd(['systemctl', 'restart', 'dnsmasq'])

        if not self.is_hostapd_running():
            raise Exception("Hostapd failed to start")
        self.logger.info(f"Started WiFi Access Point: {self.ssid}")

    def stop(self):
        try:
            run_cmd(['systemctl', 'stop', 'hostapd'], ignore_errors=True)
            run_cmd(['killall', 'hostapd'], ignore_errors=True)
            run_cmd(['systemctl', 'stop', 'dnsmasq'], ignore_errors=True)
            run_cmd(['ip', 'link', 'set', self.ap_interface, 'down'], ignore_errors=True)
            run_cmd(['nmcli', 'device', 'set', self.ap_interface, 'managed', 'yes'],
                    ignore_errors=True)
            run_cmd(['nmcli', 'device', 'set', self.internet_interface, 'managed', 'yes'],
                    ignore_errors=True)
            self.logger.info("WiFi Access Point stopped")
        except Exception as e:
            self.logger.error(f"Failed to stop WiFi Access Point: {e}")

    def is_hostapd_running(self):
        try:
            if not run_cmd(['pgrep', 'hostapd'], ignore_errors=True).strip():
                self.logger.error("No hostapd process found")
                return False
            status = run_cmd(['hostapd_cli', 'status'], ignore_errors=True)
            if status and 'state=ENABLED' not in status:
                self.logger.error("Hostapd is not in ENABLED state")
                return False
            iw_info = run_cmd(['iw', 'dev', self.ap_interface, 'info'])
            if 'type AP' not in iw_info:
                self.logger.error("Interface not in AP mode")
                return False
            return True
        except Exception as e:
            self.logger.error(f"Error checking hostapd: {e}")
            return False

    def check_status(self):
        try:
            if not self.is_hostapd_running():
                return False
            interface_status = run_cmd(['ip', 'addr', 'show', self.ap_interface])
            if 'UP' not in interface_status or self.ip not in interface_status:
                self.logger.error(f"Interface {self.ap_interface} is down or missing {self.ip}")
                return False
            dnsmasq_status = run_cmd(['systemctl', 'status', 'dnsmasq'], ignore_errors=True)
            if 'running' not in dnsmasq_status:
                self.logger.error("Dnsmasq is not running")
                return False
            return True
        except Exception as e:
            self.logger.error(f"Error checking AP status: {e}")
            return False

    def get_dhcp_leases(self):
        """Return {MAC: {'ip', 'hostname', 'lease_expiry'}} for active leases."""
        leases = {}
        try:
            if not os.path.exists(DNSMASQ_LEASES):
                return leases
            subnet_prefix = '.'.join(self.ip.split('.')[:3]) + '.'
            now = int(time.time())
            with open(DNSMASQ_LEASES) as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 5:
                        lease_expiry = int(parts[0])
                        mac = parts[1].upper()
                        ip = parts[2]
                        hostname = parts[3] if parts[3] != '*' else 'Unknown'
                        if lease_expiry > now and ip.startswith(subnet_prefix):
                            leases[mac] = {'ip': ip, 'hostname': hostname,
                                           'lease_expiry': lease_expiry}
        except Exception as e:
            self.logger.warning(f"DHCP leases check failed: {e}")
        return leases

    def get_stations(self):
        """Return list of station dicts from `iw station dump`, enriched with
        DHCP lease info and signal strength."""
        stations = []
        dhcp_info = self.get_dhcp_leases()
        try:
            result = run_cmd(['iw', 'dev', self.ap_interface, 'station', 'dump'])
            for line in result.split('\n'):
                if 'Station' in line:
                    mac = line.split()[1].upper()
                    if not is_valid_mac(mac):
                        continue
                    info = {
                        'mac_address': mac,
                        'ip': dhcp_info.get(mac, {}).get('ip', 'Unknown'),
                        'hostname': dhcp_info.get(mac, {}).get('hostname', 'Unknown'),
                        'connected': True,
                    }
                    try:
                        detail = run_cmd(['iw', 'dev', self.ap_interface,
                                          'station', 'get', mac])
                        signal = re.search(r"signal:\s*([-\d]+)\s*dBm", detail)
                        if signal:
                            info['signal'] = f"{signal.group(1)} dBm"
                    except Exception as e:
                        self.logger.debug(f"Could not get signal info for {mac}: {e}")
                    stations.append(info)
        except Exception as e:
            self.logger.warning(f"IW station dump failed: {e}")
        return stations

    def resolve_mac(self, ip_address):
        """Find the MAC currently holding an IP (DHCP leases, then neighbors)."""
        for mac, lease in self.get_dhcp_leases().items():
            if lease['ip'] == ip_address:
                return mac
        try:
            output = run_cmd(['ip', 'neigh'])
            for line in output.splitlines():
                parts = line.split()
                if parts and parts[0] == ip_address and 'lladdr' in parts:
                    return parts[parts.index('lladdr') + 1].upper()
        except Exception as e:
            self.logger.debug(f"Neighbor lookup failed for {ip_address}: {e}")
        return None

    def resolve_ip(self, mac_address):
        """Find the current IP for a MAC via DHCP leases, then the neighbor table."""
        lease = self.get_dhcp_leases().get(mac_address.upper())
        if lease:
            return lease['ip']
        try:
            output = run_cmd(['ip', 'neigh'])
            for line in output.splitlines():
                if mac_address.lower() in line.lower():
                    return line.split()[0]
        except Exception as e:
            self.logger.debug(f"Neighbor lookup failed for {mac_address}: {e}")
        return None
