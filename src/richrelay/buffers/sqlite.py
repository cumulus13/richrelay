from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import List, Optional, Union

from .base import Buffer, SpoolError, check_overflow


class SQLiteBuffer(Buffer):
    """Durable on-disk spool using only the standard library.

    Survives process restarts, bounded by ``max_items`` and ``max_bytes``.
    Use one process per database file (counters are kept in memory).
    """

    def __init__(self, path: Optional[Union[str, Path]] = None, *, name: str = "default",
                 max_items: int = 100_000, max_bytes: Optional[int] = 64 * 1024 * 1024,
                 overflow: str = "drop_oldest") -> None:
        super().__init__()
        self.path = Path(path) if path else Path.home() / ".richrelay" / f"{name}.db"
        self.max_items, self.max_bytes = max_items, max_bytes
        self.overflow = check_overflow(overflow)
        self._lock = threading.RLock()
        self._peek_ids: List[int] = []
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS spool("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, payload BLOB NOT NULL)"
            )
            self._db.commit()
            row = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(payload)), 0) FROM spool"
            ).fetchone()
        except (sqlite3.Error, OSError) as exc:
            raise SpoolError(f"sqlite spool {self.path}: {exc}") from exc
        self._count, self._bytes = int(row[0]), int(row[1])
        self.known_len = self._count

    def _over(self) -> bool:
        return self._count > self.max_items or (
            self.max_bytes is not None and self._bytes > self.max_bytes
        )

    def push(self, payload: bytes) -> bool:
        size = len(payload)
        with self._lock:
            try:
                if self.max_bytes is not None and size > self.max_bytes:
                    self.dropped += 1
                    return False
                if self.overflow == "drop_newest" and (
                    self._count >= self.max_items
                    or (self.max_bytes is not None and self._bytes + size > self.max_bytes)
                ):
                    self.dropped += 1
                    return False
                self._db.execute("INSERT INTO spool(payload) VALUES (?)", (payload,))
                self._count += 1
                self._bytes += size
                while self._over():
                    rows = self._db.execute(
                        "SELECT id, LENGTH(payload) FROM spool ORDER BY id LIMIT 64"
                    ).fetchall()
                    last = None
                    for rid, ln in rows:
                        if not self._over():
                            break
                        last = rid
                        self._count -= 1
                        self._bytes -= ln
                        self.dropped += 1
                    if last is None:
                        break
                    self._db.execute("DELETE FROM spool WHERE id <= ?", (last,))
                self._db.commit()
            except sqlite3.Error as exc:
                raise SpoolError(f"sqlite spool {self.path}: {exc}") from exc
            self.known_len = self._count
            return True

    def peek(self, n: int) -> List[bytes]:
        with self._lock:
            try:
                rows = self._db.execute(
                    "SELECT id, payload FROM spool ORDER BY id LIMIT ?", (n,)
                ).fetchall()
            except sqlite3.Error as exc:
                raise SpoolError(f"sqlite spool {self.path}: {exc}") from exc
            self._peek_ids = [r[0] for r in rows]
            return [bytes(r[1]) for r in rows]

    def discard(self, n: int) -> None:
        with self._lock:
            ids = self._peek_ids[:n]
            if not ids:
                return
            last = ids[-1]
            try:
                cnt, size = self._db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(LENGTH(payload)), 0) FROM spool WHERE id <= ?",
                    (last,),
                ).fetchone()
                self._db.execute("DELETE FROM spool WHERE id <= ?", (last,))
                self._db.commit()
            except sqlite3.Error as exc:
                raise SpoolError(f"sqlite spool {self.path}: {exc}") from exc
            self._count -= int(cnt)
            self._bytes -= int(size)
            self._peek_ids = self._peek_ids[n:]
            self.known_len = self._count

    def __len__(self) -> int:
        return self._count

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass
