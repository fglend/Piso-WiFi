import logging
import threading
import time


class TimeManager:
    """Meters connected devices' time and blocks them when balance runs out.

    Uses shared UserManager/NetworkController instances (never builds its own -
    constructing a second NetworkController would reconfigure the AP and flush
    firewall state). The per-device deduction clock is persisted in the
    sessions table so a service restart neither grants free minutes nor
    back-charges downtime.
    """

    def __init__(self, user_manager, network_controller, settings):
        self.user_manager = user_manager
        self.network_controller = network_controller
        self.check_interval = settings.check_interval
        self.pause_on_disconnect = settings.pause_on_disconnect
        self.running = False
        self.thread = None
        self.logger = logging.getLogger(__name__)

    def start(self):
        self._reset_session_clocks()
        self.running = True
        self.thread = threading.Thread(target=self._run)
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=3)
            if self.thread.is_alive():
                self.logger.warning('Time manager thread did not stop within 3 seconds')

    def _reset_session_clocks(self):
        """On startup, restart the clock for connected devices and drop the rest
        so downtime is never charged and restarts never grant free time."""
        try:
            now = time.time()
            connected = {d['mac_address']
                         for d in self.network_controller.get_connected_devices()}
            for user in self.user_manager.get_active_users():
                mac = user['mac_address']
                if mac in connected:
                    self.user_manager.set_last_deduction(mac, now)
                else:
                    self.user_manager.clear_session(mac)
        except Exception as e:
            self.logger.error(f"Error resetting session clocks: {e}")

    def _run(self):
        while self.running:
            try:
                self._check_and_deduct_time()
                time.sleep(self.check_interval)
            except Exception as e:
                self.logger.error(f"Error in time manager run loop: {e}")
                time.sleep(1)  # Prevent tight loop on error

    def _check_and_deduct_time(self):
        try:
            now = time.time()
            connected_devices = self.network_controller.get_connected_devices()
            connected_macs = set()

            for device in connected_devices:
                mac = device['mac_address']
                connected_macs.add(mac)
                try:
                    self._process_device(mac, now)
                except Exception as e:
                    self.logger.error(f"Error checking balance for {mac}: {e}")

            if self.pause_on_disconnect:
                # Stop the clock for devices that left so their balance freezes
                for user in self.user_manager.get_active_users():
                    mac = user['mac_address']
                    if mac not in connected_macs:
                        if self.user_manager.get_last_deduction(mac) is not None:
                            self.user_manager.clear_session(mac)
                            self.logger.info(f"Paused clock for disconnected device {mac}")
        except Exception as e:
            self.logger.error(f"Error in check_and_deduct_time: {e}")

    def _process_device(self, mac, now):
        balance = self.user_manager.check_balance(mac)

        if balance <= 0:
            # Act only on the transition to empty: re-blocking an already
            # blocked device every poll just spams the log and churns
            # iptables without changing any state.
            if self.network_controller.is_access_allowed(mac):
                self.logger.info(f"Balance zero for {mac}, blocking...")
                self.network_controller.block_mac(mac)
                self.user_manager.clear_session(mac)
            return

        # A concurrent top-up can race with a stale zero-balance block. Track
        # the applied firewall state and self-heal it on the next meter pass.
        if not self.network_controller.is_access_allowed(mac):
            info = self.user_manager.get_device_info(mac)
            if self.network_controller.unblock_mac(mac) and info:
                self.network_controller.set_bandwidth_limit(
                    mac, info['download_limit'], info['upload_limit'])

        last = self.user_manager.get_last_deduction(mac)
        if last is None:
            # Clock starts now; the first minute is charged a minute from now
            self.user_manager.set_last_deduction(mac, now)
            return

        elapsed_minutes = (now - last) / 60.0
        if elapsed_minutes < 1.0:
            return

        # Charge the exact elapsed time (no truncation drift)
        to_deduct = round(elapsed_minutes, 2)
        if self.user_manager.deduct_time(mac, to_deduct):
            self.user_manager.set_last_deduction(mac, now)
            new_balance = self.user_manager.check_balance(mac)
            # user_manager already logs each deduction at INFO
            self.logger.debug(
                f"Deducted {to_deduct} minute(s) from {mac}, remaining: {new_balance}")
            if new_balance <= 0:
                self.logger.info(f"Balance depleted for {mac}, blocking...")
                self.network_controller.block_mac(mac)
                self.user_manager.clear_session(mac)
