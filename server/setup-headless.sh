#!/usr/bin/env bash
# SreenLinux — make the PC work with no monitor connected (NVIDIA Jetson).
#
# With no monitor, the NVIDIA driver enables no output and GNOME draws
# nothing (the phone shows black), or falls back to 640x480. This script
# makes the driver believe a monitor is plugged into the DisplayPort
# (Option "ConnectedMonitor" + a generated "CustomEDID") and sets the
# desktop size. It takes effect after a reboot.
#
#   sudo ./setup-headless.sh              1920x1080 (default)
#   sudo ./setup-headless.sh 1920x1200    match a 16:10 tablet exactly
#   sudo ./setup-headless.sh --undo       restore the original xorg.conf
#
# The default 1920x1080 uses the standard 1080p60 timing, so a real 1080p
# monitor plugged into the same port still shows a picture. With other
# sizes, a real monitor may report "out of range" until you run --undo.
# If the graphical login ever fails to start, log in over SSH and run
#   sudo /path/to/setup-headless.sh --undo && sudo reboot

set -euo pipefail
CONF=/etc/X11/xorg.conf
BACKUP=/etc/X11/xorg.conf.sreenlinux.bak
EDID=/etc/X11/sreenlinux-edid.bin
OUTPUT=DFP-0   # the Jetson AGX Orin DisplayPort connector
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BEGIN="# >>> SreenLinux headless >>>"
END="# <<< SreenLinux headless <<<"

if [ "$(id -u)" -ne 0 ]; then
    echo "✗ Run with sudo:  sudo $0 $*"
    exit 1
fi

if [ "${1:-}" = "--undo" ]; then
    if [ -f "$BACKUP" ]; then
        cp -a "$BACKUP" "$CONF"
        echo "✓ Restored $CONF from $BACKUP. Reboot to apply."
    elif [ -f "$CONF" ]; then
        sed -i "/^$BEGIN\$/,/^$END\$/d" "$CONF"
        echo "✓ Removed SreenLinux section from $CONF. Reboot to apply."
    fi
    rm -f "$EDID"
    exit 0
fi

RES="${1:-1920x1080}"
if ! [[ "$RES" =~ ^([0-9]{3,4})x([0-9]{3,4})$ ]]; then
    echo "✗ Resolution must look like 1920x1080"
    exit 2
fi
W="${BASH_REMATCH[1]}"; H="${BASH_REMATCH[2]}"

if [ ! -f "$CONF" ]; then
    echo "✗ $CONF not found — this script is for NVIDIA Jetson (L4T)."
    exit 1
fi
DEVICE_ID=$(awk '/^[[:space:]]*Section[[:space:]]+"Device"/{d=1} d && /Identifier/{gsub(/"/,"",$2); print $2; exit}' "$CONF")
if [ -z "$DEVICE_ID" ]; then
    echo "✗ No Device section in $CONF — refusing to guess."
    exit 1
fi
if sed "/^$BEGIN\$/,/^$END\$/d" "$CONF" | grep -v "^[[:space:]]*#" | grep -qE '^[[:space:]]*Section[[:space:]]+"Screen"'; then
    echo "✗ $CONF already has its own Screen section; add 'Virtual $W $H' to its"
    echo "  Display subsection manually instead."
    exit 1
fi

# Generate the virtual monitor's EDID
/usr/bin/python3 "$SCRIPT_DIR/edid_gen.py" "${W}x${H}" "$EDID.tmp"
mv "$EDID.tmp" "$EDID"
chmod 644 "$EDID"

[ -f "$BACKUP" ] || cp -a "$CONF" "$BACKUP"
# Replace a previous SreenLinux section, if any
sed -i "/^$BEGIN\$/,/^$END\$/d" "$CONF"
cat >> "$CONF" <<EOF
$BEGIN
# Pretend a ${W}x${H} monitor is connected so GNOME draws even with no
# monitor attached (added by SreenLinux).
Section "Monitor"
    Identifier "SreenLinuxMonitor"
EndSection

Section "Screen"
    Identifier "SreenLinuxScreen"
    Device     "$DEVICE_ID"
    Monitor    "SreenLinuxMonitor"
    Option     "ConnectedMonitor" "$OUTPUT"
    Option     "CustomEDID" "$OUTPUT:$EDID"
    DefaultDepth 24
    SubSection "Display"
        Depth   24
        Virtual $W $H
    EndSubSection
EndSection
$END
EOF

echo "✓ $CONF updated: virtual ${W}x${H} monitor on $OUTPUT (backup: $BACKUP)"
if [ "${W}x${H}" != "1920x1080" ]; then
    echo "  ⚠ A real monitor plugged into the DisplayPort may now show 'out of range'"
    echo "    (use 1920x1080, or undo, if you still need one)."
fi
echo "  Reboot to apply. Undo with: sudo $0 --undo"
echo "  If the graphical login does not come up: SSH in and run the undo command."
