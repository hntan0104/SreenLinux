#!/usr/bin/env bash
# SreenLinux — easy launcher
# Checks prerequisites, ensures ADB forwarding, and starts the server.
# All arguments are passed through to screen_server.py, e.g.
#   ./start.sh --open            open the client on the phone whenever it is plugged in
#   ./start.sh --virtual         extended display instead of mirroring
# The server watches USB and re-applies `adb reverse` every time a phone is
# plugged in, so it can be started before the cable is connected.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Pre-flight checks ---
if ! command -v python3 &>/dev/null; then
    echo "✗ python3 not found. Run ./setup.sh first."
    exit 1
fi
if ! python3 -c "import PIL" 2>/dev/null; then
    echo "✗ Pillow not installed for $(command -v python3). Run ./setup.sh"
    echo "  or: python3 -m pip install Pillow"
    exit 1
fi
if [ -z "${DISPLAY:-}" ]; then
    echo "✗ \$DISPLAY is not set — run this from inside your desktop session."
    exit 1
fi
if [ "${XDG_SESSION_TYPE:-x11}" = "wayland" ]; then
    echo "⚠ Wayland session detected — capture will likely fail. Use 'Ubuntu on Xorg'."
fi

echo "=== SreenLinux server ==="

if ! command -v adb &>/dev/null; then
    echo "⚠ adb not installed — USB won't work (sudo apt install adb). Wi-Fi only: --host 0.0.0.0"
fi

exec python3 "$SCRIPT_DIR/screen_server.py" "$@"
