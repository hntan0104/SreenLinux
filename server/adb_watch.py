#!/usr/bin/env python3
"""Watch for Android devices on USB and set up ``adb reverse`` automatically.

``adb reverse`` is per-device and is lost whenever the cable is unplugged
or the phone reboots, so instead of running it once at startup the server
polls ``adb devices`` and (re)applies the forwarding for every device that
appears. Optionally it also opens the client on the phone: the SreenLinux
app if it is installed, otherwise the web client in the default browser.
"""

from __future__ import annotations

import logging
import os
import pwd
import shutil
import socket
import subprocess
import threading
import time
from typing import Optional

log = logging.getLogger("sreen_server.adb")

APP_PACKAGE = "com.sreenlinux.display"
APP_ACTIVITY = f"{APP_PACKAGE}/.MainActivity"


_run_as: dict = {}


def set_run_as(username: str) -> None:
    """Run adb as this user (when we are root), so the adb server uses that
    user's key — the one the phone has already authorized."""
    pw = pwd.getpwnam(username)
    if os.geteuid() != 0 or pw.pw_uid == 0:
        return
    _run_as.clear()
    _run_as.update(
        user=pw.pw_uid, group=pw.pw_gid,
        extra_groups=os.getgrouplist(pw.pw_name, pw.pw_gid),
        env={"HOME": pw.pw_dir, "USER": pw.pw_name, "LOGNAME": pw.pw_name,
             "PATH": "/usr/local/bin:/usr/bin:/bin"},
    )
    log.info("adb runs as user %s", username)


def _adb(*args: str, serial: Optional[str] = None, timeout: float = 10.0) -> subprocess.CompletedProcess:
    cmd = ["adb"] + (["-s", serial] if serial else []) + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **_run_as)


def list_devices() -> dict[str, str]:
    """Return {serial: state} for attached devices (state: device, unauthorized, …)."""
    r = _adb("devices")
    out = {}
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out


class AdbWatcher:
    def __init__(self, port: int, open_client: bool = False, interval: float = 2.0):
        self.port = port
        self.open_client = open_client
        self.interval = interval
        self._known: dict[str, str] = {}
        self._checked: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @staticmethod
    def available() -> bool:
        return shutil.which("adb") is not None

    def start(self) -> None:
        try:
            _adb("start-server", timeout=15)
        except Exception as e:
            log.warning("Could not start adb server: %s", e)
        self._thread = threading.Thread(target=self._run, daemon=True, name="adb-watch")
        self._thread.start()
        log.info("Watching USB for Android devices (adb reverse tcp:%d auto-setup)", self.port)

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll()
            except Exception as e:
                log.debug("adb poll failed: %s", e)
            self._stop.wait(self.interval)

    def _poll(self) -> None:
        devices = list_devices()
        now = time.monotonic()
        for serial, state in devices.items():
            prev = self._known.get(serial)
            if state == "device":
                if prev != "device":
                    # Newly authorized: set up forwarding (retried until it works)
                    if not self._on_attached(serial, first=prev != "retry"):
                        devices[serial] = "retry"
                        continue
                    self._checked[serial] = now
                elif now - self._checked.get(serial, 0) >= 15:
                    # The phone can drop the reverse rule (adbd restart etc.)
                    self._checked[serial] = now
                    self._ensure_reverse(serial)
            elif state != prev:
                if state == "unauthorized":
                    log.warning("Device %s is unauthorized — unlock the phone and accept "
                                "the 'Allow USB debugging?' prompt (tick 'Always allow')",
                                serial)
                else:
                    log.info("Device %s is %s", serial, state)
        for serial in set(self._known) - set(devices):
            log.info("Device %s disconnected", serial)
            self._checked.pop(serial, None)
        self._known = devices

    def _ensure_reverse(self, serial: str) -> None:
        spec = f"tcp:{self.port}"
        r = _adb("reverse", "--list", serial=serial)
        if r.returncode == 0 and f"{spec} {spec}" not in r.stdout:
            log.info("adb reverse missing on %s — re-applying", serial)
            _adb("reverse", spec, spec, serial=serial)

    def _on_attached(self, serial: str, first: bool = True) -> bool:
        spec = f"tcp:{self.port}"
        r = _adb("reverse", spec, spec, serial=serial)
        if r.returncode != 0:
            # Common right after authorization ("error: closed") — retry next poll
            (log.warning if first else log.debug)(
                "adb reverse failed for %s: %s (will retry)", serial,
                (r.stderr or r.stdout).strip())
            return False
        model = _adb("shell", "getprop", "ro.product.model", serial=serial).stdout.strip()
        log.info("Device %s (%s) connected — adb reverse %s active", serial, model or "?", spec)
        if self.open_client:
            self._open_client(serial)
        return True

    def _server_ready(self, timeout: float = 90.0) -> bool:
        """Wait until something listens on the port (at boot the display
        server may start after the phone is detected; opening the page too
        early would leave the browser on an error page)."""
        deadline = time.monotonic() + timeout
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return True
            except OSError:
                self._stop.wait(1.0)
        return False

    def _open_client(self, serial: str) -> None:
        if not self._server_ready():
            log.warning("Server not listening on port %d — not opening the client", self.port)
            return
        has_app =APP_PACKAGE in _adb("shell", "pm", "list", "packages", APP_PACKAGE,
                                      serial=serial).stdout
        if has_app:
            r = _adb("shell", "am", "start", "-n", APP_ACTIVITY, serial=serial)
            what = "SreenLinux app"
        else:
            url = f"http://localhost:{self.port}/"
            r = _adb("shell", "am", "start", "-a", "android.intent.action.VIEW",
                     "-d", url, serial=serial)
            what = url
        if r.returncode == 0 and "Error" not in r.stdout:
            log.info("Opened %s on %s", what, serial)
        else:
            log.warning("Could not open %s on %s: %s", what, serial,
                        (r.stderr or r.stdout).strip())
