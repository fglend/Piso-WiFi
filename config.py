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
_HEX_COLOR_RE = re.compile(r'^#[0-9A-Fa-f]{6}$')


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


def _parse_protected_devices(spec):
    """Parse 'MAC=IP,MAC=IP' into {MAC_UPPER: IP}.

    Raises RuntimeError naming the offending entry. Infrastructure that is
    silently dropped from this map gets blocked by the firewall's terminal
    DROP, so a malformed entry must stop startup, not be skipped.
    """
    devices = {}
    for raw in (spec or '').split(','):
        entry = raw.strip()
        if not entry:
            continue
        mac, separator, ip = entry.partition('=')
        mac, ip = mac.strip().upper(), ip.strip()
        if not separator or not mac or not ip:
            raise RuntimeError(
                'Invalid configuration: PROTECTED_DEVICES entries must look '
                f'like MAC=IP (got {entry!r})')
        if not _MAC_ADDRESS_RE.fullmatch(mac):
            raise RuntimeError(
                'Invalid configuration: PROTECTED_DEVICES contains an invalid '
                f'MAC address {mac!r}')
        if mac in devices:
            raise RuntimeError(
                'Invalid configuration: PROTECTED_DEVICES lists '
                f'{mac} more than once')
        devices[mac] = ip
    return devices


def is_valid_color(value):
    """True for a plain #rrggbb triple. Anything else is refused rather than
    sanitised: these values are written straight into a <style> block."""
    return bool(_HEX_COLOR_RE.match((value or '').strip()))


