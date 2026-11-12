#!/usr/bin/env bash
# SreenLinux — server setup script
# Installs system dependencies and configures ADB port forwarding.

set -euo pipefail
trap 'echo "✗ setup.sh failed at line $LINENO (exit $?) — see the messages above." >&2' ERR
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${SREEN_PORT:-6100}"

echo "=== SreenLinux server setup ==="

# --- System packages ---
echo "[1/4] Installing system dependencies…"
if command -v apt-get &>/dev/null; then
    sudo apt-get update
    # libxtst6/libxext6/libxfixes3: fast native capture (MIT-SHM), input (XTest)
    # and cursor overlay (XFixes). xdotool/x11-utils are fallbacks.
    sudo apt-get install -y \
        python3 \
        python3-pil \
        libx11-6 libxext6 libxtst6 libxfixes3 \
        xdotool \
        adb \
        x11-utils \
        x11-xserver-utils
else
    echo "  ⚠ apt-get not found — install manually: python3, Pillow, adb, xdotool,"
    echo "    xrandr, xwd and the libX11/libXext/libXtst/libXfixes libraries."
fi

# --- Python packages ---
echo "[2/4] Checking Python requirements…"
if python3 -c "import PIL" 2>/dev/null; then
    echo "  ✓ Pillow $(python3 -c 'import PIL; print(PIL.__version__)')"
else
    # Use `python3 -m pip` so Pillow lands in the interpreter that runs the
    # server (pyenv/conda/venv), not whatever `pip3` happens to be on PATH.
    # Debian/Ubuntu 23.04+ forbid system-wide pip installs (PEP 668).
    python3 -m pip install --quiet -r "$SCRIPT_DIR/requirements.txt" 2>/dev/null || \
        python3 -m pip install --user --quiet -r "$SCRIPT_DIR/requirements.txt" 2>/dev/null || \
        python3 -m pip install --user --quiet --break-system-packages -r "$SCRIPT_DIR/requirements.txt"
    python3 -c "import PIL" && echo "  ✓ Pillow installed for $(command -v python3)"
fi

# --- Make scripts executable ---
echo "[3/4] Setting permissions…"
chmod +x "$SCRIPT_DIR/screen_server.py" "$SCRIPT_DIR/start.sh"

# --- Session type check ---
if [ "${XDG_SESSION_TYPE:-x11}" = "wayland" ]; then
    echo "  ⚠ You are running Wayland. SreenLinux needs an Xorg session —"
    echo "    log out and pick 'Ubuntu on Xorg' at the login screen."
fi

# --- ADB port forwarding ---
echo "[4/4] Setting up ADB port forwarding…"
if command -v adb &>/dev/null; then
    adb start-server >/dev/null 2>&1 || true
    if adb devices | grep -q "device$"; then
        adb reverse "tcp:$PORT" "tcp:$PORT"
        echo "  ✓ ADB reverse forwarding active (tcp:$PORT → tcp:$PORT)"
    else
        echo "  ⚠ No Android device detected. Connect your device and run:"
        echo "    adb reverse tcp:$PORT tcp:$PORT"
    fi
else
    echo "  ⚠ adb not found even after install — check your PATH"
fi

echo ""
echo "=== Setup complete ==="
echo "Run ./start.sh to launch the server."
