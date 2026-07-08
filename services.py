"""Single construction point for the application's services.

Exactly one UserManager / NetworkController / TimeManager exist per process;
the old code built a second NetworkController inside TimeManager, which
reconfigured the AP and flushed firewall rules on startup.
"""
import logging
import time

from config import load_settings
from network_controller import NetworkController
from time_manager import TimeManager
from user_manager import UserManager

logger = logging.getLogger(__name__)


class Services:
    def __init__(self, settings=None, manage_hardware=True):
        self.settings = settings or load_settings()

        logger.info("Initializing user manager...")
        self.user_manager = UserManager(self.settings.db_path)

        logger.info("Initializing network controller...")
        self.network_controller = self._init_network_controller(manage_hardware)

        # Policy for devices seen for the first time: paying users get access
        # and their plan limits back, everyone else is blocked.
        self.network_controller.on_new_device = self.handle_new_device

        logger.info("Initializing time manager...")
        self.time_manager = TimeManager(self.user_manager, self.network_controller,
                                        self.settings)

        if manage_hardware:
            self.network_controller.reconcile(self.user_manager.get_active_users())

    def _init_network_controller(self, manage_hardware, max_retries=3):
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                return NetworkController(self.settings, manage_hardware=manage_hardware)
            except Exception as e:
                last_error = e
                logger.error(
                    f"Network controller initialization failed "
                    f"(attempt {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    time.sleep(5)
        raise last_error

    def handle_new_device(self, mac_address):
        info = self.user_manager.get_device_info(mac_address)
        if info and info['time_balance'] > 0:
            logger.info(f"Known device {mac_address} has balance, restoring access")
            self.network_controller.unblock_mac(mac_address)
            self.network_controller.set_bandwidth_limit(
                mac_address, info['download_limit'], info['upload_limit'])
        else:
            self.network_controller.block_mac(mac_address)
