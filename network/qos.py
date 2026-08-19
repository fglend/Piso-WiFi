"""Per-device bandwidth limits using tc HTB (download) and ingress policing (upload).

Each device gets its own class id, allocated from a map so ids never collide,
and its filters use prio == class id so removing one device's filters cannot
touch another's (the old code deleted every prio-1 filter at once).
"""
import logging
import re

from network.command import run_cmd

logger = logging.getLogger(__name__)

CLASS_ID_MIN = 20
CLASS_ID_MAX = 1019
# Low-latency lane for packets carrying the firewall's game mark (0x67).
# Game flows are tiny (~100 kbps) so the reserved rate is small; prio 0
# means the lane is served before every client class when it has packets.
GAME_CLASS_ID = 5
GAME_MARK = '0x67'
GAME_LANE_RATE_KBPS = 2048
GAME_LANE_CEIL_KBPS = 4096


class QoSManager:
    def __init__(self, ap_interface, default_download_kbps, default_upload_kbps):
        self.ap_interface = ap_interface
        self.default_download = default_download_kbps
        self.default_upload = default_upload_kbps
        self.logger = logger
        # mac -> {'class_id': int, 'ip': str}
        self._clients = {}
        # mac -> {'download_bytes': int, 'upload_bytes': int}, folded in from
        # a client's tc counters each time its class is torn down. set_limit()
        # always deletes-and-recreates a device's class on reconnect,
        # bandwidth change or pause/resume, which zeroes tc's own counters -
        # banking them here first is what keeps get_usage() monotonic across
        # those churns for as long as this process keeps running.
        self._usage_totals = {}

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

        # Game lane: marked packets bypass client queues for latency (their
        # throughput stays negligible; caps on bulk traffic are untouched).
        run_cmd(['tc', 'class', 'add', 'dev', self.ap_interface, 'parent', '1:1',
                 'classid', f'1:{GAME_CLASS_ID}', 'htb',
                 'rate', f'{GAME_LANE_RATE_KBPS}kbit',
                 'ceil', f'{GAME_LANE_CEIL_KBPS}kbit',
                 'prio', '0', 'burst', '15k'])
        run_cmd(['tc', 'qdisc', 'add', 'dev', self.ap_interface,
                 'parent', f'1:{GAME_CLASS_ID}',
                 'handle', f'{GAME_CLASS_ID}:',
                 'pfifo', 'limit', '64'])
        run_cmd(['tc', 'filter', 'add', 'dev', self.ap_interface,
                 'parent', '1:', 'protocol', 'ip', 'prio', '1',
                 'handle', GAME_MARK, 'fw',
                 'flowid', f'1:{GAME_CLASS_ID}'])

        self._clients.clear()
        self._usage_totals.clear()
        self.logger.info("QoS root qdiscs initialized (game lane on 1:%d)",
                         GAME_CLASS_ID)

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

    def _read_counters(self, class_id):
        """(download_bytes, upload_bytes) tc has counted so far for one class.
        Best-effort: a parse miss (class gone, tc quirk) reads as zero rather
        than raising, since this must never block a limit change."""
        download_output = run_cmd(
            ['tc', '-s', 'class', 'show', 'dev', self.ap_interface,
             'classid', f'1:{class_id}'], ignore_errors=True)
        upload_output = run_cmd(
            ['tc', '-s', 'filter', 'show', 'dev', self.ap_interface,
             'parent', 'ffff:', 'prio', str(class_id)], ignore_errors=True)
        return self._parse_sent_bytes(download_output), self._parse_sent_bytes(upload_output)

    @staticmethod
    def _parse_sent_bytes(output):
        if not isinstance(output, str):
            return 0
        match = re.search(r'Sent (\d+) bytes', output)
        return int(match.group(1)) if match else 0

    def get_usage(self, mac_address):
        """Cumulative (download_bytes, upload_bytes) tc has counted for this
        device since it was first tracked in this process, including whatever
        was banked from earlier class re-creations."""
        totals = dict(self._usage_totals.get(
            mac_address, {'download_bytes': 0, 'upload_bytes': 0}))
        client = self._clients.get(mac_address)
        if client:
            download, upload = self._read_counters(client['class_id'])
            totals['download_bytes'] += download
            totals['upload_bytes'] += upload
        return totals

    def remove_limit(self, mac_address):
        client = self._clients.get(mac_address)
        if not client:
            return True
        class_id = client['class_id']
        download, upload = self._read_counters(class_id)
        banked = self._usage_totals.setdefault(
            mac_address, {'download_bytes': 0, 'upload_bytes': 0})
        banked['download_bytes'] += download
        banked['upload_bytes'] += upload
        del self._clients[mac_address]
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
