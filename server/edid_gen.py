#!/usr/bin/env python3
"""Generate a minimal EDID 1.3 blob for a virtual monitor.

Used by setup-headless.sh: NVIDIA's X driver can pretend a monitor is
connected ("ConnectedMonitor") and read its capabilities from a file
("CustomEDID"). Without a connected monitor GNOME draws nothing, so the
login screen captured for the phone is black.

    python3 edid_gen.py 1920x1080 out.bin     # CEA 1080p60 (works on any 1080p monitor)
    python3 edid_gen.py 1920x1200 out.bin     # CVT reduced blanking, via `cvt -r`
"""

from __future__ import annotations

import subprocess
import sys

# Standard CEA-861 1080p60 timing — accepted by practically every 1080p monitor,
# so a real monitor plugged into the same port still shows a picture.
CEA_1080P60 = dict(clock_khz=148500, h=(1920, 2008, 2052, 2200),
                   v=(1080, 1084, 1089, 1125), hpos=True, vpos=True)


def cvt_rb(width: int, height: int) -> dict:
    """Reduced-blanking 60 Hz timing from `cvt -r`."""
    out = subprocess.run(["cvt", "-r", str(width), str(height), "60"],
                         capture_output=True, text=True, check=True).stdout
    line = next(l for l in out.splitlines() if l.strip().startswith("Modeline"))
    parts = line.split()
    clock_mhz = float(parts[2])
    nums = [int(x) for x in parts[3:11]]
    flags = " ".join(parts[11:]).lower()
    return dict(clock_khz=int(round(clock_mhz * 1000)), h=tuple(nums[0:4]),
                v=tuple(nums[4:8]), hpos="+hsync" in flags, vpos="+vsync" in flags)


def detailed_timing(t: dict, width_mm: int, height_mm: int) -> bytes:
    ha, hss, hse, ht = t["h"]
    va, vss, vse, vt = t["v"]
    hblank, vblank = ht - ha, vt - va
    hfp, hsw = hss - ha, hse - hss
    vfp, vsw = vss - va, vse - vss
    clk = t["clock_khz"] // 10
    flags = 0x18 | (0x04 if t["vpos"] else 0) | (0x02 if t["hpos"] else 0)  # digital separate sync
    return bytes([
        clk & 0xFF, clk >> 8,
        ha & 0xFF, hblank & 0xFF, ((ha >> 8) << 4) | (hblank >> 8),
        va & 0xFF, vblank & 0xFF, ((va >> 8) << 4) | (vblank >> 8),
        hfp & 0xFF, hsw & 0xFF, ((vfp & 0xF) << 4) | (vsw & 0xF),
        ((hfp >> 8) << 6) | ((hsw >> 8) << 4) | ((vfp >> 4) << 2) | (vsw >> 4),
        width_mm & 0xFF, height_mm & 0xFF, ((width_mm >> 8) << 4) | (height_mm >> 8),
        0, 0, flags,
    ])


def standard_timing(width: int, height: int) -> bytes:
    """Standard timing entry (60 Hz) if the aspect ratio is representable."""
    aspect = {(16, 10): 0, (4, 3): 1, (5, 4): 2, (16, 9): 3}
    for (aw, ah), code in aspect.items():
        if width * ah == height * aw and 256 <= width <= 2288 and width % 8 == 0:
            return bytes([width // 8 - 31, code << 6])
    return bytes([0x01, 0x01])


def text_descriptor(tag: int, text: str) -> bytes:
    body = text.encode("ascii")[:13]
    if len(body) < 13:
        body += b"\n" + b" " * (12 - len(body))
    return bytes([0, 0, 0, tag, 0]) + body


def make_edid(width: int, height: int) -> bytes:
    t = CEA_1080P60 if (width, height) == (1920, 1080) else cvt_rb(width, height)
    w_mm, h_mm = 527, int(527 * height / width)  # ~24" diagonal
    hfreq_khz = t["clock_khz"] / t["h"][3]
    max_clock_10mhz = min(255, int(t["clock_khz"] / 10000) + 2)

    e = bytearray()
    e += bytes([0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00])       # header
    e += bytes([0x4E, 0x4C])                                           # manufacturer "SRL"
    e += bytes([0x01, 0x00])                                           # product code
    e += bytes([0, 0, 0, 0])                                           # serial
    e += bytes([1, 2026 - 1990])                                       # week, year
    e += bytes([1, 3])                                                 # EDID 1.3
    e += bytes([0x80, w_mm // 10, h_mm // 10, 0x78, 0x0A])             # digital, size, gamma 2.2, RGB + preferred DTD
    e += bytes([0xEE, 0x91, 0xA3, 0x54, 0x4C, 0x99, 0x26, 0x0F, 0x50, 0x54])  # sRGB chromaticity
    e += bytes([0, 0, 0])                                              # established timings
    e += standard_timing(width, height) + bytes([0x01, 0x01] * 7)     # 1 standard timing
    e += detailed_timing(t, w_mm, h_mm)                                # preferred mode
    e += bytes([0, 0, 0, 0xFD, 0,                                      # range limits
                50, 75,                                                # V Hz
                max(1, int(hfreq_khz) - 20), min(255, int(hfreq_khz) + 20),  # H kHz
                max_clock_10mhz, 0x00, 0x0A] + [0x20] * 6)
    e += text_descriptor(0xFC, "SreenLinux")                           # monitor name
    e += bytes([0, 0, 0, 0x10, 0] + [0] * 13)                          # dummy
    e += bytes([0])                                                    # no extensions
    e += bytes([(-sum(e)) & 0xFF])                                     # checksum
    assert len(e) == 128
    return bytes(e)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    w, h = (int(v) for v in sys.argv[1].lower().split("x"))
    data = make_edid(w, h)
    with open(sys.argv[2], "wb") as f:
        f.write(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
