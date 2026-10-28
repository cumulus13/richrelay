"""Destination / buffer URL parsing.

  udp://host:514?format=rfc3164&facility=local0
  tcp://host:514?format=rfc5424&buffer=sqlite%3A%2F%2F%2Ftmp%2Fspool.db
  tls://host:6514?verify=0        unix:///dev/log
Buffers: memory://?max_items=5000   sqlite:///path/spool.db?max_mb=50
         redis://host:6379/0?key=q&max_items=100000   amqp://user:pw@host/%2F?queue=q
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .buffers import Buffer, MemoryBuffer, RabbitMQBuffer, RedisBuffer, SQLiteBuffer
from .relay import Destination, tcp, udp, unix
from .syslog import level_from_name

_TRUE = {"1", "true", "yes", "on"}


def _int(q: Dict[str, str], key: str) -> Optional[int]:
    return int(q[key]) if key in q else None


def parse_buffer(url: str) -> Buffer:
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query))
    overflow = q.get("overflow", "drop_oldest")
    scheme = parts.scheme.lower()
    if scheme == "memory":
        return MemoryBuffer(max_items=_int(q, "max_items") or 10_000,
                            max_bytes=_int(q, "max_bytes"), overflow=overflow)
    if scheme == "sqlite":
        path = parts.path
        if re.match(r"^/[A-Za-z]:", path):  # sqlite:///C:/x/y.db
            path = path[1:]
        max_mb = float(q["max_mb"]) if "max_mb" in q else 64
        return SQLiteBuffer(path or None, max_items=_int(q, "max_items") or 100_000,
                            max_bytes=int(max_mb * 1024 * 1024), overflow=overflow)
    ours = {"key", "queue", "max_items", "overflow"}
    rest = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k not in ours])
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, rest, ""))
    if scheme in ("redis", "rediss", "unix"):
        return RedisBuffer(clean, key=q.get("key", "richrelay:spool"),
                           max_items=_int(q, "max_items") or 100_000, overflow=overflow)
    if scheme in ("amqp", "amqps"):
        return RabbitMQBuffer(clean, queue=q.get("queue", "richrelay.spool"),
                              max_items=_int(q, "max_items") or 100_000, overflow=overflow)
    raise ValueError(f"unknown buffer scheme in {url!r}")


def parse_destination(url: str, **overrides: Any) -> Destination:
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query))
    scheme = parts.scheme.lower()
    dest_kw: Dict[str, Any] = {"format": q.get("format", "rfc5424")}
    if "facility" in q:
        dest_kw["facility"] = int(q["facility"]) if q["facility"].isdigit() else q["facility"]
    if "level" in q:
        dest_kw["min_level"] = level_from_name(q["level"])
    if "max_bytes" in q:
        dest_kw["max_bytes"] = int(q["max_bytes"])
    if "name" in q:
        dest_kw["name"] = q["name"]
    if "buffer" in q:
        dest_kw["buffer"] = parse_buffer(q["buffer"])
    dest_kw.update(overrides)

    if scheme == "udp":
        return udp(parts.hostname or "localhost", parts.port or 514, **dest_kw)
    if scheme in ("tcp", "tls", "tcp+tls"):
        use_tls = scheme != "tcp"
        return tcp(
            parts.hostname or "localhost", parts.port or (6514 if use_tls else 514),
            tls=use_tls, framing=q.get("framing", "auto"),
            verify=q.get("verify", "1").lower() in _TRUE, ca_file=q.get("ca"),
            cert_file=q.get("cert"), key_file=q.get("key"), **dest_kw,
        )
    if scheme == "unix":
        return unix(parts.path or "/dev/log", **dest_kw)
    raise ValueError(f"unknown destination scheme in {url!r}")
