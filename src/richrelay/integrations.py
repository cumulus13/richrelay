"""Hooks into logging, rich.print / Console.print / Console.log / print_exception, excepthooks."""

from __future__ import annotations

import io
import logging
import sys
import threading
import traceback
from typing import Any, Callable, Dict, Optional

from rich.console import Console
from rich.logging import RichHandler

from .event import Event
from .relay import Relay, format_exc_info, get_relay

_STD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message", "asctime", "taskName",
}


def event_from_record(record: logging.LogRecord, message: Optional[str] = None) -> Event:
    exc = format_exc_info(record.exc_info) if record.exc_info else record.exc_text
    if record.stack_info:
        exc = f"{exc}\n{record.stack_info}" if exc else record.stack_info
    extra = {k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS and not k.startswith("_")}
    extra.setdefault("file", f"{record.pathname}:{record.lineno}")
    extra.setdefault("func", record.funcName)
    return Event(
        message=message if message is not None else record.getMessage(),
        level=record.levelno, timestamp=record.created, logger=record.name,
        exc_text=exc, extra=extra, pid=record.process or 0, thread=record.threadName,
        source="logging",
    )


class RelayHandler(logging.Handler):
    """Plain ``logging`` handler that forwards records to a :class:`Relay`."""

    def __init__(self, relay: Optional[Relay] = None, level: int = logging.NOTSET,
                 use_formatter: bool = False) -> None:
        super().__init__(level)
        self._relay = relay
        self.use_formatter = use_formatter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            relay = self._relay or get_relay()
            if relay is None:
                return
            msg = None
            if self.use_formatter and self.formatter is not None:
                msg = self.format(record)
            relay.emit(event_from_record(record, msg))
        except Exception:
            self.handleError(record)


