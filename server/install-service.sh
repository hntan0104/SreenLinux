#!/usr/bin/env bash
# SreenLinux — install as a system service that starts at boot and shows the
# login screen on the phone (type your password with the on-screen keyboard),
# then your desktop after you log in.
#
#   sudo ./install-service.sh                       install / update
#   sudo ./install-service.sh -- --virtual          extra screen_server.py options
#   sudo ./install-service.sh --no-open             don't auto-open the client on plug-in
#   sudo ./install-service.sh --uninstall           remove
#
# Files are copied to /opt/sreenlinux (the login screen runs as user "gdm",
# which cannot read your home directory). Re-run after updating the code.

set -euo pipefail
trap 'echo "✗ install-service.sh failed at line $LINENO — see messages above." >&2' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/opt/sreenlinux
UNIT=/etc/systemd/system/sreenlinux.service
PORT=6100

if [ "$(id -u)" -ne 0 ]; then
    echo "✗ Run with sudo:  sudo $0 $*"
    exit 1
fi
TARGET_USER="${SUDO_USER:-}"
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = root ]; then
    echo "✗ Run this via sudo from your normal user account (needed for adb)."
    exit 1
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

stop_user_servers() {
    # The per-user autostart version would fight over port $PORT
    local f="$TARGET_HOME/.config/autostart/sreenlinux.desktop"
    if [ -f "$f" ]; then
        rm -f "$f"
        echo "  ✓ Removed per-user autostart ($f)"
    fi
    # Only the per-user copies — not the service's own child in $DEST
    local pids="" pid
    for pid in $(pgrep -u "$TARGET_USER" -f "screen_server.py" || true); do
        tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "$DEST/" || pids+="$pid "
    done
    pids="${pids% }"
    if [ -n "$pids" ]; then
        kill $pids 2>/dev/null || true
        echo "  ✓ Stopped running server(s): $pids"
    fi
}

if [ "${1:-}" = "--uninstall" ]; then
    systemctl disable --now sreenlinux.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    rm -rf "$DEST"
    echo "✓ SreenLinux service removed."
    exit 0
fi

OPEN_FLAG="--open"
EXTRA=()
while [ $# -gt 0 ]; do
    case "$1" in
        --no-open) OPEN_FLAG="" ;;
        --) shift; EXTRA=("$@"); break ;;
        *) echo "✗ Unknown option $1"; exit 2 ;;
    esac
    shift
done

echo "=== Installing SreenLinux system service ==="

# --- Requirements ---
echo "[1/4] Checking requirements…"
if ! /usr/bin/python3 -c "import PIL" 2>/dev/null; then
    apt-get install -y python3-pil
fi
command -v adb >/dev/null || apt-get install -y adb
command -v loginctl >/dev/null || { echo "✗ systemd-logind (loginctl) is required"; exit 1; }
if grep -qiE '^\s*WaylandEnable\s*=\s*true' /etc/gdm3/custom.conf 2>/dev/null || \
   ! grep -qiE '^\s*WaylandEnable\s*=\s*false' /etc/gdm3/custom.conf 2>/dev/null; then
    echo "  ⚠ GDM may use Wayland, which cannot be captured. Set WaylandEnable=false"
    echo "    in /etc/gdm3/custom.conf and reboot."
fi

# --- Copy files ---
echo "[2/4] Copying files to $DEST…"
rm -rf "$DEST"
install -d -m 755 "$DEST" "$DEST/web"
install -m 644 "$SCRIPT_DIR"/*.py "$DEST/"
install -m 644 "$SCRIPT_DIR"/web/* "$DEST/web/"

# --- systemd unit ---
echo "[3/4] Writing $UNIT…"
EXTRA_STR=""
if [ ${#EXTRA[@]} -gt 0 ]; then
    EXTRA_STR=" --"
    for o in "${EXTRA[@]}"; do EXTRA_STR+=" $(printf '%q' "$o")"; done
fi
cat > "$UNIT" <<EOF
[Unit]
Description=SreenLinux - use an Android device as this PC's display (USB)
After=systemd-logind.service display-manager.service
Wants=display-manager.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 $DEST/sreenlinux_service.py --port $PORT --adb-user $TARGET_USER $OPEN_FLAG$EXTRA_STR
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=graphical.target
EOF

# --- Enable ---
echo "[4/4] Starting service…"
stop_user_servers
systemctl daemon-reload
systemctl enable sreenlinux.service >/dev/null
systemctl restart sreenlinux.service
sleep 3

if systemctl is-active --quiet sreenlinux.service; then
    echo ""
    echo "✓ SreenLinux service is running and will start at every boot."
    echo ""
    journalctl -u sreenlinux.service -n 8 --no-pager -o cat || true
    echo ""
    echo "  Logs   : journalctl -u sreenlinux -f"
    echo "  Stop   : sudo systemctl stop sreenlinux"
    echo "  Remove : sudo $SCRIPT_DIR/install-service.sh --uninstall"
else
    echo "✗ Service failed to start:"
    journalctl -u sreenlinux.service -n 30 --no-pager -o cat || true
    exit 1
fi
