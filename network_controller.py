"""Facade over the network package (AP lifecycle, firewall, QoS).

Keeps the public API that main.py / time_manager.py already use, while the
actual work lives in network/ap_manager.py, network/firewall.py and
network/qos.py.
"""
import logging
from ipaddress import IPv4Address, AddressValueError
from threading import RLock

from network.ap_manager import APManager, is_valid_mac
from network.firewall import Firewall
from network.qos import QoSManager
from network.wired import WiredGateway


class NetworkController:
    DISCONNECT_CONFIRMATION_POLLS = 2
    # Bandwidth plans (kbps) - fallbacks when the plans table is unavailable
    DEFAULT_DOWNLOAD_SPEED = 2048
    DEFAULT_UPLOAD_SPEED = 1024
    PREMIUM_DOWNLOAD_SPEED = 8096
    PREMIUM_UPLOAD_SPEED = 8096

    def __init__(self, settings, manage_hardware=True):
        """manage_hardware=False builds the object without touching the system
        (used by tests)."""
        self.logger = logging.getLogger(__name__)
        self.settings = settings
        self.ap_interface = settings.ap_interface
        self.internet_interface = settings.internet_interface
        protected_devices = self._resolve_protected_devices(settings)
        self.trusted_macs = frozenset(protected_devices)
        if protected_devices:
            self.logger.info("Protected infrastructure: %s",
                             ', '.join(f'{mac}={ip}' for mac, ip
                                       in sorted(protected_devices.items())))

        backend = WiredGateway if settings.network_mode == 'wired' else APManager
        self.ap = backend(settings)
        self.firewall = Firewall(settings.ap_interface, settings.internet_interface,
                                 settings.ap_ip, protected_devices,
                                 portal_port=settings.port,
                                 game_udp_ports=getattr(
                                     settings, 'game_udp_ports', ''),
                                 block_tethering=getattr(
                                     settings, 'block_tethering', False),
                                 tethering_ttls=getattr(
                                     settings, 'tethering_blocked_ttls', ''),
                                 ssh_whitelist_enabled=getattr(
                                     settings, 'ssh_whitelist_enabled', False),
                                 ssh_whitelist_macs=settings.ssh_whitelist_mac_list()
                                 if hasattr(settings, 'ssh_whitelist_mac_list') else [],
                                 dos_protection_enabled=getattr(
                                     settings, 'dos_protection_enabled', False))
        self.qos = QoSManager(settings.ap_interface,
                              self.DEFAULT_DOWNLOAD_SPEED, self.DEFAULT_UPLOAD_SPEED)

        # Called with a MAC the first time a device is seen; services.py wires
        # this to a balance check. Default: block unknown devices.
        self.on_new_device = self.block_mac
        self.on_device_snapshot = lambda devices: None

        self.connected_devices = set()
        self._known_devices = {}
        self._absence_counts = {}
        self._discovery_lock = RLock()
        self._access_lock = RLock()
        self.allowed_macs = frozenset()

        if manage_hardware:
            self._bring_up()

    def _bring_up(self):
        self.logger.info("Initializing Network Controller...")
        self.ap.verify_requirements()
        self.ap.write_configs()
        self.ap.start()
        if not self.ap.check_status():
            raise Exception("AP failed to start properly")
        self.firewall.setup()
        self.qos.setup()
        self.logger.info("Network Controller initialized successfully")

    @staticmethod
    def is_valid_mac(mac):
        return is_valid_mac(mac)

    # --- device discovery -------------------------------------------------

    def get_connected_devices(self):
        with self._discovery_lock:
            return self._get_connected_devices()

    def _flush_stale_state(self, ip_address):
        """Clear neighbor/conntrack entries for an IP whose owner changed."""
        if not ip_address or ip_address == 'Unknown':
            return
        try:
            self.firewall.flush_device_state(ip_address)
        except Exception as e:
            self.logger.error(f"State flush failed for {ip_address}: {e}")
        # The cached lease/neighbor lookups may still name the old MAC.
        self.ap.invalidate_caches()

    def _get_connected_devices(self):
        try:
            prior_devices = dict(self._known_devices)
            # Work with copies so station data owned by a backend is never
            # mutated. MACs are canonicalized for case-insensitive matching.
            devices = [
                {**device, 'mac_address': str(device['mac_address']).upper()}
                for device in self.ap.get_stations()
            ]
            devices = [
                device for device in devices
                if device['mac_address'] not in self.trusted_macs
            ]

            observed_by_mac = {
                device['mac_address']: device for device in devices
            }
            absence_counts = {
                mac: self._absence_counts.get(mac, 0) + 1
                for mac in self._known_devices
                if mac not in observed_by_mac
            }
            pending_devices = {
                mac: dict(self._known_devices[mac])
                for mac, misses in absence_counts.items()
                if misses < self.DISCONNECT_CONFIRMATION_POLLS
            }
            effective_by_mac = {**pending_devices, **observed_by_mac}
            devices = [dict(device) for device in effective_by_mac.values()]
            self._known_devices = {
                mac: dict(device) for mac, device in effective_by_mac.items()
            }
            self._absence_counts = {
                mac: misses for mac, misses in absence_counts.items()
                if mac in pending_devices
            }

            current_macs = {d['mac_address'] for d in devices}
            new_devices = current_macs - self.connected_devices
            disconnected = self.connected_devices - current_macs

            for mac in new_devices:
                self.logger.info(f"New device connected: {mac}")
                # A new MAC may be a phone that toggled MAC randomization and
                # reclaimed the IP its old identity held; drop stale kernel
                # state so it is reachable immediately.
                self._flush_stale_state(
                    observed_by_mac.get(mac, {}).get('ip'))
                try:
                    self.on_new_device(mac)
                except Exception as e:
                    self.logger.error(f"on_new_device handler failed for {mac}: {e}")
            for mac in disconnected:
                self.logger.info(f"Device disconnected: {mac}")
                self._flush_stale_state(prior_devices.get(mac, {}).get('ip'))

            # Persist the authoritative observed snapshot. The effective list
            # returned to callers has a one-poll UI/metering debounce, while
            # SQLite independently confirms absence across polls and restarts.
            snapshot = tuple(
                dict(device) for device in observed_by_mac.values())
            try:
                self.on_device_snapshot(snapshot)
            except Exception as exc:
                self.logger.error("Device snapshot handler failed: %s", exc)

            self.connected_devices = current_macs
            return devices
        except Exception as e:
            self.logger.error(f"Error getting connected devices: {e}")
            return [dict(device) for device in self._known_devices.values()]

    def forget_devices(self, mac_addresses):
        """Block, flush and drop devices from the connected list.

        The list the dashboard shows is a debounced view of DHCP leases and the
        neighbour table: a device that walked away lingers for
        DISCONNECT_CONFIRMATION_POLLS, and a stale lease or neighbour entry can
        keep it visible longer still. Clearing the in-memory state as well as
        the kernel's is what actually makes the row go away instead of
        reappearing on the next poll.

        Nothing is deleted from the database. A device that is still physically
        connected is simply seen as new on the next poll, and handle_new_device
        restores its access if it turns out to have balance - so clearing a
        device by mistake is self-correcting.

        Protected infrastructure is never touched. Returns the MACs forgotten.
        """
        forgotten = []
        with self._discovery_lock:
            for mac in sorted({str(m).strip().upper() for m in mac_addresses}):
                if mac in self.trusted_macs:
                    self.logger.warning(
                        "Refusing to reset protected device %s", mac)
                    continue
                ip_address = (self._known_devices.get(mac) or {}).get('ip')
                try:
                    self.firewall.block_mac(mac)
                except Exception as exc:
                    self.logger.error(
                        "Could not block %s during reset: %s", mac, exc)
                self._flush_stale_state(ip_address)
                self._known_devices.pop(mac, None)
                self._absence_counts.pop(mac, None)
                self.connected_devices.discard(mac)
                forgotten.append(mac)
        if forgotten:
            self.logger.info("Reset %s connected device(s): %s",
                             len(forgotten), ', '.join(forgotten))
        return forgotten

    def _resolve_protected_devices(self, settings):
        """Infrastructure MAC -> reserved IP, never blocked or redirected.

        A PoE switch feeding several APs is the same problem as one AP, just
        with more rows, so this handles any number. Only meaningful in wired
        mode: with the Pi's own radio there is no external AP to protect.

        Entries are re-checked here even though config.validate() already
        raised on bad ones, because tests and callers can build a Settings
        object without going through validate(). A bad row is dropped with a
        loud error rather than taking the gateway down at startup.
        """
        if settings.network_mode != 'wired':
            return {}
        try:
            configured = settings.protected_device_map()
        except Exception as exc:
            self.logger.error("Ignoring PROTECTED_DEVICES: %s", exc)
            return {}

        devices = {}
        for mac, ip in configured.items():
            mac = mac.strip().upper()
            if not is_valid_mac(mac):
                self.logger.error("Ignoring protected device, bad MAC: %r", mac)
                continue
            try:
                devices[mac] = str(IPv4Address(ip.strip()))
            except (AddressValueError, AttributeError):
                self.logger.error(
                    "Ignoring protected device %s, bad IP: %r", mac, ip)
        return devices

    def resolve_ip(self, mac_address):
        return self.ap.resolve_ip(mac_address)

    def resolve_mac(self, ip_address):
        return self.ap.resolve_mac(ip_address)

    # --- access control ---------------------------------------------------

    def block_mac(self, mac_address):
        if not is_valid_mac(mac_address):
            self.logger.error(f"Refusing to block invalid MAC: {mac_address!r}")
            return False
        normalized_mac = mac_address.upper()
        if normalized_mac in self.trusted_macs:
            self.logger.warning(
                "Prevented firewall block of trusted PoE AP %s", normalized_mac)
        with self._access_lock:
            succeeded = self.firewall.block_mac(normalized_mac)
            # A failed multi-command transition leaves kernel state unknown;
            # mark it denied so a future positive-balance pass repairs it.
            self.allowed_macs = self.allowed_macs - {normalized_mac}
        return succeeded

    def unblock_mac(self, mac_address):
        if not is_valid_mac(mac_address):
            self.logger.error(f"Refusing to unblock invalid MAC: {mac_address!r}")
            return False
        normalized_mac = mac_address.upper()
        with self._access_lock:
            succeeded = self.firewall.allow_mac(normalized_mac)
            if succeeded:
                self.allowed_macs = self.allowed_macs | {normalized_mac}
            else:
                self.allowed_macs = self.allowed_macs - {normalized_mac}
        return succeeded

    def is_access_allowed(self, mac_address):
        with self._access_lock:
            return mac_address.upper() in self.allowed_macs

    # --- security toggles (Admin > Security page) --------------------------

    def apply_ssh_whitelist(self, macs, enabled):
        """Push an updated SSH MAC whitelist to the firewall immediately."""
        return self.firewall.apply_ssh_whitelist(macs, enabled)

    def apply_dos_protection(self, enabled):
        return self.firewall.apply_dos_protection(enabled)

    # --- bandwidth ----------------------------------------------------------

    def set_bandwidth_limit(self, mac_address, download_kbps=None, upload_kbps=None):
        if not is_valid_mac(mac_address):
            self.logger.error(f"Refusing to shape invalid MAC: {mac_address!r}")
            return False
        ip_address = self.ap.resolve_ip(mac_address)
        if not ip_address:
            self.logger.error(f"Could not find IP address for MAC {mac_address}")
            return False
        return self.qos.set_limit(mac_address, ip_address, download_kbps, upload_kbps)

    def remove_bandwidth_limit(self, mac_address):
        if not is_valid_mac(mac_address):
            return False
        return self.qos.remove_limit(mac_address)

    # --- lifecycle ----------------------------------------------------------

    def reconcile(self, active_users):
        """Rebuild firewall/QoS state from the database after a (re)start.

        active_users: iterable of dicts with mac_address, download_limit,
        upload_limit for users that still have balance.
        """
        users = [
            user for user in active_users
            if user['mac_address'].upper() not in self.trusted_macs
        ]
        allowed_macs = list(dict.fromkeys(
            u['mac_address'].upper() for u in users))
        with self._access_lock:
            synced = self.firewall.sync(allowed_macs)
            if synced:
                self.allowed_macs = frozenset(allowed_macs)
            else:
                self.allowed_macs = frozenset()
        for user in users:
            ip = self.ap.resolve_ip(user['mac_address'])
            if ip:
                self.qos.set_limit(user['mac_address'], ip,
                                   user.get('download_limit'), user.get('upload_limit'))
        self.logger.info(f"Reconciled network state for {len(users)} active user(s)")

    def stop_ap(self):
        self.ap.stop()
