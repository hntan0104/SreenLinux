#!/usr/bin/env python3
"""Connection transports for SreenLinux.

One TCP port serves two kinds of clients:

* **Android app** — raw binary protocol. The app starts by sending the
  6-byte hello ``b"SRLN" + version (uint16)``.
* **Web browser** — ``GET /`` returns the web client page, ``GET /ws``
  upgrades to a WebSocket that carries exactly the same messages
  (server→client: one binary WS message per JPEG frame; client→server:
  binary WS messages containing one or more 6-byte input messages).

Anything else is rejected: an HTTP POST cannot smuggle input bytes into the
raw protocol, and idle sockets (browsers pre-open spare connections) are
closed after ``SNIFF_TIMEOUT`` instead of replacing the active client.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import logging
import os
import re
import socket
import struct
import threading
from typing import Optional
from urllib.parse import urlsplit

log = logging.getLogger("sreen_server.transport")

SNIFF_TIMEOUT = 10.0
HELLO_MAGIC = b"SRLN"
MSG_LEN = 6
MAX_HEADER_BYTES = 16 * 1024
MAX_WS_PAYLOAD = 64 * 1024
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class Transport:
    """Base class: frame sending and input-message receiving."""

    kind = "?"

    def __init__(self, conn: socket.socket):
        self.conn = conn
        self._send_lock = threading.Lock()

    def send_frame(self, jpeg: bytes) -> int:
        raise NotImplementedError

    def recv_message(self) -> Optional[bytes]:
        """Return the next 6-byte input message, or None at end of stream."""
        raise NotImplementedError

    def close(self) -> None:
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass

    def _recvall(self, n: int) -> Optional[bytes]:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.conn.recv(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)


class RawTransport(Transport):
    """Android app protocol: [4 B size][JPEG] out, 6-byte messages in."""

    kind = "app"

    def send_frame(self, jpeg: bytes) -> int:
        data = struct.pack("!I", len(jpeg)) + jpeg
        with self._send_lock:
            self.conn.sendall(data)
        return len(data)

    def recv_message(self) -> Optional[bytes]:
        return self._recvall(MSG_LEN)


class WebSocketTransport(Transport):
    """Minimal RFC 6455 server side (binary messages only)."""

    kind = "web"

    def __init__(self, conn: socket.socket):
        super().__init__(conn)
        self._pending = bytearray()   # input bytes not yet returned
        self._closed = False

    @staticmethod
    def _header(opcode: int, length: int) -> bytes:
        if length < 126:
            return struct.pack("!BB", 0x80 | opcode, length)
        if length < 65536:
            return struct.pack("!BBH", 0x80 | opcode, 126, length)
        return struct.pack("!BBQ", 0x80 | opcode, 127, length)

    def _send(self, opcode: int, payload: bytes) -> int:
        data = self._header(opcode, len(payload)) + payload
        with self._send_lock:
            self.conn.sendall(data)
        return len(data)

    def send_frame(self, jpeg: bytes) -> int:
        return self._send(0x2, jpeg)

    def _read_ws_message(self) -> Optional[bytes]:
        """Read one complete data message (handles control frames)."""
        message = bytearray()
        while True:
            hdr = self._recvall(2)
            if hdr is None:
                return None
            b0, b1 = hdr
            fin, opcode = b0 & 0x80, b0 & 0x0F
            masked, length = b1 & 0x80, b1 & 0x7F
            if length == 126:
                ext = self._recvall(2)
                if ext is None:
                    return None
                length = struct.unpack("!H", ext)[0]
            elif length == 127:
                ext = self._recvall(8)
                if ext is None:
                    return None
                length = struct.unpack("!Q", ext)[0]
            if not masked or length > MAX_WS_PAYLOAD:
                # RFC 6455: client frames must be masked
                self._send_close(1002)
                return None
            mask = self._recvall(4)
            payload = self._recvall(length) if length else b""
            if mask is None or payload is None:
                return None
            payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))

            if opcode == 0x8:  # close
                self._send_close(1000)
                return None
            if opcode == 0x9:  # ping
                self._send(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            message.extend(payload)  # 0x0 continuation, 0x1 text, 0x2 binary
            if len(message) > MAX_WS_PAYLOAD:
                self._send_close(1009)
                return None
            if fin:
                return bytes(message)

    def _send_close(self, code: int) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._send(0x8, struct.pack("!H", code))
        except OSError:
            pass

    def recv_message(self) -> Optional[bytes]:
        while len(self._pending) < MSG_LEN:
            msg = self._read_ws_message()
            if msg is None:
                return None
            self._pending.extend(msg)
        out = bytes(self._pending[:MSG_LEN])
        del self._pending[:MSG_LEN]
        return out

    def close(self) -> None:
        self._send_close(1001)
        super().close()


# ---------------------------------------------------------------------------
# Connection classification + HTTP
# ---------------------------------------------------------------------------

_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "[::1]"}


def _host_allowed(host_header: str, bind_host: str) -> bool:
    """Guard against DNS rebinding: only accept loopback names / IP literals."""
    name = host_header.strip().lower()
    if name.startswith("["):
        name = name[:name.find("]") + 1]
    elif ":" in name:
        name = name.rsplit(":", 1)[0]
    if name in _LOOPBACK_NAMES:
        return True
    if bind_host in ("127.0.0.1", "localhost", "::1"):
        return False
    try:
        ipaddress.ip_address(name.strip("[]"))
        return True  # Wi-Fi mode: allow raw IP addresses, never DNS names
    except ValueError:
        return False


def _origin_allowed(origin: Optional[str], host_header: str) -> bool:
    """WebSockets ignore CORS, so check that the page came from this server."""
    if origin is None:
        return True  # non-browser client
    parts = urlsplit(origin)
    return parts.scheme == "http" and parts.netloc.lower() == host_header.strip().lower()


def _http_response(conn: socket.socket, status: str, body: bytes = b"",
                   content_type: str = "text/plain; charset=utf-8",
                   extra: str = "") -> None:
    head = (f"HTTP/1.1 {status}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            f"{extra}"
            "Connection: close\r\n\r\n")
    try:
        conn.sendall(head.encode() + body)
    except OSError:
        pass


def _read_http_request(conn: socket.socket) -> Optional[tuple[str, str, dict]]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return None
        data.extend(chunk)
        if len(data) > MAX_HEADER_BYTES:
            return None
    head = bytes(data[:data.index(b"\r\n\r\n")]).decode("latin-1")
    lines = head.split("\r\n")
    m = re.match(r"^([A-Z]+) (\S+) HTTP/1\.[01]$", lines[0])
    if not m:
        return None
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return m.group(1), m.group(2), headers


_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/manifest.json": ("manifest.json", "application/manifest+json"),
    "/icon.png": ("icon.png", "image/png"),
}


def classify(conn: socket.socket, bind_host: str) -> Optional[Transport]:
    """Decide what kind of client this is.

    Returns a Transport for streaming clients, or None if the connection was
    fully handled (static file, error) and has been closed.
    """
    conn.settimeout(SNIFF_TIMEOUT)
    try:
        first = conn.recv(4, socket.MSG_PEEK | socket.MSG_WAITALL)
    except (socket.timeout, OSError):
        conn.close()  # idle (e.g. a browser's speculative pre-connect)
        return None
    if not first:
        conn.close()
        return None

    if first == HELLO_MAGIC:
        hello = None
        try:
            hello = RawTransport(conn)._recvall(MSG_LEN)
        except OSError:
            pass
        if hello is None:
            conn.close()
            return None
        log.debug("App hello, protocol version %d", struct.unpack("!H", hello[4:6])[0])
        conn.settimeout(None)
        return RawTransport(conn)

    conn.settimeout(5.0)
    try:
        req = _read_http_request(conn) if re.match(rb"^[A-Z]+ ", first) else None
        if req is None:
            # Raw clients must stay silent first; reject anything else.
            _http_response(conn, "400 Bad Request", b"Bad request\n")
            conn.close()
            return None
        method, target, headers = req
        path = target.split("?", 1)[0]
        host_header = headers.get("host", "")

        if not _host_allowed(host_header, bind_host):
            log.warning("Rejected request with Host: %r", host_header)
            _http_response(conn, "403 Forbidden", b"Forbidden host\n")
            conn.close()
            return None
        if method != "GET":
            _http_response(conn, "405 Method Not Allowed", b"Method not allowed\n",
                           extra="Allow: GET\r\n")
            conn.close()
            return None

        if path == "/ws":
            if headers.get("upgrade", "").lower() != "websocket" or "sec-websocket-key" not in headers:
                _http_response(conn, "426 Upgrade Required", b"WebSocket only\n")
                conn.close()
                return None
            if not _origin_allowed(headers.get("origin"), host_header):
                log.warning("Rejected WebSocket from origin %r", headers.get("origin"))
                _http_response(conn, "403 Forbidden", b"Forbidden origin\n")
                conn.close()
                return None
            accept = base64.b64encode(hashlib.sha1(
                (headers["sec-websocket-key"] + WS_GUID).encode()).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\n"
                          "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            conn.settimeout(None)
            return WebSocketTransport(conn)

        if path in _STATIC:
            fname, ctype = _STATIC[path]
            try:
                with open(os.path.join(WEB_DIR, fname), "rb") as f:
                    body = f.read()
                _http_response(conn, "200 OK", body, ctype)
            except OSError:
                _http_response(conn, "404 Not Found", b"Not found\n")
        else:
            _http_response(conn, "404 Not Found", b"Not found\n")
        conn.close()
        return None
    except (OSError, socket.timeout):
        conn.close()
        return None
