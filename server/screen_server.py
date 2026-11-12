#!/usr/bin/env python3
"""SreenLinux — screen streaming server.

Captures a region (or full screen / virtual display) from X11, compresses
frames as JPEG, and streams them to the Android client over TCP.  Receives
input events from the client and replays them as X11 pointer events.

The same port also serves a browser client (GET / and a WebSocket at /ws),
so a phone can use Chrome instead of the app — see transport.py.

Protocol (all integers big-endian):
  Server → Client : [4 B frame_size][JPEG bytes]                (repeated)
                    (WebSocket: one binary message per JPEG frame)
  Client → Server : first, hello b"SRLN" + [2 B version=1]  (app only)
                    then fixed 6-byte messages [1 B type][1 B arg][2 B x][2 B y]
      type 1 — touch  : arg = 0 down / 1 move / 2 up   (left-button drag)
      type 2 — click  : arg = button (1 left, 2 middle, 3 right)
      type 3 — scroll : arg = 0 up / 1 down / 2 left / 3 right   (one step)
      type 4 — key    : arg = 0 down / 1 up / 2 tap; the 4 bytes x,y hold an
                        X11 keysym (Unicode char c → 0x01000000 + c)
  x / y are in frame-pixel coordinates.  Unknown types are ignored.
"""

from __future__ import annotations

import argparse
import io
import logging
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Optional

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    sys.stderr.write("Pillow is required:  pip3 install -r requirements.txt\n")
    sys.exit(1)

from adb_watch import AdbWatcher
from transport import Transport, classify
from virtual_display import VirtualDisplay, parse_resolution

try:
    import x11_native
except Exception as e:  # pragma: no cover — library missing entirely
    x11_native = None
    _NATIVE_ERR = e

log = logging.getLogger("sreen_server")

# Send a frame at least this often even if nothing changed, so the client
# can tell the connection is alive (its socket read timeout is 10 s).
KEEPALIVE_INTERVAL = 1.0

MSG_TOUCH, MSG_CLICK, MSG_SCROLL, MSG_KEY = 1, 2, 3, 4
KEY_DOWN, KEY_UP, KEY_TAP = 0, 1, 2


# ---------------------------------------------------------------------------
# Screen capture
# ---------------------------------------------------------------------------

def _parse_xwd(data: bytes) -> Image.Image:
    """Decode a 24/32-bit ZPixmap XWD dump (Pillow has no XWD reader)."""
    hdr = struct.unpack(">25I", data[:100])
    header_size, pix_fmt = hdr[0], hdr[2]
    width, height = hdr[4], hdr[5]
    byte_order, bpp, bpl = hdr[7], hdr[11], hdr[12]
    ncolors = hdr[19]
    if pix_fmt != 2 or bpp not in (24, 32):
        raise ValueError(f"unsupported XWD format (format={pix_fmt}, bpp={bpp})")
    offset = header_size + ncolors * 12
    raw = data[offset:offset + bpl * height]
    if bpp == 32:
        rawmode = "BGRX" if byte_order == 0 else "XRGB"
    else:
        rawmode = "BGR" if byte_order == 0 else "RGB"
    return Image.frombuffer("RGB", (width, height), raw, "raw", rawmode, bpl, 1)


