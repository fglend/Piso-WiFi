#!/bin/bash
# External liveness check for the pisowifi service.
#
# systemd's Restart=always only fires when the process exits - it cannot see
# a worker that is still running but wedged (e.g. the metering thread stuck
# in a hung shell-out). This script is the outside eye: it hits /health,
# which reports whether the metering loop is still ticking, and restarts the
# service if the process is unresponsive or reports unhealthy.
#
# Installed as a systemd timer (see install_ubuntu.sh) rather than cron so it
# shares the same logging/enable/disable surface as the service itself.
set -u

URL="http://127.0.0.1:5000/health"
LOG_TAG="pisowifi-watchdog"
# Minimum gap between watchdog-triggered restarts. A stuck service that
# stays unhealthy across restarts (a real bug, not a transient hang) would
# otherwise get restarted every single minute forever - each restart reopens
# the SQLite DB and writes session state, which is real, repeated SD-card
# wear for a fix that clearly isn't working. One restart attempt per window
# is enough for a watchdog; anything more persistent needs a human.
COOLDOWN_SECONDS=300
# /run is tmpfs (RAM, cleared on reboot) - this state never touches the SD
# card, matching journald's Storage=volatile config for the same reason.
STATE_FILE="/run/pisowifi-watchdog.last-restart"

status=$(curl --max-time 10 -s -o /dev/null -w '%{http_code}' "$URL" 2>/dev/null)

if [ "$status" = "200" ]; then
    exit 0
fi

now=$(date +%s)
last=0
[ -f "$STATE_FILE" ] && last=$(cat "$STATE_FILE" 2>/dev/null || echo 0)

if [ $((now - last)) -lt "$COOLDOWN_SECONDS" ]; then
    logger -t "$LOG_TAG" "health check failed (http_code=${status:-none}), still within cooldown - not restarting again"
    exit 0
fi

echo "$now" > "$STATE_FILE"
logger -t "$LOG_TAG" "health check failed (http_code=${status:-none}), restarting pisowifi"
systemctl restart pisowifi