class RichRelayHandler(RichHandler):
    """``RichHandler`` that also relays every record (drop-in replacement)."""

    def __init__(self, *args: Any, relay: Optional[Relay] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._relay = relay

    def emit(self, record: logging.LogRecord) -> None:
        try:
            relay = self._relay or get_relay()
            if relay is not None:
                relay.emit(event_from_record(record))
        except Exception:
            self.handleError(record)
        super().emit(record)


# --------------------------------------------------------------- rich patching
_guard = threading.local()


def _caller(skip_rich: bool = True) -> Dict[str, Any]:
    f = sys._getframe(2)
    while f is not None:
        name = f.f_globals.get("__name__", "")
        if not (name == "rich" or name.startswith("rich.") or name.startswith("richrelay")):
            return {"file": f"{f.f_code.co_filename}:{f.f_lineno}", "func": f.f_code.co_name}
        f = f.f_back
    return {}


def _render_plain(console: Console, objects: Any, kwargs: Dict[str, Any], width: int) -> str:
    cap = Console(
        file=io.StringIO(), width=width, color_system=None, force_terminal=False,
        legacy_windows=False, markup=getattr(console, "_markup", True),
        emoji=getattr(console, "_emoji", True), highlight=False,
    )
    cap.print(*objects, **kwargs)
    text = cap.file.getvalue()  # type: ignore[attr-defined]
    return "\n".join(line.rstrip() for line in text.rstrip("\n").split("\n")).strip("\n")


class Patch:
    """Handle returned by :func:`patch_rich`; call :meth:`restore` or use as a context manager."""

    def __init__(self, originals: Dict[str, Any]) -> None:
        self._originals = originals

    def restore(self) -> None:
        for name, fn in self._originals.items():
            setattr(Console, name, fn)
        self._originals = {}

    def __enter__(self) -> "Patch":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.restore()


_active_patch: Optional[Patch] = None


def patch_rich(relay: Optional[Relay] = None, *, print_level: int = logging.INFO,
               log_level: int = logging.INFO, exception_level: int = logging.ERROR,
               capture_width: int = 200,
               console_filter: Optional[Callable[[Console], bool]] = None) -> Patch:
    """Relay everything rendered through ``rich.print``, ``Console.print``, ``Console.log``
    and ``Console.print_exception``. Output on the terminal is unchanged.
    """
    global _active_patch
    if _active_patch is not None:
        _active_patch.restore()
    orig_print, orig_log, orig_exc = Console.print, Console.log, Console.print_exception

    def target() -> Optional[Relay]:
        return relay or get_relay()

    def wanted(console: Console) -> bool:
        return (target() is not None) and (console_filter is None or console_filter(console))

    def send(console: Console, objects: Any, kwargs: Dict[str, Any], level: int, source: str,
             caller: Dict[str, Any]) -> None:
        r = target()
        if r is None:
            return
        _guard.active = True
        try:
            text = _render_plain(console, objects, kwargs, capture_width)
        except Exception:
            return
        finally:
            _guard.active = False
        if text.strip():
            r.emit(Event(message=text, level=level, logger=source, source=source, extra=caller))

    def new_print(self: Console, *objects: Any, **kwargs: Any) -> None:
        if getattr(_guard, "active", False):
            return orig_print(self, *objects, **kwargs)
        _guard.active = True
        try:
            orig_print(self, *objects, **kwargs)
        finally:
            _guard.active = False
        if wanted(self):
            send(self, objects, kwargs, print_level, "rich.print", _caller())

    def new_log(self: Console, *objects: Any, **kwargs: Any) -> None:
        if getattr(_guard, "active", False):
            return orig_log(self, *objects, **kwargs)
        kwargs["_stack_offset"] = kwargs.get("_stack_offset", 1) + 1  # keep file:line correct
        _guard.active = True
        try:
            orig_log(self, *objects, **kwargs)
        finally:
            _guard.active = False
        if wanted(self):
            cap_kw = {k: v for k, v in kwargs.items()
                      if k in ("sep", "end", "style", "justify", "emoji", "markup", "highlight")}
            send(self, objects, cap_kw, log_level, "rich.log", _caller())

    def new_exc(self: Console, *args: Any, **kwargs: Any) -> None:
        if getattr(_guard, "active", False):
            return orig_exc(self, *args, **kwargs)
        exc_info = sys.exc_info()
        _guard.active = True
        try:
            orig_exc(self, *args, **kwargs)
        finally:
            _guard.active = False
        r = target()
        if r is not None and exc_info[0] is not None and wanted(self):
            r.emit(Event(
                message=f"{exc_info[0].__name__}: {exc_info[1]}", level=exception_level,
                logger="rich.exception", source="rich.exception",
                exc_text=format_exc_info(exc_info), extra=_caller(),
            ))

    Console.print, Console.log, Console.print_exception = new_print, new_log, new_exc  # type: ignore
    _active_patch = Patch({"print": orig_print, "log": orig_log, "print_exception": orig_exc})
    return _active_patch


# ------------------------------------------------------------------ excepthook
class HookHandle:
    def __init__(self, prev_sys: Any, prev_thread: Any, hooks: Any) -> None:
        self._prev_sys, self._prev_thread, self._hooks = prev_sys, prev_thread, hooks

    def restore(self) -> None:
        if sys.excepthook is self._hooks[0]:
            sys.excepthook = self._prev_sys
        if threading.excepthook is self._hooks[1]:
            threading.excepthook = self._prev_thread


def install_excepthook(relay: Optional[Relay] = None, *, rich_traceback: bool = False,
                       flush_timeout: float = 3.0, **traceback_options: Any) -> HookHandle:
    """Relay uncaught exceptions (main thread and threads) at CRITICAL, then flush.

    Install *after* ``rich.traceback.install()`` (or pass ``rich_traceback=True``) so the
    original hook still runs first and the traceback is shown before we wait on the network.
    """
    if rich_traceback:
        from rich.traceback import install as rich_install

        rich_install(**traceback_options)
    prev_sys, prev_thread = sys.excepthook, threading.excepthook

    def report(exc_type: Any, exc: Any, tb: Any, where: str) -> None:
        r = relay or get_relay()
        if r is None or issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            return
        r.emit(Event(
            message=f"Unhandled exception{where}: {exc_type.__name__}: {exc}",
            level=logging.CRITICAL, logger="excepthook", source="excepthook",
            exc_text="".join(traceback.format_exception(exc_type, exc, tb)).rstrip("\n"),
        ))
        r.flush(flush_timeout)

    def sys_hook(exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            prev_sys(exc_type, exc, tb)
        finally:
            try:
                report(exc_type, exc, tb, "")
            except Exception:
                pass

    def thread_hook(args: Any) -> None:
        try:
            prev_thread(args)
        finally:
            try:
                name = args.thread.name if args.thread else "?"
                report(args.exc_type, args.exc_value, args.exc_traceback, f" in thread {name}")
            except Exception:
                pass

    sys.excepthook, threading.excepthook = sys_hook, thread_hook
    return HookHandle(prev_sys, prev_thread, (sys_hook, thread_hook))
