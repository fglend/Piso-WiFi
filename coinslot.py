"""GPIO pulse coinslot (CH-926 style: N pulses per peso on a falling edge).

Flow: a user taps "Insert Coin" on the portal, which claims the slot for
their MAC for a limited window. Pulses arriving while a claim is active are
credited to that MAC immediately; pulses with no active claim are logged and
ignored (so stray coins can't credit a random device).
"""
import logging
import os
import select
import threading
import time

logger = logging.getLogger(__name__)

GPIO_ROOT = '/sys/class/gpio'


class SysfsGpioReader:
    """Falling-edge pulse reader on a sysfs GPIO pin."""

    def __init__(self, pin, debounce_ms=50):
        self.pin = pin
        self.debounce_s = debounce_ms / 1000.0
        self._value_fd = None
        self._last_pulse = 0.0

    def open(self):
        pin_dir = f'{GPIO_ROOT}/gpio{self.pin}'
        if not os.path.isdir(pin_dir):
            with open(f'{GPIO_ROOT}/export', 'w') as f:
                f.write(str(self.pin))
            time.sleep(0.1)
        with open(f'{pin_dir}/direction', 'w') as f:
            f.write('in')
        with open(f'{pin_dir}/edge', 'w') as f:
            f.write('falling')
        self._value_fd = os.open(f'{pin_dir}/value', os.O_RDONLY | os.O_NONBLOCK)
        os.read(self._value_fd, 8)  # clear initial state

    def close(self):
        if self._value_fd is not None:
            os.close(self._value_fd)
            self._value_fd = None

    def wait_pulse(self, timeout=1.0):
        """Block up to timeout seconds; return True when a debounced pulse fires."""
        poller = select.poll()
        poller.register(self._value_fd, select.POLLPRI | select.POLLERR)
        events = poller.poll(timeout * 1000)
        if not events:
            return False
        os.lseek(self._value_fd, 0, os.SEEK_SET)
        os.read(self._value_fd, 8)
        now = time.monotonic()
        if now - self._last_pulse < self.debounce_s:
            return False
        self._last_pulse = now
        return True


class CoinslotService:
    def __init__(self, user_manager, network_controller, settings, reader=None):
        self.user_manager = user_manager
        self.network_controller = network_controller
        self.settings = settings
        self.reader = reader or SysfsGpioReader(settings.coinslot_gpio,
                                                settings.coinslot_debounce_ms)
        self.logger = logger
        self.running = False
        self.thread = None
        self._lock = threading.Lock()
        # active claim: {'mac': str, 'expires': float, 'pulses': int, 'pesos': float}
        self._claim = None

    # --- portal API -------------------------------------------------------

    def claim(self, mac_address):
        """Reserve the coinslot for a device; returns the claim window in seconds.

        Fails (returns None) while another device holds an active claim.
        """
        with self._lock:
            now = time.monotonic()
            if self._claim and self._claim['expires'] > now \
                    and self._claim['mac'] != mac_address:
                return None
            self._claim = {'mac': mac_address,
                           'expires': now + self.settings.coinslot_claim_timeout,
                           'pulses': 0, 'pesos': 0.0}
            self.logger.info(f"Coinslot claimed by {mac_address}")
            return self.settings.coinslot_claim_timeout

    def status(self, mac_address=None):
        with self._lock:
            now = time.monotonic()
            if not self._claim or self._claim['expires'] <= now:
                return {'active': False}
            claim = self._claim
            return {
                'active': True,
                'yours': mac_address is not None and claim['mac'] == mac_address,
                'seconds_left': int(claim['expires'] - now),
                'pesos_inserted': claim['pesos'],
            }

    # --- pulse handling ---------------------------------------------------

    def _on_pulse(self):
        with self._lock:
            now = time.monotonic()
            if not self._claim or self._claim['expires'] <= now:
                self.logger.warning("Coin pulse received with no active claim - ignored")
                return
            claim = self._claim
            claim['pulses'] += 1
            if claim['pulses'] % self.settings.coinslot_pulses_per_peso != 0:
                return
            pesos = 1
            claim['pesos'] += pesos
            # inserting coins keeps the window open
            claim['expires'] = now + self.settings.coinslot_claim_timeout
            mac = claim['mac']

        minutes = pesos * self.settings.minutes_per_peso
        if self.user_manager.add_time(mac, pesos, minutes, source='coin'):
            self.network_controller.unblock_mac(mac)
            info = self.user_manager.get_device_info(mac)
            if info:
                self.network_controller.set_bandwidth_limit(
                    mac, info['download_limit'], info['upload_limit'])
            self.logger.info(f"Credited ₱{pesos} ({minutes:g} min) to {mac}")
        else:
            self.logger.error(f"Failed to credit coin for {mac}")

    def _run(self):
        while self.running:
            try:
                if self.reader.wait_pulse(timeout=1.0):
                    self._on_pulse()
            except Exception as e:
                self.logger.error(f"Coinslot error: {e}")
                time.sleep(1)

    def start(self):
        self.reader.open()
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.logger.info(f"Coinslot service started (GPIO {self.settings.coinslot_gpio})")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=3)
        self.reader.close()
