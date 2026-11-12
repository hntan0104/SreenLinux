#!/usr/bin/env python3
"""SreenLinux system service — keeps the phone display alive across logins.

Runs as root from systemd at boot. It:

* watches USB and applies ``adb reverse`` whenever a phone is plugged in
  (``adb`` runs as ``--adb-user`` so the phone's existing "Allow USB
  debugging" authorization for that user is reused);
* follows the *active* X11 session on seat0 — the GDM login screen first,
  then your desktop after you log in, back to the login screen after you
  log out — and runs ``screen_server.py`` for it, **as the user that owns
  that display** (``gdm`` for the login screen, you for your desktop).

Wayland sessions cannot be captured; set WaylandEnable=false in
/etc/gdm3/custom.conf (already the default on Jetson).
"""

from __future__ import annotations

import argparse
import logging
import os
import pwd
import re
import signal
import subprocess
import sys
import threading
import time
from typing import NamedTuple, Optional

import adb_watch
from adb_watch import AdbWatcher

log = logging.getLogger("sreenlinux.service")
HERE = os.path.dirname(os.path.abspath(__file__))


class Target(NamedTuple):
    display: str
    xauthority: str
    uid: int
    session: str
    kind: str  # "greeter" / "user"


def _loginctl(*args: str) -> str:
    r = subprocess.run(["loginctl", *args], capture_output=True, text=True, timeout=5)
    return r.stdout


def _active_x11_session() -> Optional[tuple[str, dict]]:
    for line in _loginctl("list-sessions", "--no-legend").splitlines():
        if not line.split():
            continue
        sid = line.split()[0]
        props = {}
        for p in _loginctl("show-session", sid, "-p", "Active", "-p", "Type", "-p", "Seat",
                           "-p", "Remote", "-p", "VTNr", "-p", "Class", "-p", "Name").splitlines():
            if "=" in p:
                k, v = p.split("=", 1)
                props[k] = v
        if (props.get("Active") == "yes" and props.get("Seat") == "seat0"
                and props.get("Remote") != "yes"):
            return sid, props
    return None


def _proc_cmdline(pid: str) -> list[bytes]:
    with open(f"/proc/{pid}/cmdline", "rb") as f:
        return f.read().split(b"\0")


def _proc_environ(pid: str) -> dict[bytes, bytes]:
    with open(f"/proc/{pid}/environ", "rb") as f:
        out = {}
        for item in f.read().split(b"\0"):
            if b"=" in item:
                k, v = item.split(b"=", 1)
                out[k] = v
        return out


def _find_xorg(vt: str) -> Optional[tuple[Optional[str], str]]:
    """Return (display or None, auth file) of the Xorg server on a VT."""
    want = f"vt{vt}".encode()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            args = _proc_cmdline(pid)
        except OSError:
            continue
        if not args or os.path.basename(args[0]) not in (b"Xorg", b"X"):
            continue
        if want not in args or b"-auth" not in args:
            continue
        auth = args[args.index(b"-auth") + 1].decode()
        display = next((a.decode() for a in args if re.fullmatch(rb":\d+", a)), None)
        return display, auth
    return None


def _display_for_auth(auth: str) -> Optional[str]:
    """Find DISPLAY from any process whose XAUTHORITY is this auth file."""
    want = auth.encode()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            env = _proc_environ(pid)
        except OSError:
            continue
        if env.get(b"XAUTHORITY") == want and env.get(b"DISPLAY"):
            return env[b"DISPLAY"].decode()
    return None


def detect_target() -> tuple[Optional[Target], str]:
    """Return (target, reason-if-none)."""
    s = _active_x11_session()
    if not s:
        return None, "no active session on seat0"
    sid, props = s
    if props.get("Type") != "x11":
        return None, f"active session {sid} is {props.get('Type')!r}, not X11"
    xorg = _find_xorg(props.get("VTNr", ""))
    if not xorg:
        return None, f"no Xorg server found for session {sid} (vt{props.get('VTNr')})"
    display, auth = xorg
    display = display or _display_for_auth(auth)
    if not display:
        return None, f"X server for session {sid} is still starting"
    try:
        uid = os.stat(auth).st_uid
    except OSError:
        return None, f"cannot read {auth}"
    kind = "greeter" if props.get("Class") == "greeter" else "user"
    return Target(display, auth, uid, sid, kind), ""


