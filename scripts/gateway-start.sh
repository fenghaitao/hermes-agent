#!/usr/bin/env bash
# Start the Hermes gateway (messaging-platform) systemd *user* service.
#
# Usage:
#   bash scripts/gateway-start.sh
#
# Why this script: the gateway runs as `hermes-gateway.service` under the
# per-user systemd instance, which needs XDG_RUNTIME_DIR / the session D-Bus
# to be reachable. SSH or exec-style shells often lack those, producing
# "Failed to connect to bus: No medium found". This sets them, then starts.
# See docs/gateway-service.md.
set -euo pipefail

SERVICE=hermes-gateway.service
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"

# Clear any cosmetic "failed" state from a previous SIGTERM stop (the gateway
# exits non-zero on SIGTERM by design) so start is clean.
systemctl --user reset-failed "$SERVICE" 2>/dev/null || true
systemctl --user start "$SERVICE"

sleep 2
systemctl --user --no-pager status "$SERVICE" | head -6
