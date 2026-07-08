"""Per-device bandwidth limits using tc HTB (download) and ingress policing (upload).

Each device gets its own class id, allocated from a map so ids never collide,
and its filters use prio == class id so removing one device's filters cannot
touch another's (the old code deleted every prio-1 filter at once).
"""
import logging

from network.command import run_cmd

logger = logging.getLogger(__name__)

CLASS_ID_MIN = 20
CLASS_ID_MAX = 1019


class QoSManager:
    def __init__(self, ap_interface, default_download_kbps, default_upload_kbps):
        self.ap_interface = ap_interface
        self.default_download = default_download_kbps
        self.default_upload = default_upload_kbps
        self.logger = logger
        # mac -> {'class_id': int, 'ip': str}
        self._clients = {}

    def setup(self):
        """(Re)initialize root qdiscs. Wipes all client classes."""
        run_cmd(['tc', 'qdisc', 'del', 'dev', self.ap_interface, 'root'], ignore_errors=True)
        run_cmd(['tc', 'qdisc', 'del', 'dev', self.ap_interface, 'ingress'], ignore_errors=True)

        run_cmd(['tc', 'qdisc', 'add', 'dev', self.ap_interface,
                 'root', 'handle', '1:', 'htb', 'default', '10'])
        run_cmd(['tc', 'class', 'add', 'dev', self.ap_interface, 'parent', '1:',
                 'classid', '1:1', 'htb', 'rate', '100mbit', 'burst', '15k'])
        run_cmd(['tc', 'class', 'add', 'dev', self.ap_interface, 'parent', '1:1',
                 'classid', '1:10', 'htb',
                 'rate', f'{self.default_download}kbit',
                 'ceil', f'{self.default_download}kbit', 'burst', '15k'])
        run_cmd(['tc', 'qdisc', 'add', 'dev', self.ap_interface, 'ingress'])

        self._clients.clear()
        self.logger.info("QoS root qdiscs initialized")

    def _allocate_class_id(self):
        used = {c['class_id'] for c in self._clients.values()}
        for candidate in range(CLASS_ID_MIN, CLASS_ID_MAX + 1):
            if candidate not in used:
                return candidate
        raise RuntimeError("No free QoS class ids")

    def set_limit(self, mac_address, ip_address, download_kbps=None, upload_kbps=None):
        download_kbps = download_kbps or self.default_download
        upload_kbps = upload_kbps or self.default_upload

        try:
            # Replace any existing limit for this device
            self.remove_limit(mac_address)
            class_id = self._allocate_class_id()

            run_cmd(['tc', 'class', 'add', 'dev', self.ap_interface, 'parent', '1:1',
                     'classid', f'1:{class_id}', 'htb',
                     'rate', f'{download_kbps}kbit',
                     'ceil', f'{download_kbps}kbit', 'burst', '15k'])
            run_cmd(['tc', 'qdisc', 'add', 'dev', self.ap_interface,
                     'parent', f'1:{class_id}', 'handle', f'{class_id}:',
                     'sfq', 'perturb', '10'])

            # prio == class_id keeps this device's filters individually removable
            run_cmd(['tc', 'filter', 'add', 'dev', self.ap_interface, 'parent', '1:',
                     'protocol', 'ip', 'prio', str(class_id), 'u32',
                     'match', 'ip', 'dst', ip_address, 'flowid', f'1:{class_id}'])
            run_cmd(['tc', 'filter', 'add', 'dev', self.ap_interface, 'parent', '1:',
                     'protocol', 'ip', 'prio', str(class_id), 'u32',
                     'match', 'ip', 'src', ip_address, 'flowid', f'1:{class_id}'])
            run_cmd(['tc', 'filter', 'add', 'dev', self.ap_interface, 'parent', 'ffff:',
                     'protocol', 'ip', 'prio', str(class_id), 'u32',
                     'match', 'ip', 'src', ip_address,
                     'police', 'rate', f'{upload_kbps}kbit', 'burst', '15k',
                     'drop', 'flowid', ':1'])

            self._clients[mac_address] = {'class_id': class_id, 'ip': ip_address}
            self.logger.info(
                f"Bandwidth limit for {mac_address} ({ip_address}): "
                f"{download_kbps}kbps down / {upload_kbps}kbps up")
            return True
        except Exception as e:
            self.logger.error(f"Error setting bandwidth limit for {mac_address}: {e}")
            return False

    def remove_limit(self, mac_address):
        client = self._clients.pop(mac_address, None)
        if not client:
            return True
        class_id = client['class_id']
        try:
            run_cmd(['tc', 'filter', 'del', 'dev', self.ap_interface, 'parent', '1:',
                     'prio', str(class_id)], ignore_errors=True)
            run_cmd(['tc', 'filter', 'del', 'dev', self.ap_interface, 'parent', 'ffff:',
                     'prio', str(class_id)], ignore_errors=True)
            run_cmd(['tc', 'qdisc', 'del', 'dev', self.ap_interface,
                     'parent', f'1:{class_id}'], ignore_errors=True)
            run_cmd(['tc', 'class', 'del', 'dev', self.ap_interface,
                     'classid', f'1:{class_id}'], ignore_errors=True)
            self.logger.info(f"Removed bandwidth limit for {mac_address}")
            return True
        except Exception as e:
            self.logger.error(f"Error removing bandwidth limit for {mac_address}: {e}")
            return False
