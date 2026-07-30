"""Read-only host health metrics for the admin dashboard.

Every reader is best-effort and returns None when its source is missing or
unreadable, mirroring the degrade-rather-than-fail approach used for the
optional game lane in network/firewall.py. A dashboard must never 500 because
an SBC exposes a different sysfs layout than the one we expected.

Paths are parameters so tests can point them at fixtures instead of the host.
"""
import logging
import os
import shutil
import threading
import time

from network.command import command_exists, run_cmd

logger = logging.getLogger(__name__)

# Service state is the only expensive probe here (a fork per service). It
# changes on the timescale of a crash or a manual restart, not of a dashboard
# poll, so it is cached well past the default 10s refresh interval.
SERVICE_CACHE_TTL = 30.0
_service_lock = threading.Lock()
_service_cache = {'expires': 0.0, 'names': None, 'value': {}}

THERMAL_PATHS = (
    '/sys/class/thermal/thermal_zone0/temp',
    '/sys/devices/virtual/thermal/thermal_zone0/temp',
)
LOADAVG_PATH = '/proc/loadavg'
UPTIME_PATH = '/proc/uptime'
MEMINFO_PATH = '/proc/meminfo'
ROUTE_PATH = '/proc/net/route'

# Above this the SoC is throttling on most Pi-class boards; the UI warns.
TEMP_WARN_C = 70.0
TEMP_CRITICAL_C = 80.0
DISK_WARN_PERCENT = 85.0
MEMORY_WARN_PERCENT = 90.0


def _read_text(path):
    try:
        with open(path) as handle:
            return handle.read()
    except OSError as exc:
        logger.debug("Health source %s unavailable: %s", path, exc)
        return None


def soc_temperature_c(paths=THERMAL_PATHS):
    """SoC temperature in Celsius, or None when no thermal zone is exposed."""
    for path in paths:
        raw = _read_text(path)
        if raw is None:
            continue
        try:
            value = float(raw.strip())
        except ValueError:
            continue
        # Kernels report milli-Celsius; a bare Celsius value is also seen.
        return round(value / 1000.0 if value > 1000 else value, 1)
    return None


def cpu_load(path=LOADAVG_PATH, cpu_count=None):
    """1/5/15-minute load averages plus load-per-core for the 1-minute figure."""
    raw = _read_text(path)
    if raw is None:
        return None
    parts = raw.split()
    if len(parts) < 3:
        return None
    try:
        one, five, fifteen = (float(parts[i]) for i in range(3))
    except ValueError:
        return None
    cores = cpu_count or os.cpu_count() or 1
    return {
        'one': round(one, 2),
        'five': round(five, 2),
        'fifteen': round(fifteen, 2),
        'cores': cores,
        'percent': round(min(one / cores * 100.0, 100.0), 1),
    }


def memory(path=MEMINFO_PATH):
    """Total/available/used RAM in MB with a used percentage."""
    raw = _read_text(path)
    if raw is None:
        return None
    fields = {}
    for line in raw.splitlines():
        key, _, rest = line.partition(':')
        value = rest.strip().split(' ')[0]
        try:
            fields[key.strip()] = int(value)
        except ValueError:
            continue
    total_kb = fields.get('MemTotal')
    # MemAvailable is the honest figure (MemFree ignores reclaimable cache and
    # makes a healthy box look nearly out of memory).
    available_kb = fields.get('MemAvailable', fields.get('MemFree'))
    if not total_kb or available_kb is None:
        return None
    used_kb = max(total_kb - available_kb, 0)
    return {
        'total_mb': round(total_kb / 1024),
        'available_mb': round(available_kb / 1024),
        'used_mb': round(used_kb / 1024),
        'percent': round(used_kb / total_kb * 100.0, 1),
    }


def disk(path='/'):
    """Root filesystem usage in GB with a used percentage."""
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        logger.debug("Disk usage for %s unavailable: %s", path, exc)
        return None
    if not usage.total:
        return None
    return {
        'total_gb': round(usage.total / 1024 ** 3, 1),
        'used_gb': round(usage.used / 1024 ** 3, 1),
        'free_gb': round(usage.free / 1024 ** 3, 1),
        'percent': round(usage.used / usage.total * 100.0, 1),
    }


def uptime(path=UPTIME_PATH):
    """Uptime as raw seconds plus a short human label ('3d 4h')."""
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        seconds = float(raw.split()[0])
    except (IndexError, ValueError):
        return None
    return {'seconds': int(seconds), 'label': format_duration(seconds)}


