#!/usr/bin/env python3
"""Virtual display manager for SreenLinux.

Creates and removes virtual displays via xrandr so the Android device
can act as an extended monitor rather than mirroring the primary screen.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from typing import Optional

log = logging.getLogger(__name__)

# Common Android-friendly resolutions
PRESETS = {
    "1080p": (1920, 1080, 60),
    "1440p": (2560, 1440, 60),
    "720p":  (1280, 720, 60),
    "800p":  (1280, 800, 60),   # common tablet
    "2k":    (2048, 1080, 60),
}


def parse_resolution(spec: str) -> tuple[int, int, int]:
    """Parse a preset name or ``WxH`` / ``WxH@R`` into (w, h, refresh)."""
    key = spec.strip().lower()
    if key in PRESETS:
        return PRESETS[key]
    m = re.fullmatch(r"(\d+)x(\d+)(?:@(\d+))?", key)
    if not m:
        raise ValueError(f"Invalid resolution '{spec}'. Use one of "
                         f"{', '.join(PRESETS)} or WxH (e.g. 2340x1080)")
    w, h = int(m.group(1)), int(m.group(2))
    r = int(m.group(3) or 60)
    if not (320 <= w <= 8192 and 240 <= h <= 8192):
        raise ValueError(f"Resolution {w}x{h} out of range")
    return w, h, r


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    log.debug("Running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _modeline(width: int, height: int, refresh: float = 60.0) -> tuple[str, list[str]]:
    """Generate an xrandr modeline using `cvt`."""
    if not shutil.which("cvt"):
        raise RuntimeError("`cvt` not found (install xserver-xorg-core or libxcvt)")
    r = _run(["cvt", str(width), str(height), str(int(refresh))])
    # Modeline "1920x1080_60.00"  173.00  1920 2048 ...
    for line in r.stdout.splitlines():
        if line.strip().startswith("Modeline"):
            parts = line.split(None, 2)
            name = parts[1].strip('"')
            params = parts[2].split()
            return name, params
    raise RuntimeError(f"cvt failed to produce a modeline: {r.stdout}")


def _query() -> str:
    return _run(["xrandr", "--query"]).stdout


def _find_virtual_output() -> Optional[str]:
    """Find a disconnected output to hijack, preferring VIRTUAL*."""
    out = _query()
    for line in out.splitlines():
        m = re.match(r"^(VIRTUAL[-\d]*)\s+disconnected", line)
        if m:
            return m.group(1)
    # Some drivers expose e.g. HDMI-1-1 or DP-1-1 as disconnected
    for line in out.splitlines():
        m = re.match(r"^(\S+)\s+disconnected", line)
        if m:
            return m.group(1)
    return None


class VirtualDisplay:
    """Manages a single virtual xrandr output."""

    def __init__(self):
        self.mode_name: Optional[str] = None
        self.output: Optional[str] = None
        self._mode_created = False
        self._active = False

    def create(self, width: int = 1920, height: int = 1080, refresh: float = 60.0,
               position: str = "right") -> tuple[str, int, int]:
        """Create a virtual display and return (output_name, x_offset, y_offset).

        Raises RuntimeError if the display could not be enabled.
        """
        if self._active:
            self.remove()
        if not shutil.which("xrandr"):
            raise RuntimeError("`xrandr` not found (install x11-xserver-utils)")

        self.output = _find_virtual_output()
        if not self.output:
            raise RuntimeError(
                "No disconnected xrandr output available. On Intel/AMD enable "
                "VIRTUAL outputs (see docs/TROUBLESHOOTING.md); otherwise use --region.")

        self.mode_name, params = _modeline(width, height, refresh)

        # Create the mode (an error just means it already exists)
        r = _run(["xrandr", "--newmode", self.mode_name] + params, check=False)
        self._mode_created = r.returncode == 0
        _run(["xrandr", "--addmode", self.output, self.mode_name], check=False)

        pos_flag = {
            "right": "--right-of",
            "left":  "--left-of",
            "above": "--above",
            "below": "--below",
        }.get(position, "--right-of")

        primary = self._primary_output()
        cmd = ["xrandr", "--output", self.output, "--mode", self.mode_name]
        if primary and primary != self.output:
            cmd += [pos_flag, primary]
        r = _run(cmd, check=False)
        self._active = True

        geom = None
        for _ in range(10):  # the X server may take a moment to settle
            geom = self._output_geometry(self.output)
            if geom:
                break
            time.sleep(0.1)
        if not geom or (geom[0], geom[1]) != (width, height):
            err = (r.stderr or "").strip()
            self.remove()
            raise RuntimeError(f"xrandr could not enable {self.output} at "
                               f"{width}x{height}" + (f": {err}" if err else ""))

        _, _, x_off, y_off = geom
        log.info("Virtual display %s created: %dx%d @ +%d+%d",
                 self.output, width, height, x_off, y_off)
        return self.output, x_off, y_off

    def remove(self):
        """Remove the virtual display and clean up."""
        if not self._active:
            return
        self._active = False
        if self.output:
            _run(["xrandr", "--output", self.output, "--off"], check=False)
        if self.output and self.mode_name:
            _run(["xrandr", "--delmode", self.output, self.mode_name], check=False)
        if self.mode_name and self._mode_created:
            _run(["xrandr", "--rmmode", self.mode_name], check=False)
        log.info("Virtual display %s removed", self.output)

    # --- helpers ---

    @staticmethod
    def _primary_output() -> Optional[str]:
        lines = _query().splitlines()
        for line in lines:
            if " connected primary" in line:
                return line.split()[0]
        for line in lines:
            if " connected" in line:
                return line.split()[0]
        return None

    @staticmethod
    def _output_geometry(name: str) -> Optional[tuple[int, int, int, int]]:
        """Return (w, h, x, y) of an active output, or None."""
        for line in _query().splitlines():
            if line.split(" ", 1)[0] == name:
                m = re.search(r"(\d+)x(\d+)\+(\d+)\+(\d+)", line)
                if m:
                    return tuple(int(v) for v in m.groups())  # type: ignore
                return None
        return None

    @staticmethod
    def list_presets() -> dict:
        return dict(PRESETS)

    def __del__(self):
        try:
            self.remove()
        except Exception:
            pass
