"""Buffer interface used for store-and-forward while a receiver is down."""

from __future__ import annotations

from typing import List


class SpoolError(Exception):
    """The buffer backend itself is unavailable (Redis down, broker down, disk error...)."""


class Buffer:
    """FIFO of already-formatted payloads.

    Contract (single consumer, used from one worker thread):
    ``peek(n)`` returns up to ``n`` oldest payloads without removing them;
    ``discard(n)`` removes the first ``n`` of the last peek.
    """

    overflow = "drop_oldest"

    def __init__(self) -> None:
        self.dropped = 0
        self.known_len = 0  # cheap, possibly approximate size for stats

    def push(self, payload: bytes) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def peek(self, n: int) -> List[bytes]:  # pragma: no cover - interface
        raise NotImplementedError

    def discard(self, n: int) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def __len__(self) -> int:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        pass


def check_overflow(value: str) -> str:
    if value not in ("drop_oldest", "drop_newest"):
        raise ValueError("overflow must be 'drop_oldest' or 'drop_newest'")
    return value
