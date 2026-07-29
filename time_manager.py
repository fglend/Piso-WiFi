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

    PURGE_INTERVAL_SECONDS = 3600
    # Offline devices are only billable in whole minutes, so sweeping them on
    # every few-second poll would just re-run the same queries for nothing.
    OFFLINE_SWEEP_SECONDS = 60

    def __init__(self, user_manager, network_controller, settings):
        self.user_manager = user_manager
        self.network_controller = network_controller
        self.settings = settings
        self.check_interval = settings.check_interval
        self.device_retention_hours = settings.device_retention_hours
        self.running = False
        self.thread = None
        self._next_purge_at = 0.0
        self._next_offline_sweep_at = 0.0
        self._next_expiry_sync_at = 0.0
        # MACs on a wall-clock duration pass: their balance is derived from
        # expires_at, so the elapsed-time meter must not also charge them.
        self._expiry_macs = frozenset()
        self.logger = logging.getLogger(__name__)

    @property
    def pause_on_disconnect(self):
        """Read live: the admin settings page flips this at runtime and the
        meter thread must honour it without a service restart."""
        return self.settings.pause_on_disconnect

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
                self._purge_stale_devices()
                time.sleep(self.check_interval)
            except Exception as e:
                self.logger.error(f"Error in time manager run loop: {e}")
                time.sleep(1)  # Prevent tight loop on error

    def _check_and_deduct_time(self):
        try:
            now = time.time()
            self._sync_duration_passes(now)
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
            else:
                self._meter_offline_devices(connected_macs, now)
        except Exception as e:
            self.logger.error(f"Error in check_and_deduct_time: {e}")

    def _purge_stale_devices(self):
        """Drop spent, long-idle device rows and trim the history tables.

        Runs hourly - the meter loop polls every few seconds and this is
        housekeeping, not metering. Each step is guarded separately so a
        failure in one does not skip the other.
        """
        now = time.time()
        if now < self._next_purge_at:
            return
        self._next_purge_at = now + self.PURGE_INTERVAL_SECONDS

        if self.device_retention_hours > 0:
            try:
                self.user_manager.purge_stale_devices(self.device_retention_hours)
            except Exception as e:
                self.logger.error(f"Error purging stale devices: {e}")
        try:
            self.user_manager.prune_history()
        except Exception as e:
            self.logger.error(f"Error pruning history: {e}")

    def _sync_duration_passes(self, now):
        """Refresh wall-clock passes and cut off the ones that just ran out.

        A duration pass is not metered by elapsed connect time - its balance
        is simply the distance to its deadline, recomputed here in one
        statement for all of them. Runs on a slow cadence: the portal shows
        this to the minute, so a faster refresh would only cost SD writes.
        """
        if now < self._next_expiry_sync_at:
            return
        self._next_expiry_sync_at = now + self.OFFLINE_SWEEP_SECONDS
        try:
            tracked, expired = self.user_manager.sync_expiring_devices()
        except Exception as e:
            self.logger.error(f"Error syncing duration passes: {e}")
            return
        self._expiry_macs = frozenset(tracked)
        for mac in expired:
            if self.network_controller.is_access_allowed(mac):
                self.logger.info(f"Duration pass expired for {mac}, blocking...")
                self.network_controller.block_mac(mac)
                self.user_manager.clear_session(mac)

    def _meter_offline_devices(self, connected_macs, now):
        """Keep charging devices that are not on the network right now.

        This is what makes a day/week/month package mean elapsed time rather
        than screen time: the balance runs down whether or not the phone is
        associated, and expires on schedule instead of sitting frozen until
        the customer happens to reconnect.

        Operator downtime is still not charged - _reset_session_clocks()
        restarts each clock at startup, so a power cut or service restart
        does not bill customers for time the shop was closed.
        """
        if now < self._next_offline_sweep_at:
            return
        self._next_offline_sweep_at = now + self.OFFLINE_SWEEP_SECONDS
        for user in self.user_manager.get_active_users():
            mac = user['mac_address']
            if mac in connected_macs:
                continue
            try:
                self._process_device(mac, now, connected=False)
            except Exception as e:
                self.logger.error(f"Error metering offline device {mac}: {e}")

    def _process_device(self, mac, now, connected=True):
        if self.user_manager.is_paused(mac):
            # Manually paused from the portal: keep the clock frozen and the
            # device blocked. Never self-heal access or deduct while paused.
            if self.network_controller.is_access_allowed(mac):
                self.network_controller.block_mac(mac)
            return
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
        # Only for devices actually present: resolving an absent device's IP
        # fails, and unblocking one that is not associated buys nothing.
        if connected and not self.network_controller.is_access_allowed(mac):
            info = self.user_manager.get_device_info(mac)
            if self.network_controller.unblock_mac(mac) and info:
                self.network_controller.set_bandwidth_limit(
                    mac, info['download_limit'], info['upload_limit'])

        if mac in self._expiry_macs:
            # Wall-clock pass: the deadline owns the balance. Access has been
            # repaired above if needed; there is nothing to meter.
            return

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
