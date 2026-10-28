from __future__ import annotations

from typing import Any, List, Optional

from .base import Buffer, SpoolError, check_overflow


def _import_redis() -> Any:
    try:
        import redis
    except ImportError as exc:  # pragma: no cover
        raise ImportError("RedisBuffer needs the extra: pip install 'richrelay[redis]'") from exc
    return redis


class RedisBuffer(Buffer):
    """Store-and-forward queue backed by a Redis list (RPUSH / LRANGE / LTRIM).

    Bounded by ``max_items`` only (Redis lists have no byte limit).
    """

    def __init__(self, url: str = "redis://localhost:6379/0", *, key: str = "richrelay:spool",
                 max_items: int = 100_000, overflow: str = "drop_oldest",
                 client: Any = None, socket_timeout: float = 3.0) -> None:
        super().__init__()
        self.url, self.key, self.max_items = url, key, max_items
        self.overflow = check_overflow(overflow)
        self.socket_timeout = socket_timeout
        self._client = client
        self._owns_client = client is None  # never close a client the caller injected

    def _c(self) -> Any:
        if self._client is None:
            redis = _import_redis()
            self._client = redis.Redis.from_url(
                self.url, socket_timeout=self.socket_timeout,
                socket_connect_timeout=self.socket_timeout,
            )
        return self._client

    def _call(self, fn: Any) -> Any:
        try:
            return fn(self._c())
        except SpoolError:
            raise
        except Exception as exc:  # redis.RedisError, OSError
            raise SpoolError(f"redis {self.key}: {exc}") from exc

    def push(self, payload: bytes) -> bool:
        if self.overflow == "drop_newest":
            if self._call(lambda c: c.llen(self.key)) >= self.max_items:
                self.dropped += 1
                return False
            self.known_len = self._call(lambda c: c.rpush(self.key, payload))
            return True

        def op(c: Any) -> int:
            pipe = c.pipeline(transaction=True)
            pipe.rpush(self.key, payload)
            pipe.ltrim(self.key, -self.max_items, -1)
            return int(pipe.execute()[0])

        new_len = self._call(op)
        if new_len > self.max_items:
            self.dropped += new_len - self.max_items
            new_len = self.max_items
        self.known_len = new_len
        return True

    def peek(self, n: int) -> List[bytes]:
        return [bytes(x) for x in self._call(lambda c: c.lrange(self.key, 0, n - 1))]

    def discard(self, n: int) -> None:
        if n > 0:
            self._call(lambda c: c.ltrim(self.key, n, -1))
            self.known_len = max(0, self.known_len - n)

    def __len__(self) -> int:
        self.known_len = int(self._call(lambda c: c.llen(self.key)))
        return self.known_len

    def close(self) -> None:
        if not self._owns_client:
            return
        c, self._client = self._client, None
        try:
            if c is not None:
                c.close()
        except Exception:
            pass


class RedisCache:
    """Rolling cache of the most recent events (always written, delivery-independent).

    Keeps the newest ``max_items`` payloads in a list, optionally expiring after ``ttl`` seconds.
    Read them back with :meth:`recent`.
    """

    def __init__(self, url: str = "redis://localhost:6379/0", *, key: str = "richrelay:recent",
                 max_items: int = 1000, ttl: Optional[int] = None, format: str = "json",
                 client: Any = None, socket_timeout: float = 3.0) -> None:
        self.url, self.key, self.max_items, self.ttl = url, key, max_items, ttl
        self.format = format
        self.socket_timeout = socket_timeout
        self._client = client
        self._owns_client = client is None

    def _c(self) -> Any:
        if self._client is None:
            redis = _import_redis()
            self._client = redis.Redis.from_url(
                self.url, socket_timeout=self.socket_timeout,
                socket_connect_timeout=self.socket_timeout,
            )
        return self._client

    def put(self, payload: bytes) -> None:
        pipe = self._c().pipeline(transaction=True)
        pipe.lpush(self.key, payload)
        pipe.ltrim(self.key, 0, self.max_items - 1)
        if self.ttl:
            pipe.expire(self.key, int(self.ttl))
        pipe.execute()

    def recent(self, n: int = 100) -> List[str]:
        """Newest first."""
        return [bytes(x).decode("utf-8", "replace") for x in self._c().lrange(self.key, 0, n - 1)]

    def close(self) -> None:
        if not self._owns_client:
            return
        c, self._client = self._client, None
        try:
            if c is not None:
                c.close()
        except Exception:
            pass
