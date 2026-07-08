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
        self.refresh_runtime_settings()

        logger.info("Initializing network controller...")
        self.network_controller = self._init_network_controller(manage_hardware)

        # Policy for devices seen for the first time: paying users get access
        # and their plan limits back, everyone else is blocked.
        self.network_controller.on_new_device = self.handle_new_device

        logger.info("Initializing time manager...")
        self.time_manager = TimeManager(self.user_manager, self.network_controller,
                                        self.settings)

        self.coinslot = None
        if self.settings.coinslot_enabled:
            from coinslot import CoinslotService
            logger.info("Initializing coinslot service...")
            self.coinslot = CoinslotService(self.user_manager,
                                            self.network_controller, self.settings)

        if manage_hardware:
            self.network_controller.reconcile(self.user_manager.get_active_users())

    def app_setting_defaults(self):
        return {
            'minutes_per_peso': str(self.settings.minutes_per_peso),
            'coinslot_claim_timeout': str(self.settings.coinslot_claim_timeout),
            'coinslot_pulses_per_peso': str(self.settings.coinslot_pulses_per_peso),
            'portal_title': self.settings.portal_title,
            'portal_subtitle': self.settings.portal_subtitle,
            'dashboard_refresh_seconds': str(self.settings.dashboard_refresh_seconds),
            'default_download_kbps': str(self.settings.default_download_kbps),
            'default_upload_kbps': str(self.settings.default_upload_kbps),
        }

    def refresh_runtime_settings(self):
        values = self.user_manager.get_app_settings(self.app_setting_defaults())
        self.settings.minutes_per_peso = float(values['minutes_per_peso'])
        self.settings.coinslot_claim_timeout = int(values['coinslot_claim_timeout'])
        self.settings.coinslot_pulses_per_peso = int(values['coinslot_pulses_per_peso'])
        self.settings.portal_title = values['portal_title']
        self.settings.portal_subtitle = values['portal_subtitle']
        self.settings.dashboard_refresh_seconds = int(values['dashboard_refresh_seconds'])
        self.settings.default_download_kbps = int(values['default_download_kbps'])
        self.settings.default_upload_kbps = int(values['default_upload_kbps'])
        return values

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
