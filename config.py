import os
import logging
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_INSECURE_DEFAULTS = {'your-secret-key-here', 'admin123', 'pisowifi123'}


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        logger.warning(f"Invalid integer for {name}, using default {default}")
        return int(default)


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        logger.warning(f"Invalid float for {name}, using default {default}")
        return float(default)


@dataclass
class Settings:
    # Flask
    env: str = field(default_factory=lambda: os.getenv('FLASK_ENV', 'development'))
    secret_key: str = field(default_factory=lambda: os.getenv('SECRET_KEY', 'your-secret-key-here'))
    host: str = field(default_factory=lambda: os.getenv('FLASK_HOST', '0.0.0.0'))
    port: int = field(default_factory=lambda: _env_int('FLASK_PORT', 5000))

    # Admin credentials. ADMIN_PASSWORD_HASH (werkzeug hash) takes precedence
    # over the plaintext ADMIN_PASSWORD fallback.
    admin_username: str = field(default_factory=lambda: os.getenv('ADMIN_USERNAME', 'admin'))
    admin_password: str = field(default_factory=lambda: os.getenv('ADMIN_PASSWORD', 'admin123'))
    admin_password_hash: str = field(default_factory=lambda: os.getenv('ADMIN_PASSWORD_HASH', ''))

    # Pricing: minutes of access granted per peso
    minutes_per_peso: float = field(default_factory=lambda: _env_float('RATE_MINUTES_PER_PESO', 5.0))

    # Database
    db_path: str = field(default_factory=lambda: os.getenv('DB_PATH', 'config/piso_wifi.db'))

    # Network
    # 'wired': the Pi is a wired gateway (clients come in via an external AP /
    # PoE router in bridge mode on the LAN interface). 'ap': the Pi broadcasts
    # its own hotspot with hostapd.
    network_mode: str = field(default_factory=lambda: os.getenv('NETWORK_MODE', 'ap'))
    # Client-side interface. LAN_INTERFACE wins (wired setups); falls back to
    # WIFI_INTERFACE for AP mode.
    ap_interface: str = field(default_factory=lambda: os.getenv(
        'LAN_INTERFACE', os.getenv('WIFI_INTERFACE', 'wlan0')))
    internet_interface: str = field(default_factory=lambda: os.getenv('INTERNET_INTERFACE', 'wlan1'))
    ap_ssid: str = field(default_factory=lambda: os.getenv('AP_SSID', 'PisoWiFi'))
    ap_password: str = field(default_factory=lambda: os.getenv('AP_PASSWORD', 'pisowifi123'))
    ap_ip: str = field(default_factory=lambda: os.getenv('AP_IP', '192.168.4.1'))
    dhcp_range_start: str = field(default_factory=lambda: os.getenv('DHCP_RANGE_START', '192.168.4.2'))
    dhcp_range_end: str = field(default_factory=lambda: os.getenv('DHCP_RANGE_END', '192.168.4.20'))
    network_mask: str = field(default_factory=lambda: os.getenv('NETWORK_MASK', '255.255.255.0'))

    # Time manager
    check_interval: int = field(default_factory=lambda: _env_int('CHECK_INTERVAL', 5))
    pause_on_disconnect: bool = field(
        default_factory=lambda: os.getenv('PAUSE_ON_DISCONNECT', 'true').lower() in ('1', 'true', 'yes'))

    # Coinslot (GPIO pulse type, e.g. CH-926)
    coinslot_enabled: bool = field(
        default_factory=lambda: os.getenv('COINSLOT_ENABLED', 'false').lower() in ('1', 'true', 'yes'))
    coinslot_gpio: int = field(default_factory=lambda: _env_int('COINSLOT_GPIO', 6))
    coinslot_pulses_per_peso: int = field(
        default_factory=lambda: _env_int('COINSLOT_PULSES_PER_PESO', 1))
    coinslot_claim_timeout: int = field(
        default_factory=lambda: _env_int('COINSLOT_CLAIM_TIMEOUT', 60))
    coinslot_debounce_ms: int = field(
        default_factory=lambda: _env_int('COINSLOT_DEBOUNCE_MS', 50))

    @property
    def is_production(self):
        return self.env == 'production'

    def validate(self):
        """Refuse to run in production with known-default credentials."""
        problems = []
        if self.is_production:
            if self.secret_key in _INSECURE_DEFAULTS:
                problems.append('SECRET_KEY is set to the default value')
            if not self.admin_password_hash and self.admin_password in _INSECURE_DEFAULTS:
                problems.append('ADMIN_PASSWORD is set to the default value '
                                '(set ADMIN_PASSWORD_HASH or a strong ADMIN_PASSWORD)')
        if problems:
            raise RuntimeError('Refusing to start in production: ' + '; '.join(problems))
        if not self.is_production:
            if self.secret_key in _INSECURE_DEFAULTS:
                logger.warning('SECRET_KEY is a default value - change it before deploying')
            if not self.admin_password_hash and self.admin_password in _INSECURE_DEFAULTS:
                logger.warning('ADMIN_PASSWORD is a default value - change it before deploying')
        return self


def load_settings():
    return Settings().validate()
