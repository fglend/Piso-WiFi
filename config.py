import os
import logging
import re
from ipaddress import IPv4Address, IPv4Network, AddressValueError
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_INSECURE_DEFAULTS = {'your-secret-key-here', 'admin123', 'pisowifi123'}
_MAC_ADDRESS_RE = re.compile(r'^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$')
_HOSTNAME_RE = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$')


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


def _env_bool(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in ('1', 'true', 'yes', 'on')


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

    # Portal/admin UI defaults. Admin changes can override these at runtime.
    portal_title: str = field(default_factory=lambda: os.getenv('PORTAL_TITLE', 'PISO WIFI Portal'))
    portal_subtitle: str = field(default_factory=lambda: os.getenv(
        'PORTAL_SUBTITLE', 'Only one phone can use the coin slot at a time.'))
    portal_hostname: str = field(
        default_factory=lambda: os.getenv(
            'PORTAL_HOSTNAME', 'glend-pisowifi').strip().lower())
    dashboard_refresh_seconds: int = field(
        default_factory=lambda: _env_int('DASHBOARD_REFRESH_SECONDS', 10))
    default_download_kbps: int = field(default_factory=lambda: _env_int('DEFAULT_DOWNLOAD_KBPS', 2048))
    default_upload_kbps: int = field(default_factory=lambda: _env_int('DEFAULT_UPLOAD_KBPS', 1024))

    # Database
    db_path: str = field(default_factory=lambda: os.getenv('DB_PATH', 'config/piso_wifi.db'))

    # Network
    # Set false for local Docker/dev runs that should exercise the web app and
    # database without configuring host WiFi, iptables, dnsmasq, or tc.
    manage_hardware: bool = field(default_factory=lambda: _env_bool('MANAGE_HARDWARE', True))
    dev_fake_mac: str = field(default_factory=lambda: os.getenv('DEV_FAKE_MAC', ''))
    # 'wired': the Pi is a wired gateway (clients come in via an external AP /
    # PoE router in bridge mode on the LAN interface). 'ap': the Pi broadcasts
    # its own hotspot with hostapd.
    network_mode: str = field(default_factory=lambda: os.getenv('NETWORK_MODE', 'ap'))
    # UDP ports/ranges (iptables multiport syntax, max 15 entries) treated as
    # game traffic and given the low-latency lane. Empty string disables.
    game_udp_ports: str = field(default_factory=lambda: os.getenv(
        'GAME_UDP_PORTS',
        '5000:5221,7086:7995,8001:8012,9330:9340,10012:10039,'
        '10101:10201,12235:12240,17000:18000,20561'))
    # Client-side interface. LAN_INTERFACE wins (wired setups); falls back to
    # WIFI_INTERFACE for AP mode.
    ap_interface: str = field(default_factory=lambda: os.getenv(
        'LAN_INTERFACE', os.getenv('WIFI_INTERFACE', 'wlan0')))
    internet_interface: str = field(default_factory=lambda: os.getenv('INTERNET_INTERFACE', 'wlan1'))
    # Management/bridge MAC of the external PoE access point. This device is
    # infrastructure, not a paying portal client, and must never be blocked.
    poe_ap_mac_address: str = field(
        default_factory=lambda: os.getenv(
            'POE_AP_MAC_ADDRESS', '').strip().upper())
    poe_ap_ip_address: str = field(
        default_factory=lambda: os.getenv('POE_AP_IP_ADDRESS', '').strip())
    ap_ssid: str = field(default_factory=lambda: os.getenv('AP_SSID', 'PisoWiFi'))
    ap_password: str = field(default_factory=lambda: os.getenv('AP_PASSWORD', 'pisowifi123'))
    ap_ip: str = field(default_factory=lambda: os.getenv('AP_IP', '192.168.4.1'))
    dhcp_range_start: str = field(default_factory=lambda: os.getenv('DHCP_RANGE_START', '192.168.4.2'))
    dhcp_range_end: str = field(default_factory=lambda: os.getenv('DHCP_RANGE_END', '192.168.4.20'))
    network_mask: str = field(default_factory=lambda: os.getenv('NETWORK_MASK', '255.255.255.0'))

    # Time manager
    check_interval: int = field(default_factory=lambda: _env_int('CHECK_INTERVAL', 5))
    pause_on_disconnect: bool = field(default_factory=lambda: _env_bool('PAUSE_ON_DISCONNECT', True))

    # Coinslot (GPIO pulse type, e.g. CH-926 / Weiyu universal)
    coinslot_enabled: bool = field(default_factory=lambda: _env_bool('COINSLOT_ENABLED', False))
    coinslot_gpio: int = field(default_factory=lambda: _env_int('COINSLOT_GPIO', 6))
    coinslot_pulses_per_peso: int = field(
        default_factory=lambda: _env_int('COINSLOT_PULSES_PER_PESO', 1))
    coinslot_claim_timeout: int = field(
        default_factory=lambda: _env_int('COINSLOT_CLAIM_TIMEOUT', 60))
    coinslot_debounce_ms: int = field(
        default_factory=lambda: _env_int('COINSLOT_DEBOUNCE_MS', 50))
    # Relay that switches power to the acceptor: energized only while a claim
    # is active, so the acceptor is electrically dead the rest of the time.
    coinslot_relay_gpio: int = field(default_factory=lambda: _env_int('COINSLOT_RELAY_GPIO', 7))
    # Most cheap opto-isolated relay boards trigger the relay when IN is
    # pulled LOW ("active low"). Set true only if yours energizes on HIGH.
    coinslot_relay_active_high: bool = field(
        default_factory=lambda: _env_bool('COINSLOT_RELAY_ACTIVE_HIGH', False))

    @property
    def is_production(self):
        return self.env == 'production'

    def validate(self):
        """Refuse to run in production with known-default credentials."""
        if not _HOSTNAME_RE.fullmatch(self.portal_hostname):
            raise RuntimeError(
                'Invalid configuration: PORTAL_HOSTNAME must be a single '
                'DNS label using only letters, numbers, and hyphens')
        poe_ap_mac = self.poe_ap_mac_address.strip()
        poe_ap_ip = self.poe_ap_ip_address.strip()
        if poe_ap_mac and not _MAC_ADDRESS_RE.fullmatch(poe_ap_mac):
            raise RuntimeError(
                'Invalid configuration: POE_AP_MAC_ADDRESS must be a '
                'colon-separated MAC address')
        if bool(poe_ap_mac) != bool(poe_ap_ip):
            raise RuntimeError(
                'Invalid configuration: POE_AP_MAC_ADDRESS and '
                'POE_AP_IP_ADDRESS must be set together')
        if poe_ap_ip:
            try:
                management_ip = IPv4Address(poe_ap_ip)
                lan_network = IPv4Network(
                    f'{self.ap_ip}/{self.network_mask}', strict=False)
                dhcp_start = IPv4Address(self.dhcp_range_start)
                dhcp_end = IPv4Address(self.dhcp_range_end)
            except (AddressValueError, ValueError):
                raise RuntimeError(
                    'Invalid configuration: POE_AP_IP_ADDRESS must be a '
                    'valid IPv4 address on the client LAN')
            if management_ip not in lan_network or str(management_ip) == self.ap_ip:
                raise RuntimeError(
                    'Invalid configuration: POE_AP_IP_ADDRESS must be a '
                    'reserved address on the client LAN, not AP_IP')
            if dhcp_start <= management_ip <= dhcp_end:
                raise RuntimeError(
                    'Invalid configuration: POE_AP_IP_ADDRESS must be outside '
                    'the DHCP range')
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
