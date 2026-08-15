"""Network-wide site content blocking via a dnsmasq drop-in file.

orange-piso does this with a proprietary binary manipulating iptables for
deep packet inspection. This project already runs dnsmasq for the captive
LAN (see system_info.py's service checks), so the portable equivalent is a
dnsmasq address=/domain/0.0.0.0 blackhole list - simpler, and it blocks by
name for every protocol at once instead of chasing IPs.
"""
import logging
import os

from network.command import command_exists, run_cmd

logger = logging.getLogger(__name__)

DEFAULT_CONF_PATH = '/etc/dnsmasq.d/piso_blocklist.conf'


def render_conf(entries):
    """dnsmasq drop-in file content for the enabled blocklist entries.

    Each pattern is a bare domain (e.g. "example.com"); dnsmasq's
    address=/domain/ syntax also matches subdomains of it.
    """
    lines = [
        '# Managed by piso_wifi admin content filter. Do not edit by hand -',
        '# changes are overwritten the next time the blocklist is saved.',
    ]
    for entry in sorted(entries, key=lambda e: e['pattern']):
        if not entry.get('enabled'):
            continue
        pattern = entry['pattern'].strip().lower()
        if not pattern:
            continue
        lines.append(f'address=/{pattern}/0.0.0.0')
    return '\n'.join(lines) + '\n'


def apply_blocklist(entries, manage_hardware=True, conf_path=DEFAULT_CONF_PATH):
    """Write the dnsmasq drop-in file and reload the service.

    manage_hardware=False (dev/Docker/tests) skips both the write and the
    reload, matching the convention used elsewhere for environments that
    exercise the web app without touching host networking.
    """
    if not manage_hardware:
        logger.info("Content filter: manage_hardware is false, skipping "
                    "dnsmasq write/reload")
        return True
    try:
        os.makedirs(os.path.dirname(conf_path), exist_ok=True)
        with open(conf_path, 'w') as f:
            f.write(render_conf(entries))
    except OSError as exc:
        logger.error("Could not write content filter conf %s: %s", conf_path, exc)
        return False

    if not command_exists('systemctl'):
        logger.warning("systemctl not found, dnsmasq not reloaded")
        return True
    try:
        run_cmd(['systemctl', 'reload', 'dnsmasq'], ignore_errors=True)
        logger.info("Content filter applied and dnsmasq reloaded")
        return True
    except Exception as exc:
        logger.error("Could not reload dnsmasq: %s", exc)
        return False
