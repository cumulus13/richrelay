from __future__ import annotations

import threading
from collections import deque
from typing import Deque, List, Optional

from .base import Buffer, check_overflow


class MemoryBuffer(Buffer):
    """Bounded in-process buffer. Lost when the process exits."""

    def __init__(self, max_items: int = 10_000, max_bytes: Optional[int] = None,
                 overflow: str = "drop_oldest") -> None:
        super().__init__()
        self.max_items, self.max_bytes = max_items, max_bytes
        self.overflow = check_overflow(overflow)
        self._d: Deque[bytes] = deque()
        self._bytes = 0
        self._lock = threading.Lock()

    def _over(self) -> bool:
        return len(self._d) > self.max_items or (
            self.max_bytes is not None and self._bytes > self.max_bytes
        )

    def push(self, payload: bytes) -> bool:
        with self._lock:
            if self.max_bytes is not None and len(payload) > self.max_bytes:
                self.dropped += 1
                return False
            if self.overflow == "drop_newest":
                if len(self._d) >= self.max_items or (
                    self.max_bytes is not None and self._bytes + len(payload) > self.max_bytes
                ):
                    self.dropped += 1
                    return False
            self._d.append(payload)
            self._bytes += len(payload)
            while self._over() and self._d:
                self._bytes -= len(self._d.popleft())
                self.dropped += 1
            self.known_len = len(self._d)
            return True

    def peek(self, n: int) -> List[bytes]:
        with self._lock:
            out = []
            for i, item in enumerate(self._d):
                if i >= n:
                    break
                out.append(item)
            return out

    def discard(self, n: int) -> None:
        with self._lock:
            for _ in range(min(n, len(self._d))):
                self._bytes -= len(self._d.popleft())
            self.known_len = len(self._d)

    def __len__(self) -> int:
        return len(self._d)
