#!/usr/bin/env bash
# SreenLinux — start the server automatically when you log in to the desktop.
#
#   ./install-autostart.sh                 install (mirror screen, open client on plug-in)
#   ./install-autostart.sh --virtual       install with extra server options
#   ./install-autostart.sh --uninstall     remove
#
# Default options are "--open". Any options you pass replace them.
# Log file: ~/.cache/sreenlinux/server.log

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
DESKTOP_FILE="$AUTOSTART_DIR/sreenlinux.desktop"
LOG_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/sreenlinux"

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$DESKTOP_FILE"
    echo "✓ Autostart removed ($DESKTOP_FILE)"
    echo "  A server that is already running keeps running until you log out"
    echo "  or stop it: pkill -f screen_server.py"
    exit 0
fi

# Resolve the real interpreter now: login-time autostart does not load
# ~/.bashrc, so pyenv/conda shims are not on PATH there.
PYTHON="$(python3 -c 'import sys; print(sys.executable)')"
if ! "$PYTHON" -c "import PIL" 2>/dev/null; then
    echo "✗ Pillow is not installed for $PYTHON — run ./setup.sh first."
    exit 1
fi
command -v adb >/dev/null || echo "⚠ adb not installed — USB auto-connect won't work until you run ./setup.sh"

if [ "$#" -gt 0 ]; then
    OPTS=("$@")
else
    OPTS=(--open)
fi
OPTS_STR=""
for o in "${OPTS[@]}"; do OPTS_STR+=" $(printf '%q' "$o")"; done

mkdir -p "$AUTOSTART_DIR" "$LOG_DIR"
# A short delay lets the X session and display layout settle first.
CMD="sleep 5; cd $(printf '%q' "$SCRIPT_DIR") && exec $(printf '%q' "$PYTHON") screen_server.py$OPTS_STR >> $(printf '%q' "$LOG_DIR/server.log") 2>&1"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=SreenLinux server
Comment=Use an Android device as a display for this PC (USB)
Exec=bash -c "${CMD//\"/\\\"}"
Icon=$SCRIPT_DIR/web/icon.png
Terminal=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=3
EOF

echo "✓ Autostart installed: $DESKTOP_FILE"
echo "  Command : $PYTHON screen_server.py$OPTS_STR"
echo "  Log     : $LOG_DIR/server.log"
echo ""
echo "It starts at your next login. To start it now without logging out:"
echo "  (cd $SCRIPT_DIR && nohup $PYTHON screen_server.py$OPTS_STR >> $LOG_DIR/server.log 2>&1 &)"
