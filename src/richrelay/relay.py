"""Relay: fan-out of events to several destinations, plus global setup()."""

from __future__ import annotations

import atexit
import logging
import os
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union

from .buffers import Buffer
from .channel import CacheChannel, Channel
from .event import Event
from .formats import Formatter, make_formatter
from .syslog import facility_code
from .transports import TCPTransport, Transport, UDPTransport, UnixTransport


@dataclass
class Destination:
    transport: Transport
    format: Union[str, Formatter] = "rfc5424"
    buffer: Optional[Buffer] = None
    name: Optional[str] = None
    min_level: int = logging.NOTSET
    facility: Optional[Union[str, int]] = None
    max_bytes: Optional[int] = None
    queue_size: int = 10_000
    spill_items: int = 10_000
    batch_size: int = 200
    backoff_initial: float = 1.0
    backoff_max: float = 60.0
    formatter_options: Dict[str, Any] = field(default_factory=dict)


def udp(host: str, port: int = 514, *, broadcast: bool = False, **dest: Any) -> Destination:
    return Destination(UDPTransport(host, port, broadcast=broadcast), **dest)


def tcp(host: str, port: int = 514, *, tls: bool = False, ca_file: Optional[str] = None,
        cert_file: Optional[str] = None, key_file: Optional[str] = None, verify: bool = True,
        server_hostname: Optional[str] = None, framing: str = "auto",
        connect_timeout: float = 3.0, send_timeout: float = 5.0, **dest: Any) -> Destination:
    transport = TCPTransport(
        host, port, tls=tls, ca_file=ca_file, cert_file=cert_file, key_file=key_file,
        verify=verify, server_hostname=server_hostname, framing=framing,
        connect_timeout=connect_timeout, send_timeout=send_timeout,
    )
    return Destination(transport, **dest)


def tls(host: str, port: int = 6514, **kw: Any) -> Destination:
    return tcp(host, port, tls=True, **kw)


def unix(path: str = "/dev/log", **dest: Any) -> Destination:
    return Destination(UnixTransport(path), **dest)


def format_exc_info(exc_info: Any) -> Optional[str]:
    if not exc_info or exc_info[0] is None:
        return None
    return "".join(traceback.format_exception(*exc_info)).rstrip("\n")


