#!/usr/bin/env bash
# Stop the Hermes gateway (messaging-platform) systemd *user* service.
#
# Usage:
#   bash scripts/gateway-stop.sh
#
# Why this script: the gateway runs as `hermes-gateway.service` under the
# per-user systemd instance, which needs XDG_RUNTIME_DIR / the session D-Bus
# to be reachable. SSH or exec-style shells often lack those, producing
# "Failed to connect to bus: No medium found". This sets them, then stops.
#
# Note: the gateway exits with code 1 on SIGTERM by design (so a real crash
# is auto-revived by Restart=always). systemd therefore marks the unit
# "failed" after a clean stop even though the process is gone. We run
# reset-failed afterward so `status` reads "inactive". See
# docs/gateway-service.md.
set -euo pipefail

SERVICE=hermes-gateway.service
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"

systemctl --user stop "$SERVICE"
# The post-stop "failed" result is expected and cosmetic; clear it.
systemctl --user reset-failed "$SERVICE" 2>/dev/null || true

echo "hermes-gateway.service stopped (state: $(systemctl --user is-active "$SERVICE" 2>/dev/null || true))"