class ScreenCapture:
    """Captures frames of a fixed region of the X11 root window.

    Backends, fastest first: MIT-SHM (ctypes) → PIL.ImageGrab → ``xwd``.
    """

    def __init__(self, bbox: tuple[int, int, int, int], draw_cursor: bool = True,
                 backend: str = "auto"):
        """bbox: (left, top, right, bottom) in root-window coordinates."""
        self.bbox = bbox
        self.width = bbox[2] - bbox[0]
        self.height = bbox[3] - bbox[1]
        self._lock = threading.Lock()
        self._shm = None
        self._cursor = None
        self.backend = None

        if backend in ("auto", "shm") and x11_native is not None:
            try:
                self._shm = x11_native.ShmGrabber(bbox[0], bbox[1],
                                                  self.width, self.height)
                self.backend = "shm"
            except Exception as e:
                log.info("MIT-SHM capture unavailable: %s", e)
        if self.backend is None and backend in ("auto", "pil"):
            try:
                from PIL import ImageGrab
                ImageGrab.grab(bbox=bbox)
                self.backend = "pil"
            except Exception as e:
                log.info("PIL.ImageGrab unavailable: %s", e)
        if self.backend is None and backend in ("auto", "xwd"):
            try:
                self._grab_xwd()
                self.backend = "xwd"
            except Exception as e:
                log.info("xwd capture unavailable: %s", e)
        if self.backend is None:
            raise RuntimeError("No screen capture backend works. Is this an X11 "
                               "session with $DISPLAY set? (Wayland is unsupported)")
        log.info("Capture backend: %s  region %dx%d+%d+%d", self.backend,
                 self.width, self.height, bbox[0], bbox[1])

        if draw_cursor and x11_native is not None:
            try:
                self._cursor = x11_native.CursorTracker()
            except Exception as e:
                log.info("Cursor overlay disabled: %s", e)

    def grab(self) -> tuple[Optional[Image.Image], object]:
        """Return (image, change_key).

        ``change_key`` compares equal between two calls iff the frame did not
        change, letting the caller skip encoding identical frames.
        """
        with self._lock:
            cursor = self._cursor.get() if self._cursor else None
            if self.backend == "shm":
                raw = self._shm.grab_raw()
                if raw is None:
                    return None, None
                img = self._shm.to_image(raw)
                key_base = raw
            elif self.backend == "pil":
                from PIL import ImageGrab
                img = ImageGrab.grab(bbox=self.bbox)
                key_base = None  # too expensive to compare, always "changed"
            else:
                img = self._grab_xwd()
                key_base = None

            cursor_key = None
            if cursor is not None:
                sprite, cx, cy, serial = cursor
                lx, ly = cx - self.bbox[0], cy - self.bbox[1]
                if (-sprite.width < lx < self.width and
                        -sprite.height < ly < self.height):
                    if key_base is not None:
                        img = img.copy()  # frombuffer images are read-only
                    img.paste(sprite, (lx, ly), sprite)
                    cursor_key = (lx, ly, serial)
            key = (key_base, cursor_key) if key_base is not None else object()
            return img, key

    def _grab_xwd(self) -> Image.Image:
        proc = subprocess.run(["xwd", "-root", "-silent"],
                              capture_output=True, timeout=5, check=True)
        img = _parse_xwd(proc.stdout)
        if (img.width, img.height) != (self.width, self.height) or self.bbox[:2] != (0, 0):
            img = img.crop(self.bbox)
        return img

    def close(self):
        with self._lock:
            if self._shm:
                self._shm.close()
                self._shm = None
            if self._cursor:
                self._cursor.close()
                self._cursor = None


# ---------------------------------------------------------------------------
# Frame encoder
# ---------------------------------------------------------------------------

class FrameEncoder:
    def __init__(self, quality: int = 50,
                 resize: Optional[tuple[int, int]] = None):
        self.quality = max(1, min(100, quality))
        self.resize = resize  # (w, h) or None

    def encode(self, img: Image.Image) -> bytes:
        if self.resize and img.size != self.resize:
            img = img.resize(self.resize, Image.BILINEAR)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self.quality)
        return buf.getvalue()


# ---------------------------------------------------------------------------
# Input injection
# ---------------------------------------------------------------------------