class Supervisor:
    def __init__(self, port: int, server_args: list[str]):
        self.port = port
        self.server_args = server_args
        self.child: Optional[subprocess.Popen] = None
        self.target: Optional[Target] = None
        self._last_reason = ""
        self._stop = threading.Event()
        self._last_start = 0.0

    def _spawn(self, t: Target) -> None:
        pw = pwd.getpwuid(t.uid)
        env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": pw.pw_dir, "USER": pw.pw_name, "LOGNAME": pw.pw_name,
            "DISPLAY": t.display, "XAUTHORITY": t.xauthority,
            "LANG": os.environ.get("LANG", "C.UTF-8"), "PYTHONUNBUFFERED": "1",
        }
        cmd = [sys.executable, os.path.join(HERE, "screen_server.py"),
               "--port", str(self.port), "--no-adb", *self.server_args]
        kwargs = {}
        if os.geteuid() == 0:
            if t.uid != 0:
                kwargs = dict(user=t.uid, group=pw.pw_gid,
                              extra_groups=os.getgrouplist(pw.pw_name, pw.pw_gid))
        elif t.uid != os.getuid():
            log.warning("Not running as root — cannot serve %s's display %s",
                        pw.pw_name, t.display)
            return
        log.info("Serving %s %s (user %s, session %s)",
                 "login screen" if t.kind == "greeter" else "desktop",
                 t.display, pw.pw_name, t.session)
        self.child = subprocess.Popen(cmd, env=env, cwd=HERE, **kwargs)
        self._last_start = time.monotonic()

    def _kill_child(self) -> None:
        c, self.child = self.child, None
        if c and c.poll() is None:
            c.terminate()
            try:
                c.wait(5)
            except subprocess.TimeoutExpired:
                c.kill()
                c.wait()

    def step(self) -> None:
        target, reason = detect_target()
        if target != self.target:
            if self.child:
                log.info("Active display changed — restarting server")
            self._kill_child()
            self.target = target
        if target is None:
            if reason != self._last_reason:
                log.info("Waiting: %s", reason)
                self._last_reason = reason
            return
        self._last_reason = ""
        if self.child and self.child.poll() is not None:
            log.warning("Server exited with code %s", self.child.returncode)
            self.child = None
        if self.child is None:
            # Back off if it keeps crashing (e.g. X not ready yet)
            if time.monotonic() - self._last_start < 5:
                return
            self._spawn(target)

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                log.exception("Supervisor step failed")
            self._stop.wait(2.0)
        self._kill_child()

    def stop(self) -> None:
        self._stop.set()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--port", type=int, default=6100)
    p.add_argument("--adb-user", default=None,
                   help="Run adb as this user (reuses its phone authorization)")
    p.add_argument("--open", action="store_true",
                   help="Open the client on the phone whenever it is plugged in")
    p.add_argument("--no-adb", action="store_true", help="Don't watch USB")
    p.add_argument("server_args", nargs=argparse.REMAINDER,
                   help="Extra options for screen_server.py, after `--`")
    args = p.parse_args(argv)
    extra = [a for a in args.server_args if a != "--"]

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    watcher = None
    if not args.no_adb:
        if args.adb_user:
            try:
                adb_watch.set_run_as(args.adb_user)
            except KeyError:
                log.error("Unknown --adb-user %r", args.adb_user)
                return 2
        if AdbWatcher.available():
            watcher = AdbWatcher(args.port, open_client=args.open)
            watcher.start()
        else:
            log.warning("adb not found — USB auto-connect disabled")

    sup = Supervisor(args.port, extra)

    def _stop(_sig, _frm):
        log.info("Stopping…")
        sup.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    sup.run()
    if watcher:
        watcher.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
