"""Facade over the network package (AP lifecycle, firewall, QoS).

Keeps the public API that main.py / time_manager.py already use, while the
actual work lives in network/ap_manager.py, network/firewall.py and
network/qos.py.
"""
import logging

from network.ap_manager import APManager, is_valid_mac
from network.firewall import Firewall
from network.qos import QoSManager


class NetworkController:
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

        self.ap = APManager(settings)
        self.firewall = Firewall(settings.ap_interface, settings.internet_interface,
                                 settings.ap_ip)
        self.qos = QoSManager(settings.ap_interface,
                              self.DEFAULT_DOWNLOAD_SPEED, self.DEFAULT_UPLOAD_SPEED)

        # Called with a MAC the first time a device is seen; services.py wires
        # this to a balance check. Default: block unknown devices.
        self.on_new_device = self.block_mac

        self.connected_devices = set()

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
        try:
            devices = self.ap.get_stations()

            current_macs = {d['mac_address'] for d in devices}
            new_devices = current_macs - self.connected_devices
            disconnected = self.connected_devices - current_macs

            for mac in new_devices:
                self.logger.info(f"New device connected: {mac}")
                try:
                    self.on_new_device(mac)
                except Exception as e:
                    self.logger.error(f"on_new_device handler failed for {mac}: {e}")
            for mac in disconnected:
                self.logger.info(f"Device disconnected: {mac}")

            self.connected_devices = current_macs
            return devices
        except Exception as e:
            self.logger.error(f"Error getting connected devices: {e}")
            return []

    def resolve_ip(self, mac_address):
        return self.ap.resolve_ip(mac_address)

    def resolve_mac(self, ip_address):
        return self.ap.resolve_mac(ip_address)

    # --- access control ---------------------------------------------------

    def block_mac(self, mac_address):
        if not is_valid_mac(mac_address):
            self.logger.error(f"Refusing to block invalid MAC: {mac_address!r}")
            return False
        return self.firewall.block_mac(mac_address)

    def unblock_mac(self, mac_address):
        if not is_valid_mac(mac_address):
            self.logger.error(f"Refusing to unblock invalid MAC: {mac_address!r}")
            return False
        return self.firewall.allow_mac(mac_address)

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
        users = list(active_users)
        self.firewall.sync([u['mac_address'] for u in users])
        for user in users:
            ip = self.ap.resolve_ip(user['mac_address'])
            if ip:
                self.qos.set_limit(user['mac_address'], ip,
                                   user.get('download_limit'), user.get('upload_limit'))
        self.logger.info(f"Reconciled network state for {len(users)} active user(s)")

    def stop_ap(self):
        self.ap.stop()