def format_duration(seconds):
    """Compact duration label. Module level so templates and tests share it."""
    seconds = int(max(seconds, 0))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _probe_services(names):
    """One `systemctl is-active` per service. Callers go through
    service_status(), which caches this - it is the only part of a health
    snapshot that forks."""
    statuses = {}
    have_systemctl = command_exists('systemctl')
    for name in names:
        if not have_systemctl:
            statuses[name] = 'unknown'
            continue
        try:
            output = run_cmd(['systemctl', 'is-active', name],
                             ignore_errors=True) or ''
            state = output.strip()
            statuses[name] = 'running' if state == 'active' else 'stopped'
        except Exception as exc:
            logger.debug("Service probe for %s failed: %s", name, exc)
            statuses[name] = 'unknown'
    return statuses


def service_status(names, ttl=SERVICE_CACHE_TTL):
    """Map service name -> 'running' | 'stopped' | 'unknown'.

    Cached for `ttl` seconds. Every other reader in this module is a pseudo-
    file read costing microseconds, but this one forks `systemctl` per service
    - roughly two orders of magnitude more expensive, and systemctl also makes
    a dbus round-trip. The dashboard re-collects on every live poll (default
    every 10s, per open admin tab) on a single worker that is also serving
    captive-portal pages to phones, so probing every time would spend real SBC
    time re-answering a question whose answer changes maybe once a week.

    The probe runs while holding the lock so a burst of concurrent polls
    produces one fork, not one per request thread. Pass ttl=0 to force a
    fresh probe.
    """
    names = tuple(names)
    now = time.monotonic()
    with _service_lock:
        if (ttl > 0 and _service_cache['names'] == names
                and now < _service_cache['expires']):
            return dict(_service_cache['value'])
        statuses = _probe_services(names)
        _service_cache.update(names=names, value=statuses,
                              expires=now + ttl)
        return dict(statuses)


def _reset_service_cache():
    """Drop the cached probe. Used by tests; harmless at runtime."""
    with _service_lock:
        _service_cache.update(names=None, value={}, expires=0.0)


def uplink_online(interface, path=ROUTE_PATH):
    """True when a default route exists on the uplink interface.

    Reads /proc/net/route rather than pinging: the dashboard refreshes on a
    timer and a blocking ping would stall the request on a dead uplink.
    """
    raw = _read_text(path)
    if raw is None:
        return None
    for line in raw.splitlines()[1:]:
        fields = line.split()
        # columns: Iface Destination Gateway ... ; 00000000 == default route
        if len(fields) >= 3 and fields[0] == interface and fields[1] == '00000000':
            return True
    return False


def collect(settings=None, services=('hostapd', 'dnsmasq')):
    """Whole health snapshot for the dashboard. Never raises."""
    interface = getattr(settings, 'internet_interface', '') if settings else ''
    snapshot = {
        'temperature_c': soc_temperature_c(),
        'load': cpu_load(),
        'memory': memory(),
        'disk': disk(),
        'uptime': uptime(),
        'services': service_status(services),
        'uplink_interface': interface,
        'uplink_online': uplink_online(interface) if interface else None,
    }
    snapshot['alerts'] = _alerts(snapshot)
    return snapshot


def _alerts(snapshot):
    """Human-readable warnings, worst first. Empty list means all clear."""
    alerts = []
    temperature = snapshot.get('temperature_c')
    if temperature is not None and temperature >= TEMP_CRITICAL_C:
        alerts.append(('danger', f"SoC at {temperature}°C - likely throttling"))
    elif temperature is not None and temperature >= TEMP_WARN_C:
        alerts.append(('warn', f"SoC running warm at {temperature}°C"))

    disk_usage = snapshot.get('disk')
    if disk_usage and disk_usage['percent'] >= DISK_WARN_PERCENT:
        alerts.append(('warn', f"Disk {disk_usage['percent']}% full"))

    memory_usage = snapshot.get('memory')
    if memory_usage and memory_usage['percent'] >= MEMORY_WARN_PERCENT:
        alerts.append(('warn', f"Memory {memory_usage['percent']}% used"))

    for name, state in (snapshot.get('services') or {}).items():
        if state == 'stopped':
            alerts.append(('danger', f"{name} is not running"))

    if snapshot.get('uplink_online') is False:
        alerts.append(('danger', 'No default route on the uplink interface'))
    return alerts
