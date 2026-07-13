"""iptables access control scoped to a dedicated PISOWIFI chain.

Rule order inside the chain (first match wins):
  1. per-MAC DROP rules (blocked devices, inserted at the top)
  2. per-MAC ACCEPT rules (paying devices)
  3. static service rules (DNS, DHCP, portal access)
  4. ESTABLISHED,RELATED accept (last, so a fresh DROP cuts existing flows)
Everything that falls through the chain is dropped by the FORWARD policy.
"""
import logging

from network.command import run_cmd

CHAIN = 'PISOWIFI'

logger = logging.getLogger(__name__)


class Firewall:
    def __init__(self, ap_interface, internet_interface, ap_ip):
        self.ap_interface = ap_interface
        self.internet_interface = internet_interface
        self.ap_ip = ap_ip
        self.logger = logger

    def setup(self):
        """Create the chain, NAT and static rules. Safe to run repeatedly."""
        # IP forwarding (written directly - no shell redirection)
        with open('/proc/sys/net/ipv4/ip_forward', 'w') as f:
            f.write('1')

        # Fresh chain
        run_cmd(['iptables', '-N', CHAIN], ignore_errors=True)
        run_cmd(['iptables', '-F', CHAIN])

        # Default policies
        run_cmd(['iptables', '-P', 'FORWARD', 'DROP'])
        run_cmd(['iptables', '-P', 'INPUT', 'ACCEPT'])
        run_cmd(['iptables', '-P', 'OUTPUT', 'ACCEPT'])

        # The customer portal is LAN-facing. Do not expose its plain-HTTP
        # admin login on the ISP/uplink network.
        uplink_portal_rule = [
            'INPUT', '-i', self.internet_interface,
            '-p', 'tcp', '--dport', '5000', '-j', 'DROP',
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

        self._add_static_rules()
        self.logger.info(f"Firewall chain {CHAIN} initialized")

    def _add_static_rules(self):
        # DNS and DHCP for everyone (needed to join the network and see the portal)
        run_cmd(['iptables', '-A', CHAIN, '-p', 'udp', '--dport', '53', '-j', 'ACCEPT'])
        run_cmd(['iptables', '-A', CHAIN, '-p', 'tcp', '--dport', '53', '-j', 'ACCEPT'])
        run_cmd(['iptables', '-A', CHAIN, '-p', 'udp', '--dport', '67:68', '-j', 'ACCEPT'])
        # Portal access
        run_cmd(['iptables', '-A', CHAIN, '-d', self.ap_ip, '-j', 'ACCEPT'])
        # Keep-state last so an inserted DROP beats it
        run_cmd(['iptables', '-A', CHAIN, '-m', 'state',
                 '--state', 'ESTABLISHED,RELATED', '-j', 'ACCEPT'])

    def _delete_mac_rules(self, mac_address):
        run_cmd(['iptables', '-D', CHAIN, '-m', 'mac', '--mac-source', mac_address,
                 '-j', 'ACCEPT'], ignore_errors=True)
        run_cmd(['iptables', '-D', CHAIN, '-m', 'mac', '--mac-source', mac_address,
                 '-j', 'DROP'], ignore_errors=True)

    def allow_mac(self, mac_address):
        try:
            self._delete_mac_rules(mac_address)
            run_cmd(['iptables', '-I', CHAIN, '1', '-m', 'mac',
                     '--mac-source', mac_address, '-j', 'ACCEPT'])
            self.logger.info(f"Allowed MAC {mac_address}")
            return True
        except Exception as e:
            self.logger.error(f"Error allowing MAC {mac_address}: {e}")
            return False

    def block_mac(self, mac_address):
        try:
            self._delete_mac_rules(mac_address)
            run_cmd(['iptables', '-I', CHAIN, '1', '-m', 'mac',
                     '--mac-source', mac_address, '-j', 'DROP'])
            self.logger.info(f"Blocked MAC {mac_address}")
            return True
        except Exception as e:
            self.logger.error(f"Error blocking MAC {mac_address}: {e}")
            return False

    def sync(self, allowed_macs):
        """Rebuild the chain from the given allow-list (e.g. after a restart)."""
        try:
            run_cmd(['iptables', '-F', CHAIN])
            self._add_static_rules()
            for mac in allowed_macs:
                run_cmd(['iptables', '-I', CHAIN, '1', '-m', 'mac',
                         '--mac-source', mac, '-j', 'ACCEPT'])
            self.logger.info(f"Firewall synced: {len(allowed_macs)} device(s) allowed")
            return True
        except Exception as e:
            self.logger.error(f"Error syncing firewall: {e}")
            return False
