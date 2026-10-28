"""Network transports: UDP, TCP, TLS and Unix datagram sockets."""

from __future__ import annotations

import errno
import select
import socket
import ssl
from typing import Optional


class TransportError(Exception):
    """Raised when a payload could not be delivered.

    ``permanent`` means retrying the same payload can never succeed
    (for example a datagram larger than the path allows) so it is dropped
    instead of blocking the spool forever.
    """

    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class Transport:
    kind = "base"
    default_max_bytes = 1_048_576

    def send(self, payload: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        pass

    def resolve_framing(self, default: str) -> None:
        pass

    def describe(self) -> str:
        return self.kind


class UDPTransport(Transport):
    """Connected UDP socket. Delivery is best effort (see README, "UDP caveat")."""

    kind = "udp"
    default_max_bytes = 8192  # rsyslog's default maxMessageSize

    def __init__(self, host: str, port: int = 514, *, broadcast: bool = False,
                 timeout: float = 2.0) -> None:
        self.host, self.port = host, int(port)
        self.broadcast, self.timeout = broadcast, timeout
        self._sock: Optional[socket.socket] = None

    def _connect(self) -> None:
        last: Optional[OSError] = None
        for fam, st, proto, _, sa in socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_DGRAM):
            s = None
            try:
                s = socket.socket(fam, st, proto)
                s.settimeout(self.timeout)
                if self.broadcast:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.connect(sa)
                self._sock = s
                return
            except OSError as exc:
                last = exc
                if s is not None:
                    s.close()
        raise last or OSError("no usable address")

    def send(self, payload: bytes) -> None:
        try:
            if self._sock is None:
                self._connect()
            assert self._sock is not None
            self._sock.send(payload)
        except OSError as exc:
            self.close()
            raise TransportError(
                f"udp {self.host}:{self.port}: {exc}",
                permanent=getattr(exc, "errno", None) == errno.EMSGSIZE,
            ) from exc

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def describe(self) -> str:
        return f"udp://{self.host}:{self.port}"


class TCPTransport(Transport):
    """TCP with optional TLS, lazy (re)connect and dead-peer detection."""

    kind = "tcp"

    def __init__(self, host: str, port: int = 514, *, tls: bool = False,
                 ca_file: Optional[str] = None, cert_file: Optional[str] = None,
                 key_file: Optional[str] = None, verify: bool = True,
                 server_hostname: Optional[str] = None, framing: str = "auto",
                 connect_timeout: float = 3.0, send_timeout: float = 5.0) -> None:
        if framing not in ("auto", "octet", "newline", "null", "none"):
            raise ValueError(f"unknown framing {framing!r}")
        self.host, self.port, self.tls = host, int(port), tls
        self.ca_file, self.cert_file, self.key_file = ca_file, cert_file, key_file
        self.verify, self.server_hostname = verify, server_hostname
        self.framing = framing
        self.active_framing = "newline" if framing == "auto" else framing
        self.connect_timeout, self.send_timeout = connect_timeout, send_timeout
        self._sock: Optional[socket.socket] = None
        self._ctx: Optional[ssl.SSLContext] = None
        if tls:
            self.kind = "tls"

    def resolve_framing(self, default: str) -> None:
        if self.framing == "auto":
            self.active_framing = default

    def _context(self) -> ssl.SSLContext:
        if self._ctx is None:
            ctx = ssl.create_default_context(cafile=self.ca_file)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            if not self.verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            if self.cert_file:
                ctx.load_cert_chain(self.cert_file, self.key_file)
            self._ctx = ctx
        return self._ctx

    def _connect(self) -> None:
        self._drop()
        s = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if self.tls:
                s = self._context().wrap_socket(s, server_hostname=self.server_hostname or self.host)
            s.settimeout(self.send_timeout)
        except BaseException:
            s.close()
            raise
        self._sock = s

    def _drop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _alive(self) -> bool:
        """Detect a peer that closed the connection since our last send."""
        s = self._sock
        if s is None:
            return False
        try:
            readable, _, _ = select.select([s], [], [], 0)
            if not readable:
                return True
            if isinstance(s, ssl.SSLSocket):
                s.settimeout(0.0)
                try:
                    data = s.recv(1)
                except (ssl.SSLWantReadError, BlockingIOError):
                    return True
                finally:
                    s.settimeout(self.send_timeout)
                return bool(data)
            if s.recv(1, socket.MSG_PEEK) == b"":
                return False
            s.recv(65536)  # unexpected inbound data: discard
            return True
        except (OSError, ValueError):
            return False

    def _frame(self, payload: bytes) -> bytes:
        f = self.active_framing
        if f == "octet":
            return str(len(payload)).encode("ascii") + b" " + payload
        if f == "newline":
            return payload.replace(b"\r", b"").replace(b"\n", b"#012") + b"\n"
        if f == "null":
            return payload.replace(b"\x00", b"") + b"\x00"
        return payload

    def send(self, payload: bytes) -> None:
        data = self._frame(payload)
        try:
            if not self._alive():
                self._connect()
        except OSError as exc:  # includes ssl.SSLError, timeouts, DNS errors
            self._drop()
            raise TransportError(f"{self.kind} {self.host}:{self.port}: {exc}") from exc
        try:
            assert self._sock is not None
            self._sock.sendall(data)
            return
        except OSError:
            self._drop()
        try:  # the connection died under us: reconnect once and resend
            self._connect()
            assert self._sock is not None
            self._sock.sendall(data)
        except OSError as exc:
            self._drop()
            raise TransportError(f"{self.kind} {self.host}:{self.port}: {exc}") from exc

    def close(self) -> None:
        self._drop()

    def describe(self) -> str:
        return f"{self.kind}://{self.host}:{self.port}"


class UnixTransport(Transport):
    """Unix datagram socket such as ``/dev/log``."""

    kind = "unix"
    default_max_bytes = 8192

    def __init__(self, path: str = "/dev/log") -> None:
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("Unix sockets are not available on this platform")
        self.path = path
        self._sock: Optional[socket.socket] = None

    def send(self, payload: bytes) -> None:
        try:
            if self._sock is None:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                try:
                    s.connect(self.path)
                except OSError:
                    s.close()
                    raise
                self._sock = s
            self._sock.send(payload)
        except OSError as exc:
            self.close()
            raise TransportError(f"unix {self.path}: {exc}") from exc

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def describe(self) -> str:
        return f"unix://{self.path}"
