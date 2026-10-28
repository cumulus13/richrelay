"""Wire formats: RFC 5424, RFC 3164, JSON, GELF 1.1, logfmt and plain text."""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple, Type, Union

from .event import Event
from .syslog import facility_code, level_name, pri, severity

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_NON_PRINTUSASCII = re.compile(r"[^\x21-\x7e]")


def default_app_name() -> str:
    argv0 = sys.argv[0] if sys.argv else ""
    base = os.path.basename(argv0) if argv0 else ""
    if not base or base == "-c":
        return "python"
    return base[:-3] if base.endswith(".py") else base


def _utc_iso(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}Z"


def _header_field(value: Any, maxlen: int) -> str:
    """RFC 5424 header fields: PRINTUSASCII, max length, NILVALUE when empty."""
    if value is None or value == "":
        return "-"
    out = _NON_PRINTUSASCII.sub("_", str(value))[:maxlen]
    return out or "-"


class Formatter:
    """Base class. ``format`` returns the *unframed* message text."""

    name = "base"
    default_framing = "newline"  # how a stream transport should delimit messages

    def __init__(self, *, hostname: Any = None, app_name: Any = None,
                 facility: Union[str, int] = "user", **options: Any) -> None:
        self.hostname = hostname or socket.gethostname() or "localhost"
        self.app_name = app_name or default_app_name()
        self.facility = facility_code(facility)
        if options:
            raise TypeError(f"{type(self).__name__}: unknown options {sorted(options)}")

    def format(self, event: Event) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    @staticmethod
    def body(event: Event) -> str:
        if event.exc_text:
            return f"{event.message}\n{event.exc_text}" if event.message else event.exc_text
        return event.message


class Rfc5424Formatter(Formatter):
    """``<PRI>1 TIMESTAMP HOSTNAME APP-NAME PROCID MSGID [SD] MSG`` (RFC 5424)."""

    name = "rfc5424"
    default_framing = "octet"  # RFC 6587 octet counting

    def __init__(self, *, sd_id: str = "richrelay@32473", include_sd: bool = True,
                 bom: bool = False, **kw: Any) -> None:
        super().__init__(**kw)
        self.sd_id = sd_id
        self.include_sd = include_sd
        self.bom = bom

    @staticmethod
    def _sd_name(key: Any) -> str:
        return re.sub(r'[^\x21-\x7e]|[=\]"]', "_", str(key))[:32] or "_"

    @staticmethod
    def _sd_value(value: Any) -> str:
        s = str(value)[:512]
        return s.replace("\\", "\\\\").replace('"', '\\"').replace("]", "\\]")

    def _structured_data(self, e: Event) -> str:
        if not self.include_sd:
            return "-"
        params: List[Tuple[str, Any]] = [("logger", e.logger), ("source", e.source)]
        if e.thread:
            params.append(("thread", e.thread))
        params.extend(e.extra.items())
        parts = " ".join(f'{self._sd_name(k)}="{self._sd_value(v)}"' for k, v in params[:32])
        return f"[{self.sd_id} {parts}]"

    def format(self, e: Event) -> str:
        prival = pri(self.facility, severity(e.level))
        head = (
            f"<{prival}>1 {_utc_iso(e.timestamp)} {_header_field(self.hostname, 255)} "
            f"{_header_field(self.app_name, 48)} {_header_field(e.pid, 128)} "
            f"{_header_field(e.msgid, 32)} {self._structured_data(e)}"
        )
        msg = self.body(e)
        if not msg:
            return head
        return f"{head} {chr(0xFEFF) if self.bom else ''}{msg}"


class Rfc3164Formatter(Formatter):
    """BSD syslog: ``<PRI>Mmm dd hh:mm:ss HOSTNAME TAG[PID]: MSG`` (RFC 3164)."""

    name = "rfc3164"

    def __init__(self, *, include_pid: bool = True, **kw: Any) -> None:
        super().__init__(**kw)
        self.include_pid = include_pid

    def format(self, e: Event) -> str:
        prival = pri(self.facility, severity(e.level))
        t = time.localtime(e.timestamp)
        stamp = f"{_MONTHS[t.tm_mon - 1]} {t.tm_mday:2d} {time.strftime('%H:%M:%S', t)}"
        host = _NON_PRINTUSASCII.sub("_", str(self.hostname))[:255] or "localhost"
        tag = re.sub(r"[^A-Za-z0-9_.\-/]", "_", str(self.app_name))[:32] or "python"
        if self.include_pid:
            tag = f"{tag}[{e.pid}]"
        return f"<{prival}>{stamp} {host} {tag}: {self.body(e)}"


