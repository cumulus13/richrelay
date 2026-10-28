"""Tiny syslog receiver (UDP + TCP/TLS) and a Rich-based live viewer."""

from __future__ import annotations

import re
import socket
import ssl
import threading
from typing import Any, Callable, Iterator, List, Optional, Tuple

Callback = Callable[[bytes, Tuple[Any, ...], str], None]


def iter_frames(buf: bytearray) -> Iterator[bytes]:
    """Extract complete messages from a TCP stream buffer (RFC 6587 octet-counting,
    otherwise LF- or NUL-delimited). Consumes what it yields."""
    while buf:
        if buf[:1].isdigit():
            m = re.match(rb"(\d{1,7}) ", bytes(buf[:9]))
            if m:
                n, start = int(m.group(1)), m.end()
                if len(buf) < start + n:
                    return
                frame = bytes(buf[start:start + n])
                del buf[:start + n]
                yield frame
                continue
            if re.fullmatch(rb"\d{1,7}", bytes(buf[:8])):
                return  # length prefix not complete yet
        idx = min((i for i in (buf.find(b"\n"), buf.find(b"\x00")) if i >= 0), default=-1)
        if idx < 0:
            return
        line = bytes(buf[:idx])
        del buf[:idx + 1]
        if line.strip():
            yield line


class Receiver:
    """Threaded receiver; ``stop()`` closes listeners *and* live connections (simulates a crash)."""

    def __init__(self, callback: Callback, *, host: str = "127.0.0.1",
                 udp_port: Optional[int] = None, tcp_port: Optional[int] = None,
                 certfile: Optional[str] = None, keyfile: Optional[str] = None) -> None:
        self.callback, self.host = callback, host
        self._want_udp, self._want_tcp = udp_port, tcp_port
        self.certfile, self.keyfile = certfile, keyfile
        self.udp_port: Optional[int] = None
        self.tcp_port: Optional[int] = None
        self._socks: List[socket.socket] = []
        self._conns: List[socket.socket] = []
        self._threads: List[threading.Thread] = []
        self._running = False
        self._lock = threading.Lock()

    def start(self) -> "Receiver":
        self._running = True
        if self._want_udp is not None:
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.bind((self.host, self._want_udp))
            u.settimeout(0.2)
            self.udp_port = u.getsockname()[1]
            self._socks.append(u)
            self._spawn(self._udp_loop, u)
        if self._want_tcp is not None:
            t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            t.bind((self.host, self._want_tcp))
            t.listen(16)
            t.settimeout(0.2)
            self.tcp_port = t.getsockname()[1]
            self._socks.append(t)
            self._spawn(self._accept_loop, t)
        return self

    def _spawn(self, fn: Callable[..., None], *args: Any) -> None:
        th = threading.Thread(target=fn, args=args, daemon=True)
        th.start()
        self._threads.append(th)

    def _udp_loop(self, sock: socket.socket) -> None:
        while self._running:
            try:
                data, peer = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            self.callback(data, peer, "udp")

    def _accept_loop(self, srv: socket.socket) -> None:
        ctx = None
        if self.certfile:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(self.certfile, self.keyfile)
        while self._running:
            try:
                conn, peer = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                if ctx is not None:
                    conn = ctx.wrap_socket(conn, server_side=True)
            except (ssl.SSLError, OSError):
                conn.close()
                continue
            with self._lock:
                self._conns.append(conn)
            self._spawn(self._conn_loop, conn, peer)

    def _conn_loop(self, conn: socket.socket, peer: Tuple[Any, ...]) -> None:
        buf = bytearray()
        conn.settimeout(0.2)
        try:
            while self._running:
                try:
                    chunk = conn.recv(65536)
                except (socket.timeout, ssl.SSLWantReadError):
                    continue
                if not chunk:
                    break
                buf += chunk
                for frame in iter_frames(buf):
                    self.callback(frame, peer, "tls" if self.certfile else "tcp")
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._running = False
        for s in self._socks:
            try:
                s.close()
            except OSError:
                pass
        with self._lock:
            conns, self._conns = self._conns, []
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass
        for th in self._threads:
            th.join(1.0)
        self._socks, self._threads = [], []


SEV_STYLE = {0: "bold white on red", 1: "bold red", 2: "bold red", 3: "red",
             4: "yellow", 5: "cyan", 6: "green", 7: "dim"}


def run_viewer(host: str, udp_port: Optional[int], tcp_port: Optional[int], *,
               certfile: Optional[str] = None, keyfile: Optional[str] = None,
               as_json: bool = False, raw: bool = False) -> None:
    import json
    import time

    from rich.console import Console
    from rich.markup import escape
    from rich.text import Text

    from .parsers import parse_message

    console = Console()

    def on_msg(data: bytes, peer: Tuple[Any, ...], proto: str) -> None:
        if raw:
            console.print(Text(data.decode("utf-8", "replace")))
            return
        p = parse_message(data)
        if as_json:
            console.print_json(json.dumps(p.to_dict(), default=str))
            return
        style = SEV_STYLE.get(p.severity if p.severity is not None else 6, "")
        head = Text.assemble(
            (f"{p.timestamp or time.strftime('%H:%M:%S')} ", "dim"),
            (f"{p.severity_name:<7}", style), " ",
            (f"{p.hostname or peer[0]} ", "blue"),
            (f"{p.app or '-'}", "magenta"),
            (f"[{p.procid}]" if p.procid else "", "magenta"),
            (f" ({p.format}/{proto}) ", "dim"),
        )
        console.print(head, Text(p.message), sep="")

    rx = Receiver(on_msg, host=host, udp_port=udp_port, tcp_port=tcp_port,
                  certfile=certfile, keyfile=keyfile).start()
    ports = ", ".join(x for x in (f"udp/{rx.udp_port}" if rx.udp_port else "",
                                  f"tcp/{rx.tcp_port}" if rx.tcp_port else "") if x)
    console.print(f"[bold]richrelay listen[/bold] on {escape(host)} {ports} - Ctrl+C to stop")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        rx.stop()
