#!/usr/bin/env python3
"""Thin ctypes bindings to Xlib / XShm / XTest / XFixes for SreenLinux.

Everything here is optional: each class raises ``X11Unavailable`` from its
constructor when the needed library or extension is missing, so callers can
fall back to slower pure-subprocess implementations.

* ``ShmGrabber``     — fast screen capture through the MIT-SHM extension.
* ``XTestInjector``  — synthetic pointer events without spawning processes.
* ``CursorTracker``  — reads the current cursor image + position (XFixes) so
                       it can be composited into captured frames.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import threading
import time
from ctypes import (POINTER, Structure, byref, c_char_p, c_int,
                    c_short, c_uint, c_ulong, c_ushort, c_void_p)
from typing import Optional

from PIL import Image

log = logging.getLogger(__name__)


class X11Unavailable(RuntimeError):
    """Raised when a native X11 feature cannot be used."""


# ---------------------------------------------------------------------------
# Library loading
# ---------------------------------------------------------------------------

def _load(name: str) -> Optional[ctypes.CDLL]:
    path = ctypes.util.find_library(name)
    candidates = [path] if path else []
    candidates.append(f"lib{name}.so.6")
    candidates.append(f"lib{name}.so")
    for cand in candidates:
        try:
            return ctypes.CDLL(cand)
        except OSError:
            continue
    return None


_xlib = _load("X11")
_xext = _load("Xext")
_xtst = _load("Xtst")
_xfixes = _load("Xfixes")
_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

Display_p = c_void_p
Window = c_ulong

_threads_initialized = False
_init_lock = threading.Lock()

# X error handling: the default Xlib handler calls exit(), which would kill
# the whole server on e.g. a BadMatch caused by a capture region that is
# partially off-screen.  Install a handler that only records the error.
_XErrorHandler = ctypes.CFUNCTYPE(c_int, c_void_p, c_void_p)
_last_x_error = threading.local()


@_XErrorHandler
def _on_x_error(_dpy, event_p):
    # XErrorEvent: int type; Display*; XID resourceid; ulong serial;
    #              uchar error_code; ...  — we only want error_code.
    class _XErrorEvent(Structure):
        _fields_ = [("type", c_int), ("display", c_void_p),
                    ("resourceid", c_ulong), ("serial", c_ulong),
                    ("error_code", ctypes.c_ubyte),
                    ("request_code", ctypes.c_ubyte),
                    ("minor_code", ctypes.c_ubyte)]
    ev = ctypes.cast(event_p, POINTER(_XErrorEvent)).contents
    _last_x_error.code = ev.error_code
    log.debug("X error code=%d request=%d", ev.error_code, ev.request_code)
    return 0


def _init_xlib() -> None:
    global _threads_initialized
    if _xlib is None:
        raise X11Unavailable("libX11 not found")
    with _init_lock:
        if _threads_initialized:
            return
        _xlib.XInitThreads.restype = c_int
        _xlib.XInitThreads()
        _xlib.XOpenDisplay.argtypes = [c_char_p]
        _xlib.XOpenDisplay.restype = Display_p
        _xlib.XCloseDisplay.argtypes = [Display_p]
        _xlib.XDefaultScreen.argtypes = [Display_p]
        _xlib.XDefaultScreen.restype = c_int
        _xlib.XRootWindow.argtypes = [Display_p, c_int]
        _xlib.XRootWindow.restype = Window
        _xlib.XDefaultVisual.argtypes = [Display_p, c_int]
        _xlib.XDefaultVisual.restype = c_void_p
        _xlib.XDefaultDepth.argtypes = [Display_p, c_int]
        _xlib.XDefaultDepth.restype = c_int
        _xlib.XDisplayWidth.argtypes = [Display_p, c_int]
        _xlib.XDisplayWidth.restype = c_int
        _xlib.XDisplayHeight.argtypes = [Display_p, c_int]
        _xlib.XDisplayHeight.restype = c_int
        _xlib.XFlush.argtypes = [Display_p]
        _xlib.XSync.argtypes = [Display_p, c_int]
        _xlib.XFree.argtypes = [c_void_p]
        _xlib.XSetErrorHandler.argtypes = [_XErrorHandler]
        _xlib.XSetErrorHandler.restype = c_void_p
        _xlib.XSetErrorHandler(_on_x_error)
        _threads_initialized = True


def _open_display() -> int:
    _init_xlib()
    dpy = _xlib.XOpenDisplay(None)
    if not dpy:
        raise X11Unavailable("cannot open X display (is $DISPLAY set?)")
    return dpy


def screen_size() -> tuple[int, int]:
    """Return the current root-window size in pixels."""
    dpy = _open_display()
    try:
        scr = _xlib.XDefaultScreen(dpy)
        return _xlib.XDisplayWidth(dpy, scr), _xlib.XDisplayHeight(dpy, scr)
    finally:
        _xlib.XCloseDisplay(dpy)


# ---------------------------------------------------------------------------
# MIT-SHM screen capture
# ---------------------------------------------------------------------------

class _XImage(Structure):
    # Only the leading fields we read; we never allocate this ourselves.
    _fields_ = [
        ("width", c_int), ("height", c_int), ("xoffset", c_int),
        ("format", c_int), ("data", c_void_p), ("byte_order", c_int),
        ("bitmap_unit", c_int), ("bitmap_bit_order", c_int),
        ("bitmap_pad", c_int), ("depth", c_int), ("bytes_per_line", c_int),
        ("bits_per_pixel", c_int), ("red_mask", c_ulong),
        ("green_mask", c_ulong), ("blue_mask", c_ulong),
    ]


class _XShmSegmentInfo(Structure):
    _fields_ = [("shmseg", c_ulong), ("shmid", c_int),
                ("shmaddr", c_void_p), ("readOnly", c_int)]


_ZPixmap = 2
_IPC_PRIVATE = 0
_IPC_CREAT = 0o1000
_IPC_RMID = 0
_ALL_PLANES = c_ulong(-1).value


class ShmGrabber:
    """Captures a fixed rectangle of the root window via XShmGetImage."""

    def __init__(self, x: int, y: int, width: int, height: int):
        if _xext is None:
            raise X11Unavailable("libXext not found")
        self.dpy = _open_display()
        self.x, self.y, self.width, self.height = x, y, width, height
        self._img_p = None
        self._shminfo = _XShmSegmentInfo()
        try:
            self._setup()
        except Exception:
            self.close()
            raise

    def _setup(self) -> None:
        x = _xext
        x.XShmQueryExtension.argtypes = [Display_p]
        x.XShmQueryExtension.restype = c_int
        if not x.XShmQueryExtension(self.dpy):
            raise X11Unavailable("MIT-SHM extension not available")

        x.XShmCreateImage.argtypes = [Display_p, c_void_p, c_uint, c_int,
                                      c_char_p, POINTER(_XShmSegmentInfo),
                                      c_uint, c_uint]
        x.XShmCreateImage.restype = POINTER(_XImage)
        x.XShmAttach.argtypes = [Display_p, POINTER(_XShmSegmentInfo)]
        x.XShmDetach.argtypes = [Display_p, POINTER(_XShmSegmentInfo)]
        x.XShmGetImage.argtypes = [Display_p, Window, POINTER(_XImage),
                                   c_int, c_int, c_ulong]
        x.XShmGetImage.restype = c_int

        _libc.shmget.argtypes = [c_int, ctypes.c_size_t, c_int]
        _libc.shmget.restype = c_int
        _libc.shmat.argtypes = [c_int, c_void_p, c_int]
        _libc.shmat.restype = c_void_p
        _libc.shmdt.argtypes = [c_void_p]
        _libc.shmctl.argtypes = [c_int, c_int, c_void_p]

        scr = _xlib.XDefaultScreen(self.dpy)
        self.root = _xlib.XRootWindow(self.dpy, scr)
        visual = _xlib.XDefaultVisual(self.dpy, scr)
        depth = _xlib.XDefaultDepth(self.dpy, scr)

        img_p = x.XShmCreateImage(self.dpy, visual, depth, _ZPixmap, None,
                                  byref(self._shminfo),
                                  self.width, self.height)
        if not img_p:
            raise X11Unavailable("XShmCreateImage failed")
        self._img_p = img_p
        img = img_p.contents
        if img.bits_per_pixel != 32:
            raise X11Unavailable(f"unsupported pixel depth {img.bits_per_pixel} bpp")
        # LSBFirst (0) → bytes in memory are B,G,R,X
        self._rawmode = "BGRX" if img.byte_order == 0 else "XRGB"
        self._stride = img.bytes_per_line
        self._size = img.bytes_per_line * img.height

        shmid = _libc.shmget(_IPC_PRIVATE, self._size, _IPC_CREAT | 0o600)
        if shmid < 0:
            raise X11Unavailable("shmget failed")
        addr = _libc.shmat(shmid, None, 0)
        if addr in (None, c_void_p(-1).value):
            _libc.shmctl(shmid, _IPC_RMID, None)
            raise X11Unavailable("shmat failed")
        self._shminfo.shmid = shmid
        self._shminfo.shmaddr = addr
        self._shminfo.readOnly = 0
        img.data = addr

        _last_x_error.code = 0
        x.XShmAttach(self.dpy, byref(self._shminfo))
        _xlib.XSync(self.dpy, 0)
        # Mark for deletion now; it's freed once both sides detach.
        _libc.shmctl(shmid, _IPC_RMID, None)
        if getattr(_last_x_error, "code", 0):
            raise X11Unavailable("XShmAttach failed (remote X display?)")
        self._buf = (ctypes.c_char * self._size).from_address(addr)
        # Probe once so a bad region fails here rather than mid-stream.
        if self.grab_raw() is None:
            raise X11Unavailable("XShmGetImage failed — region outside screen?")

    def grab_raw(self) -> Optional[bytes]:
        """Capture and return a copy of the raw pixel buffer (or None)."""
        _last_x_error.code = 0
        ok = _xext.XShmGetImage(self.dpy, self.root, self._img_p,
                                self.x, self.y, _ALL_PLANES)
        if not ok or getattr(_last_x_error, "code", 0):
            return None
        return bytes(self._buf)

    def to_image(self, raw: bytes) -> Image.Image:
        return Image.frombuffer("RGB", (self.width, self.height), raw,
                                "raw", self._rawmode, self._stride, 1)

    def close(self) -> None:
        if getattr(self, "dpy", None):
            if self._shminfo.shmaddr:
                try:
                    _xext.XShmDetach(self.dpy, byref(self._shminfo))
                    _xlib.XSync(self.dpy, 0)
                except Exception:
                    pass
                _libc.shmdt(self._shminfo.shmaddr)
                self._shminfo.shmaddr = None
            if self._img_p:
                self._img_p.contents.data = None
                _xlib.XFree(self._img_p)
                self._img_p = None
            _xlib.XCloseDisplay(self.dpy)
            self.dpy = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# XTest input injection
# ---------------------------------------------------------------------------

class XTestInjector:
    """Moves the pointer and presses buttons using the XTEST extension."""

    def __init__(self):
        if _xtst is None:
            raise X11Unavailable("libXtst not found")
        self.dpy = _open_display()
        t = _xtst
        t.XTestQueryExtension.argtypes = [Display_p, POINTER(c_int),
                                          POINTER(c_int), POINTER(c_int),
                                          POINTER(c_int)]
        t.XTestQueryExtension.restype = c_int
        a, b, c, d = c_int(), c_int(), c_int(), c_int()
        if not t.XTestQueryExtension(self.dpy, byref(a), byref(b),
                                     byref(c), byref(d)):
            _xlib.XCloseDisplay(self.dpy)
            raise X11Unavailable("XTEST extension not available")
        t.XTestFakeMotionEvent.argtypes = [Display_p, c_int, c_int, c_int, c_ulong]
        t.XTestFakeButtonEvent.argtypes = [Display_p, c_uint, c_int, c_ulong]
        t.XTestFakeKeyEvent.argtypes = [Display_p, c_uint, c_int, c_ulong]
        x = _xlib
        x.XDisplayKeycodes.argtypes = [Display_p, POINTER(c_int), POINTER(c_int)]
        x.XGetKeyboardMapping.argtypes = [Display_p, ctypes.c_ubyte, c_int, POINTER(c_int)]
        x.XGetKeyboardMapping.restype = POINTER(c_ulong)
        x.XChangeKeyboardMapping.argtypes = [Display_p, c_int, c_int, POINTER(c_ulong), c_int]
        self._lock = threading.Lock()
        self._keymap: Optional[list[list[int]]] = None
        self._spare: list[int] = []
        self._spare_next = 0
        self._shift_down_for: dict[int, bool] = {}  # keysym → we pressed Shift for it

    def move(self, x: int, y: int) -> None:
        with self._lock:
            _xtst.XTestFakeMotionEvent(self.dpy, -1, x, y, 0)
            _xlib.XFlush(self.dpy)

    def button(self, button: int, pressed: bool) -> None:
        with self._lock:
            _xtst.XTestFakeButtonEvent(self.dpy, button, 1 if pressed else 0, 0)
            _xlib.XFlush(self.dpy)

    # -- keyboard --

    def _load_keymap(self) -> None:
        lo, hi = c_int(), c_int()
        _xlib.XDisplayKeycodes(self.dpy, byref(lo), byref(hi))
        per = c_int()
        count = hi.value - lo.value + 1
        p = _xlib.XGetKeyboardMapping(self.dpy, lo.value, count, byref(per))
        if not p:
            raise X11Unavailable("XGetKeyboardMapping failed")
        n = per.value
        flat = p[:count * n]
        _xlib.XFree(p)
        self._min_kc, self._per = lo.value, n
        self._keymap = [flat[i * n:(i + 1) * n] for i in range(count)]
        self._keymap_time = time.monotonic()
        if not self._spare:
            # Keycodes with no symbols at all can be borrowed for unmapped
            # keysyms. Computed once, so our own bindings don't shrink it.
            self._spare = [lo.value + i for i, syms in enumerate(self._keymap)
                           if not any(syms)][-10:]

    def _lookup(self, keysym: int) -> tuple[Optional[int], bool]:
        """Return (keycode, needs_shift) for a keysym, remapping if needed."""
        if self._keymap is None or time.monotonic() - self._keymap_time > 5.0:
            self._load_keymap()  # picks up keyboard-layout changes
        for i, syms in enumerate(self._keymap):
            if syms and syms[0] == keysym:
                return self._min_kc + i, False
        for i, syms in enumerate(self._keymap):
            if len(syms) > 1 and syms[1] == keysym:
                return self._min_kc + i, True
        if not self._spare:
            return None, False
        # Bind the keysym to a spare keycode (both levels → no Shift needed)
        kc = self._spare[self._spare_next % len(self._spare)]
        self._spare_next += 1
        syms = (c_ulong * self._per)(*([keysym, keysym] + [0] * (self._per - 2)))
        _xlib.XChangeKeyboardMapping(self.dpy, kc, self._per, syms, 1)
        _xlib.XSync(self.dpy, 0)
        self._keymap[kc - self._min_kc] = [keysym, keysym] + [0] * (self._per - 2)
        return kc, False

    def key(self, keysym: int, pressed: bool) -> bool:
        """Press or release the key producing ``keysym``. Returns False if impossible."""
        with self._lock:
            kc, shift = self._lookup(keysym)
            if kc is None:
                return False
            shift_kc = self._lookup(0xFFE1)[0] if shift else None  # Shift_L
            if pressed:
                if shift_kc:
                    _xtst.XTestFakeKeyEvent(self.dpy, shift_kc, 1, 0)
                    self._shift_down_for[keysym] = True
                _xtst.XTestFakeKeyEvent(self.dpy, kc, 1, 0)
            else:
                _xtst.XTestFakeKeyEvent(self.dpy, kc, 0, 0)
                if self._shift_down_for.pop(keysym, False) and shift_kc:
                    _xtst.XTestFakeKeyEvent(self.dpy, shift_kc, 0, 0)
            _xlib.XFlush(self.dpy)
            return True

    def refresh_keymap(self) -> None:
        with self._lock:
            self._keymap = None

    def close(self) -> None:
        if self.dpy:
            _xlib.XCloseDisplay(self.dpy)
            self.dpy = None


# ---------------------------------------------------------------------------
# XFixes cursor image
# ---------------------------------------------------------------------------

class _XFixesCursorImage(Structure):
    _fields_ = [
        ("x", c_short), ("y", c_short),
        ("width", c_ushort), ("height", c_ushort),
        ("xhot", c_ushort), ("yhot", c_ushort),
        ("cursor_serial", c_ulong),
        ("pixels", POINTER(c_ulong)),
    ]


class CursorTracker:
    """Returns the current cursor sprite and its position on the root window."""

    def __init__(self):
        if _xfixes is None:
            raise X11Unavailable("libXfixes not found")
        self.dpy = _open_display()
        f = _xfixes
        f.XFixesQueryExtension.argtypes = [Display_p, POINTER(c_int), POINTER(c_int)]
        f.XFixesQueryExtension.restype = c_int
        f.XFixesQueryVersion.argtypes = [Display_p, POINTER(c_int), POINTER(c_int)]
        f.XFixesQueryVersion.restype = c_int
        ev, err = c_int(), c_int()
        if not f.XFixesQueryExtension(self.dpy, byref(ev), byref(err)):
            _xlib.XCloseDisplay(self.dpy)
            raise X11Unavailable("XFIXES extension not available")
        major, minor = c_int(4), c_int(0)
        f.XFixesQueryVersion(self.dpy, byref(major), byref(minor))
        f.XFixesGetCursorImage.argtypes = [Display_p]
        f.XFixesGetCursorImage.restype = POINTER(_XFixesCursorImage)
        self._serial = None
        self._sprite: Optional[Image.Image] = None
        self._hot = (0, 0)

    def get(self) -> Optional[tuple[Image.Image, int, int, int]]:
        """Return (sprite_rgba, top_left_x, top_left_y, serial) or None."""
        p = _xfixes.XFixesGetCursorImage(self.dpy)
        if not p:
            return None
        try:
            ci = p.contents
            if ci.width == 0 or ci.height == 0:
                return None
            if ci.cursor_serial != self._serial:
                n = ci.width * ci.height
                # Each pixel is a 32-bit premultiplied ARGB value stored in a
                # C `unsigned long` (64-bit on LP64 platforms).
                px = ci.pixels[:n]
                data = bytearray(n * 4)
                for i, v in enumerate(px):
                    o = i * 4
                    data[o] = v & 0xFF
                    data[o + 1] = (v >> 8) & 0xFF
                    data[o + 2] = (v >> 16) & 0xFF
                    data[o + 3] = (v >> 24) & 0xFF
                size = (ci.width, ci.height)
                try:
                    sprite = Image.frombytes("RGBA", size, bytes(data), "raw", "BGRa")
                except ValueError:
                    sprite = Image.frombytes("RGBA", size, bytes(data), "raw", "BGRA")
                self._sprite = sprite
                self._hot = (ci.xhot, ci.yhot)
                self._serial = ci.cursor_serial
            return (self._sprite, ci.x - self._hot[0], ci.y - self._hot[1],
                    self._serial)
        finally:
            _xlib.XFree(p)

    def close(self) -> None:
        if self.dpy:
            _xlib.XCloseDisplay(self.dpy)
            self.dpy = None