class InputInjector:
    """Replays pointer events on the X server (XTest, or xdotool fallback)."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._xtest = None
        self._xdotool = False
        if not enabled:
            log.info("Input injection disabled (view-only mode)")
            return
        if x11_native is not None:
            try:
                self._xtest = x11_native.XTestInjector()
                log.info("Input backend: XTest")
                return
            except Exception as e:
                log.info("XTest unavailable: %s", e)
        try:
            subprocess.run(["xdotool", "--version"], capture_output=True,
                           timeout=2, check=True)
            self._xdotool = True
            log.info("Input backend: xdotool")
        except Exception:
            log.warning("Neither XTest nor xdotool available — input ignored. "
                        "Install with: sudo apt install xdotool")
            self.enabled = False

    def _xdo(self, *args) -> None:
        # One process per event, with all steps chained so they run in order.
        subprocess.Popen(["xdotool", *map(str, args)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def move(self, x: int, y: int) -> None:
        if self._xtest:
            self._xtest.move(x, y)
        elif self._xdotool:
            self._xdo("mousemove", x, y)

    def press(self, x: int, y: int, button: int = 1) -> None:
        if self._xtest:
            self._xtest.move(x, y)
            self._xtest.button(button, True)
        elif self._xdotool:
            self._xdo("mousemove", x, y, "mousedown", button)

    def release(self, x: int, y: int, button: int = 1) -> None:
        if self._xtest:
            self._xtest.move(x, y)
            self._xtest.button(button, False)
        elif self._xdotool:
            self._xdo("mousemove", x, y, "mouseup", button)

    def click(self, x: int, y: int, button: int = 1) -> None:
        if self._xtest:
            self._xtest.move(x, y)
            self._xtest.button(button, True)
            self._xtest.button(button, False)
        elif self._xdotool:
            self._xdo("mousemove", x, y, "click", button)

    def key(self, action: int, keysym: int) -> None:
        if not self.enabled or not (0 < keysym <= 0x0110FFFF):
            return
        try:
            if self._xtest:
                if action in (KEY_DOWN, KEY_TAP):
                    if not self._xtest.key(keysym, True):
                        log.debug("No keycode available for keysym 0x%x", keysym)
                        return
                if action in (KEY_UP, KEY_TAP):
                    self._xtest.key(keysym, False)
            elif self._xdotool:
                cmd = {KEY_DOWN: "keydown", KEY_UP: "keyup", KEY_TAP: "key"}.get(action)
                if cmd:
                    self._xdo(cmd, f"0x{keysym:x}")
        except Exception as e:
            log.debug("Key injection error: %s", e)

    def handle(self, msg_type: int, arg: int, x: int, y: int) -> None:
        if not self.enabled:
            return
        try:
            if msg_type == MSG_TOUCH:
                if arg == 0:
                    self.press(x, y)
                elif arg == 1:
                    self.move(x, y)
                elif arg == 2:
                    self.release(x, y)
            elif msg_type == MSG_CLICK and arg in (1, 2, 3):
                self.click(x, y, arg)
            elif msg_type == MSG_SCROLL and arg in (0, 1, 2, 3):
                self.click(x, y, 4 + arg)  # X buttons 4/5/6/7
            else:
                log.debug("Ignoring message type=%d arg=%d", msg_type, arg)
        except Exception as e:
            log.debug("Input injection error: %s", e)


# ---------------------------------------------------------------------------
# Client handler
# ---------------------------------------------------------------------------

class ClientHandler:
    """Manages one streaming client: sends frames, receives input events."""

    def __init__(self, transport: Transport, addr,
                 capture: ScreenCapture, encoder: FrameEncoder,
                 injector: InputInjector, target_fps: float,
                 x_offset: int, y_offset: int,
                 scale_x: float, scale_y: float):
        self.transport = transport
        self.addr = addr
        self.capture = capture
        self.encoder = encoder
        self.injector = injector
        self.frame_interval = 1.0 / max(1.0, target_fps)
        self.x_offset, self.y_offset = x_offset, y_offset
        self.scale_x, self.scale_y = scale_x, scale_y
        self._stop = threading.Event()
        self.frames_sent = 0
        self.bytes_sent = 0

    @property
    def label(self) -> str:
        return f"{self.transport.kind} {self.addr[0]}:{self.addr[1]}"

    def start(self) -> None:
        for target, name in ((self._send_loop, "send"), (self._recv_loop, "recv")):
            threading.Thread(target=target, daemon=True,
                             name=f"{name}-{self.addr[1]}").start()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until the session ends. Returns True if it ended."""
        return self._stop.wait(timeout)

    @property
    def alive(self) -> bool:
        return not self._stop.is_set()

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self.transport.close()

    # -- loops --

    def _send_loop(self) -> None:
        last_key = None
        last_send = 0.0
        stats_t0 = time.monotonic()
        stats_frames = 0
        last_size = 0
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                img, key = self.capture.grab()
                if img is not None and (key != last_key or
                                        t0 - last_send >= KEEPALIVE_INTERVAL):
                    jpeg = self.encoder.encode(img)
                    self.bytes_sent += self.transport.send_frame(jpeg)
                    last_key, last_send, last_size = key, t0, len(jpeg)
                    self.frames_sent += 1
                    stats_frames += 1

                now = time.monotonic()
                if now - stats_t0 >= 5.0:
                    log.debug("%s: %.1f fps sent, last frame %d KB", self.label,
                              stats_frames / (now - stats_t0), last_size // 1024)
                    stats_t0, stats_frames = now, 0

                sleep = self.frame_interval - (now - t0)
                if sleep > 0:
                    self._stop.wait(sleep)
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            if not self._stop.is_set():
                log.info("Client %s disconnected (send: %s)", self.label, e)
        except Exception:
            log.exception("Send loop crashed")
        finally:
            self.stop()

    def _recv_loop(self) -> None:
        try:
            while not self._stop.is_set():
                data = self.transport.recv_message()
                if data is None:
                    break
                msg_type, arg, x, y = struct.unpack("!BBHH", data)
                if msg_type == MSG_KEY:
                    self.injector.key(arg, (x << 16) | y)
                    continue
                sx = self.x_offset + int(x * self.scale_x)
                sy = self.y_offset + int(y * self.scale_y)
                self.injector.handle(msg_type, arg, sx, sy)
        except (ConnectionResetError, OSError) as e:
            if not self._stop.is_set():
                log.info("Client %s disconnected (recv: %s)", self.label, e)
        except Exception:
            log.exception("Receive loop crashed")
        finally:
            self.stop()


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class ScreenServer:
    """Accepts clients; a new streaming client replaces the current one."""

    def __init__(self, host: str, port: int, capture: ScreenCapture,
                 encoder: FrameEncoder, injector: InputInjector,
                 target_fps: float, x_offset: int, y_offset: int,
                 scale_x: float, scale_y: float):
        self.host, self.port = host, port
        self.capture, self.encoder, self.injector = capture, encoder, injector
        self.target_fps = target_fps
        self.x_offset, self.y_offset = x_offset, y_offset
        self.scale_x, self.scale_y = scale_x, scale_y
        self._sock: Optional[socket.socket] = None
        self._handler: Optional[ClientHandler] = None
        self._handler_lock = threading.Lock()
        self._stop = threading.Event()
        self.on_listening = None  # optional callback once the socket is bound

    def serve_forever(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        log.info("Listening on %s:%d  (fps=%d, quality=%d)", self.host,
                 self.port, self.target_fps, self.encoder.quality)
        shown = "localhost" if self.host in ("127.0.0.1", "0.0.0.0") else self.host
        log.info("Open http://%s:%d in Chrome on the phone, or start the SreenLinux app",
                 shown, self.port)
        if self.on_listening:
            self.on_listening()

        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                self._reap()
                continue
            except OSError:
                break
            threading.Thread(target=self._on_connection, args=(conn, addr),
                             daemon=True, name=f"conn-{addr[1]}").start()

        with self._handler_lock:
            if self._handler:
                self._handler.stop()
        if self._sock:
            self._sock.close()
        log.info("Server stopped")

    def _reap(self) -> None:
        with self._handler_lock:
            h = self._handler
            if h and not h.alive:
                log.info("Session %s ended (%d frames, %.1f MB)", h.label,
                         h.frames_sent, h.bytes_sent / 1e6)
                self._handler = None

    def _on_connection(self, conn: socket.socket, addr) -> None:
        transport = classify(conn, self.host)
        if transport is None or self._stop.is_set():
            if transport:
                transport.close()
            return
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        handler = ClientHandler(
            transport, addr, self.capture, self.encoder, self.injector,
            self.target_fps, self.x_offset, self.y_offset,
            self.scale_x, self.scale_y)
        with self._handler_lock:
            old = self._handler
            if old and old.alive:
                log.info("New client %s replaces %s", handler.label, old.label)
                old.stop()
                old.wait(2.0)
            self._handler = handler
        log.info("Client connected: %s", handler.label)
        handler.start()

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# Screen geometry helpers
# ---------------------------------------------------------------------------

def get_screen_size() -> tuple[int, int]:
    """Return (width, height) of the default X screen."""
    if x11_native is not None:
        try:
            return x11_native.screen_size()
        except Exception:
            pass
    try:
        r = subprocess.run(["xdpyinfo"], capture_output=True, text=True, timeout=3)
        for line in r.stdout.splitlines():
            if "dimensions:" in line:
                w, h = line.split()[1].split("x")
                return int(w), int(h)
    except Exception:
        pass
    log.warning("Could not determine screen size, assuming 1920x1080")
    return 1920, 1080


def compute_output_size(cw: int, ch: int, width: int, height: int) -> Optional[tuple[int, int]]:
    """Return the resized frame size for --width/--height, or None."""
    if width and height:
        return width, height
    if width:
        return width, max(1, round(ch * width / cw))
    if height:
        return max(1, round(cw * height / ch)), height
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="SreenLinux — stream your Linux screen to an Android device over USB (ADB).",
    )
    p.add_argument("--host", default="127.0.0.1",
                   help="Bind address (default: 127.0.0.1 — enough for adb reverse; "
                        "use 0.0.0.0 for Wi-Fi)")
    p.add_argument("--port", type=int, default=6100, help="TCP port (default: 6100)")
    p.add_argument("--fps", type=int, default=30, help="Target FPS (default: 30)")
    p.add_argument("--quality", type=int, default=50,
                   help="JPEG quality 1-100 (default: 50)")
    p.add_argument("--width", type=int, default=0,
                   help="Resize output width (0 = no resize)")
    p.add_argument("--height", type=int, default=0,
                   help="Resize output height (0 = no resize)")
    p.add_argument("--region", type=str, default=None,
                   help="Capture region as X,Y,W,H (e.g. 0,0,1920,1080)")
    p.add_argument("--virtual", action="store_true",
                   help="Create a virtual (extended) display for the Android device")
    p.add_argument("--virtual-res", type=str, default="1080p",
                   help="Virtual display preset (720p, 800p, 1080p, 1440p, 2k) or WxH")
    p.add_argument("--position", choices=["right", "left", "above", "below"],
                   default="right", help="Virtual display position (default: right)")
    p.add_argument("--capture", choices=["auto", "shm", "pil", "xwd"], default="auto",
                   help="Force a capture backend (default: auto)")
    p.add_argument("--no-cursor", action="store_true",
                   help="Don't draw the mouse cursor into frames")
    p.add_argument("--no-input", action="store_true",
                   help="View-only: ignore touch input from the device")
    p.add_argument("--no-adb", action="store_true",
                   help="Don't watch USB for devices / don't run adb reverse automatically")
    p.add_argument("--open", action="store_true",
                   help="When a phone is plugged in, open the SreenLinux app "
                        "(or the web client in its browser)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if x11_native is None:
        log.warning("Native X11 helpers unavailable (%s) — using slow fallbacks", _NATIVE_ERR)

    vdisplay: Optional[VirtualDisplay] = None
    capture: Optional[ScreenCapture] = None
    try:
        if args.virtual:
            try:
                vw, vh, vr = parse_resolution(args.virtual_res)
            except ValueError as e:
                log.error("%s", e)
                return 2
            vdisplay = VirtualDisplay()
            try:
                _, x_off, y_off = vdisplay.create(vw, vh, vr, args.position)
            except RuntimeError as e:
                log.error("Could not create virtual display: %s", e)
                log.error("Tip: use --region X,Y,W,H, or see docs/TROUBLESHOOTING.md §3")
                return 1
            bbox = (x_off, y_off, x_off + vw, y_off + vh)
        elif args.region:
            try:
                rx, ry, rw, rh = (int(v) for v in args.region.split(","))
                if rw <= 0 or rh <= 0:
                    raise ValueError
            except ValueError:
                log.error("Invalid --region format. Use X,Y,W,H (e.g. 0,0,1280,720)")
                return 2
            bbox = (rx, ry, rx + rw, ry + rh)
        else:
            sw, sh = get_screen_size()
            bbox = (0, 0, sw, sh)

        cw, ch = bbox[2] - bbox[0], bbox[3] - bbox[1]
        resize = compute_output_size(cw, ch, args.width, args.height)
        out_w, out_h = resize or (cw, ch)
        log.info("Streaming %dx%d region at +%d+%d as %dx%d frames",
                 cw, ch, bbox[0], bbox[1], out_w, out_h)

        try:
            capture = ScreenCapture(bbox, draw_cursor=not args.no_cursor,
                                    backend=args.capture)
        except RuntimeError as e:
            log.error("%s", e)
            return 1
        encoder = FrameEncoder(quality=args.quality, resize=resize)
        injector = InputInjector(enabled=not args.no_input)

        server = ScreenServer(
            args.host, args.port, capture, encoder, injector, args.fps,
            x_offset=bbox[0], y_offset=bbox[1],
            scale_x=cw / out_w, scale_y=ch / out_h,
        )

        watcher = None
        if not args.no_adb:
            if AdbWatcher.available():
                watcher = AdbWatcher(args.port, open_client=args.open)
                server.on_listening = watcher.start
            else:
                log.warning("adb not found — USB auto-connect disabled "
                            "(install with: sudo apt install adb)")

        def _signal_handler(_sig, _frame):
            log.info("Shutting down…")
            if watcher:
                watcher.stop()
            server.stop()

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGHUP, _signal_handler)

        try:
            server.serve_forever()
        except OSError as e:
            log.error("Cannot listen on %s:%d — %s", args.host, args.port, e)
            return 1
        return 0
    finally:
        if capture:
            capture.close()
        if vdisplay:
            vdisplay.remove()


if __name__ == "__main__":
    sys.exit(main())
