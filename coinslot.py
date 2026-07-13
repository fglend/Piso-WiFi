"""GPIO pulse coinslot (CH-926 / Weiyu universal style: N pulses per peso on
a falling edge).

Flow: a user taps "Insert Coin" on the portal, which claims the slot for
their MAC for a limited window and energizes a relay that powers the
acceptor. Pulses arriving while a claim is active are credited to that MAC
immediately; pulses with no active claim are logged and ignored (belt and
suspenders - the relay should already have cut the acceptor's power by
then). The relay de-energizes on expiry or shutdown, so the acceptor is
electrically dead outside of an active claim window.
"""
import logging
import os
import select
import threading
import time

from pricing import compute_minutes

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


class SysfsGpioRelay:
    """Output-only sysfs GPIO pin driving a relay (or any on/off actuator).

    Defaults to "off" the moment the pin is exported, before this process
    (or a future one) ever calls set() - a crash or a slow app start leaves
    the relay de-energized rather than in an undefined state.
    """

    def __init__(self, pin, active_high=False):
        self.pin = pin
        self.active_high = active_high

    def _write(self, path, value):
        with open(path, 'w') as f:
            f.write(value)

    def open(self):
        pin_dir = f'{GPIO_ROOT}/gpio{self.pin}'
        if not os.path.isdir(pin_dir):
            self._write(f'{GPIO_ROOT}/export', str(self.pin))
            time.sleep(0.1)
        # 'low'/'high' set direction=out AND an initial value atomically,
        # so there's no window where the pin is an output with whatever
        # value it happened to power up with.
        self._write(f'{pin_dir}/direction', 'low' if self.active_high else 'high')
        self.set(False)

    def set(self, active):
        value = active if self.active_high else not active
        self._write(f'{GPIO_ROOT}/gpio{self.pin}/value', '1' if value else '0')

    def close(self):
        self.set(False)
        pin_dir = f'{GPIO_ROOT}/gpio{self.pin}'
        if os.path.isdir(pin_dir):
            self._write(f'{GPIO_ROOT}/unexport', str(self.pin))


class CoinslotService:
    def __init__(self, user_manager, network_controller, settings, reader=None, relay=None):
        self.user_manager = user_manager
        self.network_controller = network_controller
        self.settings = settings
        self.reader = reader or SysfsGpioReader(settings.coinslot_gpio,
                                                settings.coinslot_debounce_ms)
        self.relay = relay or SysfsGpioRelay(settings.coinslot_relay_gpio,
                                             settings.coinslot_relay_active_high)
        self.logger = logger
        self.running = False
        self.thread = None
        self._lock = threading.Lock()
        # active claim: {'mac': str, 'expires': float, 'pulses': int, 'pesos': float}
        self._claim = None
        self._relay_on = False

    # --- relay ---------------------------------------------------------

    def _set_relay(self, active):
        """Energize/de-energize the acceptor's power relay (no-op if unchanged)."""
        if active == self._relay_on:
            return
        self.relay.set(active)
        self._relay_on = active
        self.logger.info(f"Coinslot relay {'energized' if active else 'de-energized'}")

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
                           'pulses': 0, 'pesos': 0.0, 'minutes_added': 0.0,
                           # rate table snapshot: one session, one price list
                           'rates': self.user_manager.get_rates()}
            self._set_relay(True)
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
                'minutes_added': round(claim['minutes_added'], 1),
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
            old_total = claim['pesos']
            claim['pesos'] = old_total + pesos
            # inserting coins keeps the window open
            claim['expires'] = now + self.settings.coinslot_claim_timeout
            mac = claim['mac']
            # Tier the CUMULATIVE session total and credit the delta, so a
            # ₱5 coin (5 pulses) earns the ₱5 tier, not 5x the ₱1 tier
            fallback = self.settings.minutes_per_peso
            minutes = (compute_minutes(claim['pesos'], claim['rates'], fallback)
                       - compute_minutes(old_total, claim['rates'], fallback))
            claim['minutes_added'] += minutes
        if self.user_manager.add_time(mac, pesos, minutes, source='coin'):
            self.network_controller.unblock_mac(mac)
            info = self.user_manager.get_device_info(mac)
            if info:
                self.network_controller.set_bandwidth_limit(
                    mac, info['download_limit'], info['upload_limit'])
            self.logger.info(f"Credited ₱{pesos} ({minutes:g} min) to {mac}")
        else:
            self.logger.error(f"Failed to credit coin for {mac}")

    def _expire_claim_if_due(self):
        with self._lock:
            if self._claim and self._claim['expires'] <= time.monotonic():
                self._claim = None
                self._set_relay(False)

    def _run(self):
        while self.running:
            try:
                if self.reader.wait_pulse(timeout=1.0):
                    self._on_pulse()
                self._expire_claim_if_due()
            except Exception as e:
                self.logger.error(f"Coinslot error: {e}")
                time.sleep(1)

    def start(self):
        self.relay.open()
        self.reader.open()
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.logger.info(
            f"Coinslot service started (SIG GPIO {self.settings.coinslot_gpio}, "
            f"relay GPIO {self.settings.coinslot_relay_gpio})")

    def stop(self):
        self.running = False
        errors = []
        actions = [
            # Cut acceptor power before waiting for the pulse thread to exit.
            ('de-energize relay', lambda: self._set_relay(False)),
            ('join pulse thread',
             lambda: self.thread.join(timeout=3) if self.thread else None),
            ('close pulse reader', self.reader.close),
            # close() retries the inactive write before unexporting the GPIO.
            ('close relay GPIO', self.relay.close),
        ]
        for label, action in actions:
            try:
                action()
            except Exception as exc:
                errors.append(f'{label}: {exc}')
                self.logger.error(f'Coinslot shutdown failed to {label}: {exc}')
        if errors:
            raise RuntimeError('; '.join(errors))
