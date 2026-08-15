"""Single construction point for the application's services.

Exactly one UserManager / NetworkController / TimeManager exist per process;
the old code built a second NetworkController inside TimeManager, which
reconfigured the AP and flushed firewall rules on startup.
"""
import logging
import time

from config import is_valid_color, load_settings
from network_controller import NetworkController
from time_manager import TimeManager
from user_manager import UserManager

logger = logging.getLogger(__name__)


def _as_bool(value):
    """Parse a stored app_settings flag. Module level, not a method: the
    settings helpers are bound onto plain namespaces in tests."""
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _safe_color(value, fallback):
    """Stored colour if it is a valid hex triple, else the previous value."""
    candidate = str(value or '').strip()
    if is_valid_color(candidate):
        return candidate
    if candidate:
        logger.warning("Ignoring invalid stored theme colour %r", candidate)
    return fallback


class Services:
    def __init__(self, settings=None, manage_hardware=True):
        self._shutdown = False
        self.settings = settings or load_settings()

        logger.info("Initializing user manager...")
        self.user_manager = UserManager(self.settings.db_path)
        self.refresh_runtime_settings()

        logger.info("Initializing network controller...")
        self.network_controller = self._init_network_controller(manage_hardware)

        # Policy for devices seen for the first time: paying users get access
        # and their plan limits back, everyone else is blocked.
        self.network_controller.on_new_device = self.handle_new_device
        self.network_controller.on_device_snapshot = (
            self.user_manager.sync_connection_snapshot)
        self.network_controller.on_reassociation = self._handle_reassociation

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

    def shutdown(self):
        """Release hardware promptly during a graceful process shutdown."""
        if self._shutdown:
            return
        succeeded = True
        if self.coinslot:
            try:
                self.coinslot.stop()
            except Exception as exc:
                succeeded = False
                logger.error(f"Could not stop coinslot safely: {exc}")
        try:
            self.time_manager.stop()
        except Exception as exc:
            succeeded = False
            logger.error(f"Could not stop time manager: {exc}")
        self._shutdown = succeeded

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
            # Stored as a runtime setting so the operator can switch metering
            # policy from the settings page. The .env value is only the
            # first-run default; the database wins after that.
            'pause_on_disconnect': '1' if self.settings.pause_on_disconnect else '0',
            'allow_manual_pause': '1' if self.settings.allow_manual_pause else '0',
            'theme_accent': self.settings.theme_accent,
            'theme_accent_strong': self.settings.theme_accent_strong,
            'portal_logo': self.settings.portal_logo,
            'portal_footer_text': self.settings.portal_footer_text,
            'login_max_attempts': str(self.settings.login_max_attempts),
            'login_lockout_seconds': str(self.settings.login_lockout_seconds),
            'ssh_whitelist_enabled': '1' if self.settings.ssh_whitelist_enabled else '0',
            'ssh_whitelist_macs': self.settings.ssh_whitelist_macs,
            'dos_protection_enabled': '1' if self.settings.dos_protection_enabled else '0',
            'content_filter_enabled': '1' if self.settings.content_filter_enabled else '0',
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
        self.settings.pause_on_disconnect = _as_bool(
            values['pause_on_disconnect'])
        self.settings.allow_manual_pause = _as_bool(
            values['allow_manual_pause'])
        # A stored colour is re-validated on every load: the database is the
        # writable surface here, and a bad row must not reach the <style> block
        # even if it somehow bypassed the settings form.
        self.settings.theme_accent = _safe_color(
            values['theme_accent'], self.settings.theme_accent)
        self.settings.theme_accent_strong = _safe_color(
            values['theme_accent_strong'], self.settings.theme_accent_strong)
        self.settings.portal_logo = values['portal_logo']
        self.settings.portal_footer_text = values['portal_footer_text']
        self.settings.login_max_attempts = int(values['login_max_attempts'])
        self.settings.login_lockout_seconds = int(values['login_lockout_seconds'])
        self.settings.ssh_whitelist_enabled = _as_bool(values['ssh_whitelist_enabled'])
        self.settings.ssh_whitelist_macs = values['ssh_whitelist_macs']
        self.settings.dos_protection_enabled = _as_bool(values['dos_protection_enabled'])
        self.settings.content_filter_enabled = _as_bool(values['content_filter_enabled'])
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

    def _handle_reassociation(self, mac_address, connected_seconds, prior_seconds):
        """A connected MAC's Wi-Fi association age reset unexpectedly.

        Logged, not acted on: a genuine cloned-MAC+IP bypass attempt looks
        exactly like a phone's radio sleeping and reconnecting, so this is a
        signal for the operator (Security page / audit log), not an automatic
        block. See NetworkController._check_reassociation for the reasoning.
        """
        self.user_manager.log_audit(
            'device_reassociated', target=mac_address,
            detail=(f"Wi-Fi association reset ({prior_seconds}s -> "
                    f"{connected_seconds}s) while already connected - "
                    "possible duplicate MAC/IP (cloned device)."))

    def handle_new_device(self, mac_address):
        info = self.user_manager.get_device_info(mac_address)
        if info and info.get('paused'):
            logger.info(f"Known device {mac_address} is paused, keeping it blocked")
            self.network_controller.block_mac(mac_address)
        elif info and info['time_balance'] > 0:
            logger.info(f"Known device {mac_address} has balance, restoring access")
            self.network_controller.unblock_mac(mac_address)
            self.network_controller.set_bandwidth_limit(
                mac_address, info['download_limit'], info['upload_limit'])
        else:
            self.network_controller.block_mac(mac_address)
