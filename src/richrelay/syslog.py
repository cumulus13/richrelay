"""Syslog constants and level mapping (RFC 5424 section 6.2)."""

from __future__ import annotations

from typing import Union

FACILITIES = {
    "kern": 0, "user": 1, "mail": 2, "daemon": 3, "auth": 4, "syslog": 5,
    "lpr": 6, "news": 7, "uucp": 8, "cron": 9, "authpriv": 10, "ftp": 11,
    "ntp": 12, "audit": 13, "alert": 14, "clock": 15,
    "local0": 16, "local1": 17, "local2": 18, "local3": 19,
    "local4": 20, "local5": 21, "local6": 22, "local7": 23,
}  # fmt: skip

SEVERITY_NAMES = ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug")

NOTICE = 25  # not a stdlib level, but maps to syslog "notice"

_NAME_TO_LEVEL = {
    "debug": 10, "info": 20, "notice": NOTICE, "warning": 30, "warn": 30,
    "error": 40, "err": 40, "critical": 50, "crit": 50, "fatal": 50,
    "alert": 55, "emerg": 60,
}  # fmt: skip


def facility_code(facility: Union[str, int]) -> int:
    if isinstance(facility, int):
        if 0 <= facility <= 23:
            return facility
        raise ValueError(f"facility out of range 0..23: {facility}")
    try:
        return FACILITIES[facility.strip().lower()]
    except KeyError:
        raise ValueError(f"unknown syslog facility: {facility!r}") from None


def severity(levelno: int) -> int:
    """Map a Python logging level number to a syslog severity (0..7)."""
    if levelno >= 60:
        return 0
    if levelno >= 55:
        return 1
    if levelno >= 50:
        return 2
    if levelno >= 40:
        return 3
    if levelno >= 30:
        return 4
    if levelno >= NOTICE:
        return 5
    if levelno >= 20:
        return 6
    return 7


def level_name(levelno: int) -> str:
    if levelno >= 50:
        return "CRITICAL"
    if levelno >= 40:
        return "ERROR"
    if levelno >= 30:
        return "WARNING"
    if levelno >= NOTICE:
        return "NOTICE"
    if levelno >= 20:
        return "INFO"
    return "DEBUG"


def level_from_name(name: Union[str, int]) -> int:
    if isinstance(name, int):
        return name
    s = name.strip().lower()
    if s.isdigit():
        return int(s)
    try:
        return _NAME_TO_LEVEL[s]
    except KeyError:
        raise ValueError(f"unknown level: {name!r}") from None


def pri(facility: int, sev: int) -> int:
    return facility * 8 + sev
