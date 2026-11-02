#!/bin/sh
# run-bridge.sh - start pc-bridge.py on Linux (Ubuntu).
#
# Usage:
#   ./run-bridge.sh            # foreground; Ctrl-C stops the bridge
#   nohup ./run-bridge.sh &    # background (or use pc-bridge.service)
#
# Configuration: edit the defaults below, or override them with
# environment variables, e.g.:
#   ALLOW_NODE=mynode ALLOW_WRITE="$HOME/work" ./run-bridge.sh
#
# The bridge binds only to the Tailscale interface IP, never 0.0.0.0,
# so it is reachable only from your tailnet. Stopping this script (or
# the systemd service) disconnects the bridge immediately.

set -eu

# ---- settings (edit if needed) ----
ALLOW_NODE="${ALLOW_NODE:-your-control-node}"  # Tailscale node name of the control side
ALLOW_READ="${ALLOW_READ:-$HOME}"              # dirs the bridge can READ (comma-separated)
ALLOW_WRITE="${ALLOW_WRITE:-}"                # dirs the bridge can WRITE (empty = no write)
ALLOW_CMD="${ALLOW_CMD:-hdc}"                  # allowed command prefixes (comma-separated)
BIND="${BIND:-}"                               # bind IP override (empty = Tailscale interface IP)
PORT="${PORT:-18743}"
# -----------------------------------

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

command -v python3 >/dev/null 2>&1 || {
    echo "pc-bridge: python3 not found. Install it: sudo apt install python3" >&2
    exit 1
}
if [ -z "$BIND" ]; then
    command -v tailscale >/dev/null 2>&1 || {
        echo "pc-bridge: tailscale not found. Install it and run 'sudo tailscale up' first." >&2
        exit 1
    }
fi
if [ ! -f "$SCRIPT_DIR/pc-bridge.py" ]; then
    echo "pc-bridge: pc-bridge.py not found next to $0" >&2
    exit 1
fi

echo "Starting pc-bridge.py ..."
echo "  allow-node : $ALLOW_NODE"
echo "  allow-read : $ALLOW_READ"
echo "  allow-write: ${ALLOW_WRITE:-(none)}"
echo "  allow-cmd  : $ALLOW_CMD"
echo "  port       : $PORT"
echo "Stop with Ctrl-C (or: systemctl stop pc-bridge, when run under systemd)."
echo

if [ -n "$BIND" ]; then
    exec python3 "$SCRIPT_DIR/pc-bridge.py" \
        --bind "$BIND" \
        --port "$PORT" \
        --allow-node "$ALLOW_NODE" \
        --allow-dir "$ALLOW_READ" \
        --allow-write-dir "$ALLOW_WRITE" \
        --allow-cmd "$ALLOW_CMD"
else
    exec python3 "$SCRIPT_DIR/pc-bridge.py" \
        --port "$PORT" \
        --allow-node "$ALLOW_NODE" \
        --allow-dir "$ALLOW_READ" \
        --allow-write-dir "$ALLOW_WRITE" \
        --allow-cmd "$ALLOW_CMD"
fi