def _env_color(name, default):
    value = (os.getenv(name) or '').strip()
    if not value:
        return default
    if not is_valid_color(value):
        logger.warning(f"Invalid hex colour for {name}, using default {default}")
        return default
    return value


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
    # Operator branding. These land in a <style> block as CSS custom property
    # values, so a malformed colour would be injected markup - _env_color
    # rejects anything that is not a plain 6-digit hex triple.
    theme_accent: str = field(
        default_factory=lambda: _env_color('THEME_ACCENT', '#0f766e'))
    theme_accent_strong: str = field(
        default_factory=lambda: _env_color('THEME_ACCENT_STRONG', '#0c5f59'))
    # Filename inside static/uploads/, written by the admin logo upload.
    portal_logo: str = field(
        default_factory=lambda: os.getenv('PORTAL_LOGO', '').strip())
    portal_footer_text: str = field(
        default_factory=lambda: os.getenv('PORTAL_FOOTER_TEXT', '').strip())
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
    # Additional infrastructure devices for a multi-AP build (PoE switch plus
    # two or more APs): 'MAC=IP,MAC=IP'. Merged with the POE_AP_* pair above,
    # which stays supported so a single-AP .env needs no edit.
    protected_devices_spec: str = field(
        default_factory=lambda: os.getenv('PROTECTED_DEVICES', '').strip())
    ap_ssid: str = field(default_factory=lambda: os.getenv('AP_SSID', 'PisoWiFi'))
    ap_password: str = field(default_factory=lambda: os.getenv('AP_PASSWORD', 'pisowifi123'))
    ap_ip: str = field(default_factory=lambda: os.getenv('AP_IP', '192.168.4.1'))
    dhcp_range_start: str = field(default_factory=lambda: os.getenv('DHCP_RANGE_START', '192.168.4.2'))
    dhcp_range_end: str = field(default_factory=lambda: os.getenv('DHCP_RANGE_END', '192.168.4.20'))
    network_mask: str = field(default_factory=lambda: os.getenv('NETWORK_MASK', '255.255.255.0'))

    # Time manager. The deduction is elapsed-time based, so a longer interval
    # never loses metering accuracy - it only delays the block once a balance
    # hits zero (up to CHECK_INTERVAL of free browsing). 15s balances that leak
    # against poll cost (each poll spawns `ip neigh` + DB writes) on an SBC.
    check_interval: int = field(default_factory=lambda: _env_int('CHECK_INTERVAL', 15))
    # Metering is manual-pause based: a device's clock keeps counting down even
    # while it is disconnected (only the portal Pause button freezes it), so
    # this defaults off. Set PAUSE_ON_DISCONNECT=true to auto-freeze on drop.
    pause_on_disconnect: bool = field(default_factory=lambda: _env_bool('PAUSE_ON_DISCONNECT', False))
    # Whether customers get the portal's "Pause my time" button. Independent of
    # PAUSE_ON_DISCONNECT: the common setup is automatic pausing off (a package
    # expires on elapsed time) with manual pausing on, so a customer who knows
    # they are leaving can deliberately stop their own clock first.
    allow_manual_pause: bool = field(
        default_factory=lambda: _env_bool('ALLOW_MANUAL_PAUSE', True))
    # Spent devices idle this long are deleted, so the rows left behind by
    # rotated randomized MACs don't accumulate forever. A purged device is
    # simply unknown again (zero balance); set to 0 to keep every row.
    device_retention_hours: int = field(
        default_factory=lambda: _env_int('DEVICE_RETENTION_HOURS', 24))

    # Coinslot (GPIO pulse type, e.g. CH-926 / Weiyu universal)
    coinslot_enabled: bool = field(default_factory=lambda: _env_bool('COINSLOT_ENABLED', False))
    coinslot_gpio: int = field(default_factory=lambda: _env_int('COINSLOT_GPIO', 6))
    coinslot_pulses_per_peso: int = field(
        default_factory=lambda: _env_int('COINSLOT_PULSES_PER_PESO', 1))
    coinslot_claim_timeout: int = field(
        default_factory=lambda: _env_int('COINSLOT_CLAIM_TIMEOUT', 60))
    coinslot_debounce_ms: int = field(
        default_factory=lambda: _env_int('COINSLOT_DEBOUNCE_MS', 50))
    # Quiet gap that marks the end of one coin's pulse burst. Must be longer
    # than the acceptor's inter-pulse gap (so a ₱5 coin's pulses group into a
    # single tiered credit) but shorter than the time between two hand-inserted
    # coins. Raise it if a single coin is being split into multiple credits.
    coinslot_coin_settle_ms: int = field(
        default_factory=lambda: _env_int('COINSLOT_COIN_SETTLE_MS', 400))
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
        # Every extra infrastructure device gets the same checks. A typo here
        # means an AP or switch is treated as an unpaid client and blocked, so
        # this fails loudly at startup rather than being skipped.
        for mac, ip in _parse_protected_devices(
                self.protected_devices_spec).items():
            self._validate_protected_address(mac, ip)
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
        self._validate_coinslot_pins()
        return self._validate_credentials()

    def _validate_protected_address(self, mac, ip):
        """Same rules the single PoE AP has always had, applied per device."""
        try:
            management_ip = IPv4Address(ip)
            lan_network = IPv4Network(
                f'{self.ap_ip}/{self.network_mask}', strict=False)
            dhcp_start = IPv4Address(self.dhcp_range_start)
            dhcp_end = IPv4Address(self.dhcp_range_end)
        except (AddressValueError, ValueError):
            raise RuntimeError(
                f'Invalid configuration: PROTECTED_DEVICES entry {mac} has an '
                f'invalid IPv4 address {ip!r}')
        if management_ip not in lan_network or str(management_ip) == self.ap_ip:
            raise RuntimeError(
                f'Invalid configuration: PROTECTED_DEVICES entry {mac} must '
                'use a reserved address on the client LAN, not AP_IP')
        if dhcp_start <= management_ip <= dhcp_end:
            raise RuntimeError(
                f'Invalid configuration: PROTECTED_DEVICES entry {mac} must '
                'use an address outside the DHCP range')

    def protected_device_map(self):
        """MAC -> reserved IP for every infrastructure device.

        One PoE AP, or a switch plus several APs: the firewall keeps these
        permanently allowed and never captive-redirects them. The legacy
        POE_AP_* pair is folded in first so an existing single-AP .env keeps
        working with no edit; PROTECTED_DEVICES wins on a duplicate MAC.
        """
        devices = {}
        mac = self.poe_ap_mac_address.strip().upper()
        ip = self.poe_ap_ip_address.strip()
        if mac and ip:
            devices[mac] = ip
        devices.update(_parse_protected_devices(self.protected_devices_spec))
        return devices

    def _validate_coinslot_pins(self):
        if self.coinslot_enabled and self.coinslot_gpio == self.coinslot_relay_gpio:
            raise RuntimeError(
                'Invalid configuration: COINSLOT_GPIO and COINSLOT_RELAY_GPIO '
                'must be different pins (both are '
                f'{self.coinslot_gpio}). The pulse input and relay output '
                'cannot share one GPIO.')

    def _validate_credentials(self):
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
