"""Owned iptables chains for access control and captive HTTP redirection.

PISOWIFI handles LAN forwarding: blocked MAC DROP, paying MAC ACCEPT to the
uplink, protected AP ACCEPT to the uplink, then a final DROP. PISOWIFI_INPUT
allows only local DHCP, DNS, and portal traffic from the customer LAN.
PISOWIFI_PORTAL redirects unpaid HTTP while paid/protected devices RETURN.
"""
import logging
from functools import wraps
from threading import RLock

from network.command import command_exists, run_cmd

CHAIN = 'PISOWIFI'
CAPTIVE_CHAIN = 'PISOWIFI_PORTAL'
INPUT_CHAIN = 'PISOWIFI_INPUT'
GAME_CHAIN = 'PISOWIFI_GAME'
# fw mark consumed by the tc low-latency lane (see network/qos.py)
GAME_MARK = '0x67'
MAX_MULTIPORT_ENTRIES = 15

logger = logging.getLogger(__name__)


def _synchronized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


class Firewall:
    def __init__(self, ap_interface, internet_interface, ap_ip,
                 protected_devices=None, portal_port=5000,
                 game_udp_ports=''):
        self.ap_interface = ap_interface
        self.internet_interface = internet_interface
        self.ap_ip = ap_ip
        self.portal_port = int(portal_port)
        self.game_udp_ports = self._parse_game_ports(game_udp_ports)
        self.protected_devices = {
            mac.strip().upper(): ip.strip()
            for mac, ip in (protected_devices or {}).items()
        }
        self.protected_macs = frozenset(self.protected_devices)
        self._lock = RLock()
        self.logger = logger

    @_synchronized
    def setup(self):
        """Create the chain, NAT and static rules. Safe to run repeatedly."""
        # IP forwarding (written directly - no shell redirection)
        with open('/proc/sys/net/ipv4/ip_forward', 'w') as f:
            f.write('1')

        # Fresh chain
        run_cmd(['iptables', '-N', CHAIN], ignore_errors=True)
        run_cmd(['iptables', '-F', CHAIN])

        # Own only project-specific chains. Do not replace host-wide INPUT or
        # OUTPUT policy that may be managed by the operator.
        run_cmd(['iptables', '-t', 'filter', '-N', INPUT_CHAIN],
                ignore_errors=True)
        run_cmd(['iptables', '-t', 'filter', '-F', INPUT_CHAIN])
        input_jump = ['INPUT', '-i', self.ap_interface, '-j', INPUT_CHAIN]
        run_cmd(['iptables', '-D', *input_jump], ignore_errors=True)
        run_cmd(['iptables', '-I', 'INPUT', '1', *input_jump[1:]])
        self._add_input_rules()

        # The customer portal is LAN-facing. Do not expose its plain-HTTP
        # admin login on the ISP/uplink network.
        uplink_portal_rule = [
            'INPUT', '-i', self.internet_interface,
            '-p', 'tcp', '--dport', str(self.portal_port), '-j', 'DROP',
        ]
        run_cmd(['iptables', '-D', *uplink_portal_rule], ignore_errors=True)
        run_cmd(['iptables', '-I', 'INPUT', '1', *uplink_portal_rule[1:]])

        # Route all AP traffic through our chain (delete-then-insert keeps it single)
        run_cmd(['iptables', '-D', 'FORWARD', '-i', self.ap_interface, '-j', CHAIN],
                ignore_errors=True)
        run_cmd(['iptables', '-I', 'FORWARD', '1', '-i', self.ap_interface, '-j', CHAIN])

        # Replies enter from the uplink, not the LAN-side PISOWIFI chain. With
        # the global FORWARD policy set to DROP they need an explicit return
        # path for connections initiated by an allowed client.
        return_rule = [
            'FORWARD', '-i', self.internet_interface, '-o', self.ap_interface,
            '-m', 'state', '--state', 'ESTABLISHED,RELATED', '-j', 'ACCEPT',
        ]
        run_cmd(['iptables', '-D', *return_rule], ignore_errors=True)
        run_cmd(['iptables', '-I', 'FORWARD', '1', *return_rule[1:]])

        # NAT (delete-then-add keeps it idempotent)
        run_cmd(['iptables', '-t', 'nat', '-D', 'POSTROUTING',
                 '-o', self.internet_interface, '-j', 'MASQUERADE'], ignore_errors=True)
        run_cmd(['iptables', '-t', 'nat', '-A', 'POSTROUTING',
                 '-o', self.internet_interface, '-j', 'MASQUERADE'])

        # Unpaid clients are redirected only for plain HTTP. HTTPS is left
        # untouched because intercepting TLS would produce certificate errors.
        run_cmd(['iptables', '-t', 'nat', '-N', CAPTIVE_CHAIN],
                ignore_errors=True)
        run_cmd(['iptables', '-t', 'nat', '-F', CAPTIVE_CHAIN])
        captive_jump = [
            'PREROUTING', '-i', self.ap_interface,
            '-p', 'tcp', '--dport', '80', '-j', CAPTIVE_CHAIN,
        ]
        run_cmd(['iptables', '-t', 'nat', '-D', *captive_jump],
                ignore_errors=True)
        run_cmd(['iptables', '-t', 'nat', '-I', 'PREROUTING', '1',
                 *captive_jump[1:]])
        self._add_captive_rules([])

        self._add_static_rules()
        self._add_game_marking()
        self.logger.info(f"Firewall chain {CHAIN} initialized")

    def _parse_game_ports(self, spec):
        """Validate a comma-separated multiport spec ('5000:5221,20561')."""
        entries = []
        for raw in (spec or '').split(','):
            entry = raw.strip()
            if not entry:
                continue
            parts = entry.split(':')
            if len(parts) > 2 or not all(
                    part.isdigit() and 0 < int(part) <= 65535
                    for part in parts):
                logger.error("Ignoring invalid game port entry %r", entry)
                continue
            entries.append(entry)
        if len(entries) > MAX_MULTIPORT_ENTRIES:
            logger.warning(
                "Game port list truncated to %d entries (iptables multiport "
                "limit); dropped: %s", MAX_MULTIPORT_ENTRIES,
                ','.join(entries[MAX_MULTIPORT_ENTRIES:]))
            entries = entries[:MAX_MULTIPORT_ENTRIES]
        return entries

    def _add_game_marking(self):
        """Low-latency lane marking: game UDP replies to clients get a fw
        mark (tc lifts them past bulk traffic); game uploads to the internet
        get DSCP EF so upstream gear that honors DSCP can prioritize too.
        Bandwidth caps are unaffected - this changes queueing order only."""
        run_cmd(['iptables', '-t', 'mangle', '-N', GAME_CHAIN],
                ignore_errors=True)
        run_cmd(['iptables', '-t', 'mangle', '-F', GAME_CHAIN])
        game_jump = ['POSTROUTING', '-j', GAME_CHAIN]
        run_cmd(['iptables', '-t', 'mangle', '-D', *game_jump],
                ignore_errors=True)
        if not self.game_udp_ports:
            return
        run_cmd(['iptables', '-t', 'mangle', '-A', *game_jump])
        ports = ','.join(self.game_udp_ports)
        run_cmd(['iptables', '-t', 'mangle', '-A', GAME_CHAIN,
                 '-o', self.ap_interface, '-p', 'udp',
                 '-m', 'multiport', '--sports', ports,
                 '-j', 'MARK', '--set-mark', GAME_MARK])
        run_cmd(['iptables', '-t', 'mangle', '-A', GAME_CHAIN,
                 '-o', self.internet_interface, '-p', 'udp',
                 '-m', 'multiport', '--dports', ports,
                 '-j', 'DSCP', '--set-dscp-class', 'ef'])
        self.logger.info(
            "Game low-latency marking enabled for UDP ports %s", ports)

    def _add_static_rules(self):
        # Infrastructure devices must remain reachable even when there are no
        # paying clients. These rules are rebuilt by both setup() and sync().
        for mac, ip in sorted(self.protected_devices.items()):
            self._insert_protected_accept(mac, ip, append=True)
        # Nothing else from the customer LAN may be routed. DNS, DHCP, and the
        # portal terminate on the Pi and are handled by the INPUT chain below.
        run_cmd(['iptables', '-A', CHAIN, '-j', 'DROP'])

    def _add_input_rules(self):
        run_cmd(['iptables', '-A', INPUT_CHAIN, '-d', self.ap_ip,
                 '-p', 'udp', '--dport', '53', '-j', 'ACCEPT'])
        run_cmd(['iptables', '-A', INPUT_CHAIN, '-d', self.ap_ip,
                 '-p', 'tcp', '--dport', '53', '-j', 'ACCEPT'])
        run_cmd(['iptables', '-A', INPUT_CHAIN,
                 '-p', 'udp', '--dport', '67', '-j', 'ACCEPT'])
        run_cmd(['iptables', '-A', INPUT_CHAIN, '-d', self.ap_ip,
                 '-p', 'tcp', '--dport', str(self.portal_port), '-j', 'ACCEPT'])
        run_cmd(['iptables', '-A', INPUT_CHAIN, '-j', 'DROP'])

    def _delete_mac_rules(self, mac_address):
        run_cmd(['iptables', '-D', CHAIN, '-o', self.internet_interface,
                 '-m', 'mac', '--mac-source', mac_address, '-j', 'ACCEPT'],
                ignore_errors=True)
        run_cmd(['iptables', '-D', CHAIN, '-m', 'mac', '--mac-source', mac_address,
                 '-j', 'DROP'], ignore_errors=True)
        protected_ip = self.protected_devices.get(mac_address)
        if protected_ip:
            run_cmd(['iptables', '-D', CHAIN, '-s', protected_ip,
                     '-o', self.internet_interface, '-m', 'mac',
                     '--mac-source', mac_address, '-j', 'ACCEPT'],
                    ignore_errors=True)

    def _delete_captive_rules(self, mac_address):
        run_cmd(['iptables', '-t', 'nat', '-D', CAPTIVE_CHAIN,
                 '-m', 'mac', '--mac-source', mac_address, '-j', 'RETURN'],
                ignore_errors=True)
        protected_ip = self.protected_devices.get(mac_address)
        if protected_ip:
            run_cmd(['iptables', '-t', 'nat', '-D', CAPTIVE_CHAIN,
                     '-s', protected_ip, '-m', 'mac',
                     '--mac-source', mac_address, '-j', 'RETURN'],
                    ignore_errors=True)

    def _insert_captive_return(self, mac_address, ip_address=None,
                               append=False):
        operation = '-A' if append else '-I'
        position = [] if append else ['1']
        source = ['-s', ip_address] if ip_address else []
        run_cmd(['iptables', '-t', 'nat', operation, CAPTIVE_CHAIN,
                 *position, *source, '-m', 'mac',
                 '--mac-source', mac_address, '-j', 'RETURN'])

    def _add_captive_rules(self, allowed_macs):
        for mac, ip in sorted(self.protected_devices.items()):
            self._insert_captive_return(mac, ip, append=True)
        for mac in allowed_macs:
            self._insert_captive_return(mac)
        run_cmd(['iptables', '-t', 'nat', '-A', CAPTIVE_CHAIN,
                 '-p', 'tcp', '-j', 'REDIRECT',
                 '--to-ports', str(self.portal_port)])

    def _insert_protected_accept(self, mac_address, ip_address, append=False):
        operation = '-A' if append else '-I'
        position = [] if append else ['1']
        run_cmd(['iptables', operation, CHAIN, *position,
                 '-s', ip_address, '-o', self.internet_interface,
                 '-m', 'mac', '--mac-source', mac_address, '-j', 'ACCEPT'])

    @_synchronized
    def allow_mac(self, mac_address):
        normalized_mac = mac_address.strip().upper()
        try:
            self._delete_mac_rules(normalized_mac)
            self._delete_captive_rules(normalized_mac)
            protected_ip = self.protected_devices.get(normalized_mac)
            if protected_ip:
                self._insert_protected_accept(normalized_mac, protected_ip)
                self._insert_captive_return(normalized_mac, protected_ip)
                self.logger.info(
                    "Allowed protected MAC/IP %s/%s",
                    normalized_mac, protected_ip)
                return True
            run_cmd(['iptables', '-I', CHAIN, '1',
                     '-o', self.internet_interface, '-m', 'mac',
                     '--mac-source', normalized_mac, '-j', 'ACCEPT'])
            self._insert_captive_return(normalized_mac)
            self.logger.info(f"Allowed MAC {normalized_mac}")
            return True
        except Exception as e:
            self.logger.error(f"Error allowing MAC {mac_address}: {e}")
            return False

    @_synchronized
    def block_mac(self, mac_address):
        normalized_mac = mac_address.strip().upper()
        if normalized_mac in self.protected_macs:
            self.logger.warning(
                "Prevented firewall block of protected MAC %s", normalized_mac)
            return self.allow_mac(normalized_mac)
        try:
            self._delete_mac_rules(normalized_mac)
            self._delete_captive_rules(normalized_mac)
            run_cmd(['iptables', '-I', CHAIN, '1', '-m', 'mac',
                     '--mac-source', normalized_mac, '-j', 'DROP'])
            self.logger.info(f"Blocked MAC {normalized_mac}")
            return True
        except Exception as e:
            self.logger.error(f"Error blocking MAC {mac_address}: {e}")
            return False

    @_synchronized
    def flush_device_state(self, ip_address):
        """Drop kernel state bound to an IP after its owner changes or leaves.

        Phones that toggle MAC randomization reconnect with a new MAC but
        often reclaim their previous IP; a stale neighbor entry or conntrack
        flow bound to the dead MAC would blackhole them until it times out.
        Best-effort: never raises (conntrack may not be installed).
        """
        try:
            run_cmd(['ip', 'neigh', 'flush', 'dev', self.ap_interface,
                     'to', ip_address], ignore_errors=True)
            if command_exists('conntrack'):
                run_cmd(['conntrack', '-D', '-s', ip_address],
                        ignore_errors=True)
                run_cmd(['conntrack', '-D', '-d', ip_address],
                        ignore_errors=True)
            self.logger.info("Flushed neighbor/conntrack state for %s",
                             ip_address)
        except Exception as e:
            self.logger.warning(
                "Could not flush device state for %s: %s", ip_address, e)

    @_synchronized
    def sync(self, allowed_macs):
        """Rebuild the chain from the given allow-list (e.g. after a restart)."""
        try:
            run_cmd(['iptables', '-F', CHAIN])
            self._add_static_rules()
            run_cmd(['iptables', '-t', 'nat', '-F', CAPTIVE_CHAIN])
            self._add_captive_rules(allowed_macs)
            for mac in allowed_macs:
                run_cmd(['iptables', '-I', CHAIN, '1',
                         '-o', self.internet_interface, '-m', 'mac',
                         '--mac-source', mac, '-j', 'ACCEPT'])
            self.logger.info(f"Firewall synced: {len(allowed_macs)} device(s) allowed")
            return True
        except Exception as e:
            self.logger.error(f"Error syncing firewall: {e}")
            return False