class Relay:
    def __init__(self, destinations: Sequence[Union[Destination, str]], *,
                 app_name: Optional[str] = None, hostname: Optional[str] = None,
                 facility: Union[str, int] = "user", caches: Sequence[Any] = ()) -> None:
        from .urls import parse_destination  # local import avoids a cycle

        self.app_name, self.hostname, self.facility = app_name, hostname, facility_code(facility)
        self.channels: List[Channel] = []
        self.cache_channels: List[CacheChannel] = []
        self._closed = False
        for i, d in enumerate(destinations):
            dest = parse_destination(d) if isinstance(d, str) else d
            fac = dest.facility if dest.facility is not None else self.facility
            fmt = make_formatter(
                dest.format, hostname=hostname, app_name=app_name, facility=fac,
                **dest.formatter_options,
            ) if not isinstance(dest.format, Formatter) else dest.format
            self.channels.append(Channel(
                dest.name or f"{dest.transport.kind}{i}", dest.transport, fmt, dest.buffer,
                min_level=dest.min_level, queue_size=dest.queue_size, max_bytes=dest.max_bytes,
                spill_items=dest.spill_items, batch_size=dest.batch_size,
                backoff_initial=dest.backoff_initial, backoff_max=dest.backoff_max,
            ))
        for i, cache in enumerate(caches):
            fmt = make_formatter(getattr(cache, "format", "json"), hostname=hostname,
                                 app_name=app_name, facility=self.facility)
            self.cache_channels.append(CacheChannel(f"cache{i}", cache, fmt))
        atexit.register(self._atexit)

    # ------------------------------------------------------------------- emit
    def emit(self, event: Event) -> None:
        if self._closed:
            return
        for ch in self.channels:
            ch.submit(event)
        for cc in self.cache_channels:
            cc.submit(event)

    def log(self, level: int, message: str, *, logger: str = "richrelay", exc_info: Any = None,
            source: str = "api", **extra: Any) -> None:
        if exc_info is True:
            exc_info = sys.exc_info()
        elif isinstance(exc_info, BaseException):
            exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
        self.emit(Event(message=str(message), level=level, logger=logger,
                        exc_text=format_exc_info(exc_info), extra=extra, source=source))

    def debug(self, msg: str, **kw: Any) -> None: self.log(logging.DEBUG, msg, **kw)
    def info(self, msg: str, **kw: Any) -> None: self.log(logging.INFO, msg, **kw)
    def warning(self, msg: str, **kw: Any) -> None: self.log(logging.WARNING, msg, **kw)
    def error(self, msg: str, **kw: Any) -> None: self.log(logging.ERROR, msg, **kw)
    def critical(self, msg: str, **kw: Any) -> None: self.log(logging.CRITICAL, msg, **kw)

    def exception(self, msg: str, **kw: Any) -> None:
        kw.setdefault("exc_info", True)
        self.log(logging.ERROR, msg, **kw)

    # ---------------------------------------------------------------- control
    def flush(self, timeout: float = 5.0) -> bool:
        ok = True
        for ch in [*self.channels, *self.cache_channels]:
            ok = ch.flush(timeout) and ok
        return ok

    def wait_drained(self, timeout: float = 30.0) -> bool:
        """Retry now and wait until every buffer is empty."""
        return all([ch.wait_drained(timeout) for ch in self.channels])

    def stats(self) -> Dict[str, Any]:
        return {"destinations": [c.stats() for c in self.channels],
                "caches": [c.stats() for c in self.cache_channels]}

    def close(self, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        for ch in [*self.channels, *self.cache_channels]:
            ch.close(timeout)
        try:
            atexit.unregister(self._atexit)
        except Exception:
            pass

    def _atexit(self) -> None:
        self.close(timeout=3.0)

    def __enter__(self) -> "Relay":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ------------------------------------------------------------ global instance
_default: Optional[Relay] = None
_state: Dict[str, Any] = {}


def get_relay() -> Optional[Relay]:
    return _default


def setup(destinations: Optional[Sequence[Union[Destination, str]]] = None, *,
          app_name: Optional[str] = None, hostname: Optional[str] = None,
          facility: Union[str, int] = "user", caches: Sequence[Any] = (),
          patch_rich: bool = True, excepthook: bool = True, rich_traceback: bool = False,
          attach_logging: Union[bool, str, logging.Logger] = False,
          **patch_options: Any) -> Relay:
    """Create the global Relay and enable the integrations.

    ``destinations`` falls back to whitespace-separated URLs in ``$RICHRELAY_URLS``.
    """
    global _default
    from . import integrations

    shutdown()
    if destinations is None:
        env = os.environ.get("RICHRELAY_URLS", "").split()
        if not env:
            raise ValueError("no destinations given and $RICHRELAY_URLS is empty")
        destinations = env
    relay = Relay(destinations, app_name=app_name, hostname=hostname, facility=facility, caches=caches)
    _default = relay
    if patch_rich:
        _state["patch"] = integrations.patch_rich(relay, **patch_options)
    if excepthook:
        _state["hook"] = integrations.install_excepthook(relay, rich_traceback=rich_traceback)
    if attach_logging:
        target = logging.getLogger() if attach_logging is True else (
            logging.getLogger(attach_logging) if isinstance(attach_logging, str) else attach_logging)
        handler = integrations.RelayHandler(relay)
        target.addHandler(handler)
        _state["handler"] = (target, handler)
    return relay


def shutdown(timeout: float = 5.0) -> None:
    global _default
    for key in ("patch", "hook"):
        obj = _state.pop(key, None)
        if obj is not None:
            obj.restore()
    pair = _state.pop("handler", None)
    if pair:
        pair[0].removeHandler(pair[1])
    if _default is not None:
        _default.close(timeout)
        _default = None
