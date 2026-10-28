"""Parsers for RFC 5424 / RFC 3164 / JSON lines. Used by the bundled viewer and tests."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .syslog import FACILITIES, SEVERITY_NAMES

_SD_ELEM = r'\[[^\s\]=]+(?: [^\s=\]"]+="(?:[^"\\]|\\.)*")*\]'
_RE_5424 = re.compile(
    rf"^<(\d{{1,3}})>(\d{{1,2}}) (\S+) (\S+) (\S+) (\S+) (\S+) (-|(?:{_SD_ELEM})+)(?: (.*))?$", re.S)
_RE_3164 = re.compile(
    r"^<(\d{1,3})>(?:([A-Z][a-z]{2}) +(\d{1,2}) (\d{2}:\d{2}:\d{2}) )?"
    r"(?:(\S+) )?(?:([^\s:\[]+)(?:\[(\d+)\])?: ?)?(.*)$", re.S)
_RE_PARAM = re.compile(r'([^\s=\]"]+)="((?:[^"\\]|\\.)*)"')
_FAC_NAMES = {v: k for k, v in FACILITIES.items()}


@dataclass
class ParsedMessage:
    format: str
    message: str
    facility: Optional[int] = None
    severity: Optional[int] = None
    timestamp: Optional[str] = None
    hostname: Optional[str] = None
    app: Optional[str] = None
    procid: Optional[str] = None
    msgid: Optional[str] = None
    structured_data: Dict[str, Dict[str, str]] = field(default_factory=dict)
    fields: Dict[str, Any] = field(default_factory=dict)

    @property
    def facility_name(self) -> str:
        return _FAC_NAMES.get(self.facility, "?") if self.facility is not None else "?"

    @property
    def severity_name(self) -> str:
        return SEVERITY_NAMES[self.severity] if self.severity is not None else "?"

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["facility_name"], d["severity_name"] = self.facility_name, self.severity_name
        return d


def _nil(v: Optional[str]) -> Optional[str]:
    return None if v in (None, "-") else v


def parse_structured_data(sd: str) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    if sd == "-":
        return out
    for m in re.finditer(_SD_ELEM, sd):
        elem = m.group(0)[1:-1]
        sd_id = elem.split(" ", 1)[0]
        out[sd_id] = {
            k: re.sub(r'\\(["\\\]])', r"\1", v) for k, v in _RE_PARAM.findall(elem)
        }
    return out


def parse_message(raw: "bytes | str") -> ParsedMessage:
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    text = text.strip("\r\n\x00")
    m = _RE_5424.match(text)
    if m:
        pri = int(m.group(1))
        return ParsedMessage(
            "rfc5424", (m.group(9) or "").lstrip("\ufeff"), pri >> 3, pri & 7, _nil(m.group(3)),
            _nil(m.group(4)), _nil(m.group(5)), _nil(m.group(6)), _nil(m.group(7)),
            parse_structured_data(m.group(8)),
        )
    if text.startswith("{"):
        try:
            doc = json.loads(text)
            if isinstance(doc, dict):
                msg = doc.get("message") or doc.get("short_message") or ""
                return ParsedMessage(
                    "gelf" if doc.get("version") == "1.1" else "json", str(msg),
                    severity=doc.get("severity", doc.get("level")) if isinstance(
                        doc.get("severity", doc.get("level")), int) else None,
                    timestamp=str(doc.get("timestamp", "")) or None,
                    hostname=doc.get("host"), app=doc.get("app") or doc.get("_app"), fields=doc)
        except ValueError:
            pass
    m = _RE_3164.match(text)
    if m and m.group(1):
        pri = int(m.group(1))
        ts = f"{m.group(2)} {m.group(3)} {m.group(4)}" if m.group(2) else None
        return ParsedMessage("rfc3164", m.group(8), pri >> 3, pri & 7, ts,
                             m.group(5), m.group(6), m.group(7))
    return ParsedMessage("raw", text)
