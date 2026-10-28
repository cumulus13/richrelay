"""Per-destination worker: format -> send -> (on failure) buffer -> replay in order."""

from __future__ import annotations

import dataclasses
import random
import sys
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from .buffers import Buffer, MemoryBuffer
from .event import Event
from .formats import Formatter
from .transports import Transport, TransportError

_MARK = "...[truncated]"


def _cut(text: str, remove_bytes: int) -> str:
    raw = text.encode("utf-8")
    keep = max(0, len(raw) - remove_bytes - len(_MARK))
    return raw[:keep].decode("utf-8", "ignore") + _MARK


class Channel:
    """One destination: transport + formatter + buffer, driven by a daemon thread."""

    def __init__(self, name: str, transport: Transport, formatter: Formatter,
                 buffer: Optional[Buffer] = None, *, min_level: int = 0,
                 queue_size: int = 10_000, max_bytes: Optional[int] = None,
                 spill_items: int = 10_000, batch_size: int = 200,
                 backoff_initial: float = 1.0, backoff_max: float = 60.0) -> None:
        self.name, self.transport, self.formatter = name, transport, formatter
        self.buffer = buffer
        self.spill = MemoryBuffer(max_items=spill_items)  # used if the buffer backend fails
        self.min_level = min_level
        self.queue_size = queue_size
        self.max_bytes = max_bytes or transport.default_max_bytes
        self.batch_size = batch_size
        self.backoff_initial, self.backoff_max = backoff_initial, backoff_max
        transport.resolve_framing(formatter.default_framing)

        self._q: Deque[Event] = deque()
        self._cv = threading.Condition()
        self._stop = False
        self._busy = False
        self._has_buffered = True  # verified at thread start
        self._next_retry = 0.0
        self._attempts = 0
        self._online = True
        self._deadline: Optional[float] = None
        self._last_error: Optional[str] = None
        self._warned_at = 0.0
        self.counters: Dict[str, int] = dict.fromkeys(
            ("submitted", "sent", "buffered", "replayed", "dropped_queue",
             "dropped_permanent", "errors"), 0)
        self._thread = threading.Thread(target=self._run, name=f"richrelay-{name}", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ public
    def submit(self, event: Event) -> bool:
        if event.level < self.min_level:
            return False
        with self._cv:
            if self._stop:
                return False
            if len(self._q) >= self.queue_size:
                self._q.popleft()
                self.counters["dropped_queue"] += 1
            self._q.append(event)
            self.counters["submitted"] += 1
            self._cv.notify_all()
        return True

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until every submitted event was sent *or* stored in the buffer."""
        end = time.monotonic() + timeout
        with self._cv:
            while self._q or self._busy:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(min(left, 0.1))
        return True

    def wait_drained(self, timeout: float = 30.0) -> bool:
        """Retry immediately and wait until the buffer is empty (receiver caught up)."""
        end = time.monotonic() + timeout
        with self._cv:
            self._next_retry = 0.0
            self._cv.notify_all()
            while self._q or self._busy or self._has_buffered:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(min(left, 0.05))
        return True

    def close(self, timeout: float = 5.0) -> None:
        with self._cv:
            self._stop = True
            self._deadline = time.monotonic() + timeout
            self._cv.notify_all()
        self._thread.join(timeout + 1.0)
        for obj in (self.transport, self.buffer):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass

    def stats(self) -> Dict[str, Any]:
        dropped_buf = (self.buffer.dropped if self.buffer else 0) + self.spill.dropped
        return {
            "destination": self.transport.describe(),
            "format": self.formatter.name,
            "state": "online" if self._online else "offline",
            **self.counters,
            "dropped_buffer": dropped_buf,
            "queued": len(self._q),
            "in_buffer": (self.buffer.known_len if self.buffer else 0) + self.spill.known_len,
            "last_error": self._last_error,
        }

    # ---------------------------------------------------------------- internals
    def _note_error(self, where: str, exc: BaseException) -> None:
        self.counters["errors"] += 1
        self._last_error = f"{where}: {exc}"
        now = time.monotonic()
        if where == "internal" and now - self._warned_at > 30:
            self._warned_at = now
            print(f"[richrelay:{self.name}] {self._last_error}", file=sys.stderr)

    def _schedule_retry(self) -> None:
        self._attempts += 1
        delay = min(self.backoff_max, self.backoff_initial * (2 ** (self._attempts - 1)))
        self._next_retry = time.monotonic() + delay * random.uniform(0.8, 1.2)
        self._online = False

    def _encode(self, event: Event) -> bytes:
        text = self.formatter.format(event)
        data = text.encode("utf-8", "replace")
        for _ in range(6):  # shrink exception/message text so JSON & headers stay intact
            if len(data) <= self.max_bytes:
                return data
            excess = len(data) - self.max_bytes + 16
            if event.exc_text and len(event.exc_text) > len(_MARK):
                event = dataclasses.replace(event, exc_text=_cut(event.exc_text, excess))
            elif len(event.message) > len(_MARK):
                event = dataclasses.replace(event, message=_cut(event.message, excess))
            else:
                break
            data = self.formatter.format(event).encode("utf-8", "replace")
        return data[: self.max_bytes].decode("utf-8", "ignore").encode("utf-8")

    def _store(self, payload: bytes) -> None:
        self.counters["buffered"] += 1
        if self.buffer is not None:
            try:
                self.buffer.push(payload)
                self._has_buffered = True
                return
            except Exception as exc:
                self._note_error("buffer", exc)
        self.spill.push(payload)
        self._has_buffered = True

    def _drain_source(self, src: Buffer) -> str:
        while True:
            try:
                batch = src.peek(self.batch_size)
            except Exception as exc:
                self._note_error("buffer", exc)
                return "source"
            if not batch:
                return "empty"
            done = 0
            failure: Optional[TransportError] = None
            for payload in batch:
                try:
                    self.transport.send(payload)
                except TransportError as exc:
                    if exc.permanent:  # can never succeed: drop instead of blocking forever
                        self.counters["dropped_permanent"] += 1
                        done += 1
                        continue
                    failure = exc
                    break
                done += 1
                self.counters["replayed"] += 1
            try:
                src.discard(done)
            except Exception as exc:
                self._note_error("buffer", exc)
            if failure is not None:
                self._note_error("transport", failure)
                return "transport"
            if self._deadline is not None and time.monotonic() > self._deadline:
                return "deadline"

    def _maybe_drain(self, force: bool = False) -> None:
        if not self._has_buffered:
            return
        if not force and time.monotonic() < self._next_retry:
            return
        sources: List[Buffer] = ([self.buffer] if self.buffer is not None else []) + [self.spill]
        result = "empty"
        for src in sources:
            status = self._drain_source(src)
            if status in ("transport", "deadline"):
                result = status
                break
            if status == "source":
                result = "source"
        if result == "empty":
            with self._cv:
                self._has_buffered = False
                self._attempts = 0
                self._online = True
                self._cv.notify_all()
        elif result in ("transport", "source"):
            self._schedule_retry()

    def _deliver(self, event: Event) -> None:
        try:
            payload = self._encode(event)
        except Exception as exc:
            self._note_error("internal", exc)
            return
        if self._has_buffered:  # keep ordering: older buffered items go first
            self._store(payload)
            return
        try:
            self.transport.send(payload)
            self.counters["sent"] += 1
        except TransportError as exc:
            if exc.permanent:
                self.counters["dropped_permanent"] += 1
                return
            self._note_error("transport", exc)
            self._store(payload)
            self._schedule_retry()

    def _wait_timeout(self) -> float:
        if self._has_buffered:
            return min(1.0, max(0.01, self._next_retry - time.monotonic()))
        return 1.0

    def _run(self) -> None:
        try:
            self._has_buffered = (self.buffer is not None and len(self.buffer) > 0) or False
        except Exception as exc:
            self._note_error("buffer", exc)
            self._has_buffered = True
        while True:
            with self._cv:
                if not self._q and not self._stop:
                    self._cv.wait(self._wait_timeout())
                event = self._q.popleft() if self._q else None
                if event is not None:
                    self._busy = True
                stopping = self._stop and event is None
            try:
                if event is None:
                    if stopping:
                        self._maybe_drain(force=True)
                        return
                    self._maybe_drain()
                    continue
                self._maybe_drain()
                self._deliver(event)
            except Exception as exc:  # never let the worker die
                self._note_error("internal", exc)
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()


class CacheChannel:
    """Feeds a cache (e.g. RedisCache) with every event. Best effort, no replay."""

    def __init__(self, name: str, cache: Any, formatter: Formatter, *, queue_size: int = 5000) -> None:
        self.name, self.cache, self.formatter = name, cache, formatter
        self._q: Deque[Event] = deque()
        self._cv = threading.Condition()
        self._stop = False
        self._busy = False
        self.queue_size = queue_size
        self.counters = dict.fromkeys(("submitted", "cached", "dropped_queue", "errors"), 0)
        self._last_error: Optional[str] = None
        self._thread = threading.Thread(target=self._run, name=f"richrelay-cache-{name}", daemon=True)
        self._thread.start()

    def submit(self, event: Event) -> bool:
        with self._cv:
            if self._stop:
                return False
            if len(self._q) >= self.queue_size:
                self._q.popleft()
                self.counters["dropped_queue"] += 1
            self._q.append(event)
            self.counters["submitted"] += 1
            self._cv.notify_all()
        return True

    def flush(self, timeout: float = 5.0) -> bool:
        end = time.monotonic() + timeout
        with self._cv:
            while self._q or self._busy:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(min(left, 0.1))
        return True

    def close(self, timeout: float = 5.0) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        self._thread.join(timeout)
        try:
            self.cache.close()
        except Exception:
            pass

    def stats(self) -> Dict[str, Any]:
        return {"cache": self.name, **self.counters, "queued": len(self._q),
                "last_error": self._last_error}

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._q and not self._stop:
                    self._cv.wait(1.0)
                if not self._q:
                    return
                event = self._q.popleft()
                self._busy = True
            try:
                self.cache.put(self.formatter.format(event).encode("utf-8", "replace"))
                self.counters["cached"] += 1
            except Exception as exc:
                self.counters["errors"] += 1
                self._last_error = str(exc)
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()
