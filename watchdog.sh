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

status=$(curl --max-time 10 -s -o /dev/null -w '%{http_code}' "$URL" 2>/dev/null)

if [ "$status" = "200" ]; then
    exit 0
fi

logger -t "$LOG_TAG" "health check failed (http_code=${status:-none}), restarting pisowifi"
systemctl restart pisowifi