class JsonFormatter(Formatter):
    """One JSON object per message."""

    name = "json"

    def format(self, e: Event) -> str:
        doc: Dict[str, Any] = {
            "timestamp": _utc_iso(e.timestamp),
            "level": level_name(e.level),
            "severity": severity(e.level),
            "facility": self.facility,
            "host": self.hostname,
            "app": self.app_name,
            "pid": e.pid,
            "logger": e.logger,
            "source": e.source,
            "message": e.message,
        }
        if e.thread:
            doc["thread"] = e.thread
        if e.exc_text:
            doc["exception"] = e.exc_text
        for k, v in e.extra.items():
            doc[k if k not in doc else f"extra_{k}"] = v
        return json.dumps(doc, default=str, ensure_ascii=False, separators=(",", ":"))


class GelfFormatter(Formatter):
    """GELF 1.1 JSON (Graylog). Uncompressed, no UDP chunking; TCP is NUL-terminated."""

    name = "gelf"
    default_framing = "null"

    def format(self, e: Event) -> str:
        first = (e.message.splitlines() or [""])[0] or "(empty)"
        doc: Dict[str, Any] = {
            "version": "1.1",
            "host": str(self.hostname),
            "short_message": first,
            "timestamp": round(e.timestamp, 6),
            "level": severity(e.level),
            "_app": self.app_name,
            "_logger": e.logger,
            "_source": e.source,
            "_pid": e.pid,
        }
        full = self.body(e)
        if full != first:
            doc["full_message"] = full
        for k, v in e.extra.items():
            key = "_" + re.sub(r"[^\w.\-]", "_", str(k))
            if key != "_id":
                doc[key] = v if isinstance(v, (str, int, float)) else str(v)
        return json.dumps(doc, default=str, ensure_ascii=False, separators=(",", ":"))


def _logfmt_value(v: Any) -> str:
    s = str(v)
    if s == "" or re.search(r'[\s="\\]', s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return s


class LogfmtFormatter(Formatter):
    name = "logfmt"

    def format(self, e: Event) -> str:
        pairs: List[Tuple[str, Any]] = [
            ("ts", _utc_iso(e.timestamp)), ("level", level_name(e.level).lower()),
            ("host", self.hostname), ("app", self.app_name), ("pid", e.pid),
            ("logger", e.logger), ("msg", e.message),
        ]  # fmt: skip
        if e.exc_text:
            pairs.append(("exc", e.exc_text))
        pairs.extend(e.extra.items())
        return " ".join(f"{k}={_logfmt_value(v)}" for k, v in pairs)


class PlainFormatter(Formatter):
    name = "plain"

    def format(self, e: Event) -> str:
        return f"{_utc_iso(e.timestamp)} {level_name(e.level):<8} {e.logger}: {self.body(e)}"


FORMATS: Dict[str, Type[Formatter]] = {
    "rfc5424": Rfc5424Formatter, "5424": Rfc5424Formatter, "syslog": Rfc5424Formatter,
    "rfc3164": Rfc3164Formatter, "3164": Rfc3164Formatter, "bsd": Rfc3164Formatter,
    "json": JsonFormatter, "gelf": GelfFormatter,
    "logfmt": LogfmtFormatter, "plain": PlainFormatter,
}  # fmt: skip


def make_formatter(fmt: Union[str, Formatter], **kwargs: Any) -> Formatter:
    if isinstance(fmt, Formatter):
        return fmt
    try:
        cls = FORMATS[fmt.strip().lower()]
    except KeyError:
        raise ValueError(f"unknown format {fmt!r}; choose from {sorted(set(FORMATS))}") from None
    return cls(**kwargs)
