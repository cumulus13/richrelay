from __future__ import annotations

from typing import Any, List, Optional, Tuple

from .base import Buffer, SpoolError, check_overflow


class RabbitMQBuffer(Buffer):
    """Durable store-and-forward queue in RabbitMQ (pika BlockingConnection).

    The queue is declared durable with ``x-max-length``; messages are published persistent
    and only acknowledged once the real receiver has taken them. If the process dies
    mid-replay, unacknowledged messages are redelivered (at-least-once).

    Note: with ``drop_oldest`` the *broker* discards the head, so ``dropped`` is not tracked.
    Changing ``max_items``/``overflow`` for an existing queue requires deleting the queue
    (RabbitMQ rejects redeclaration with different arguments).
    """

    def __init__(self, url: str = "amqp://guest:guest@localhost:5672/%2F", *,
                 queue: str = "richrelay.spool", max_items: int = 100_000,
                 overflow: str = "drop_oldest", durable: bool = True) -> None:
        super().__init__()
        self.url, self.queue, self.max_items, self.durable = url, queue, max_items, durable
        self.overflow = check_overflow(overflow)
        try:  # import eagerly: importing inside the worker thread during exit is unreliable
            import pika  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise ImportError("RabbitMQBuffer needs: pip install 'richrelay[rabbitmq]'") from exc
        self._conn: Any = None
        self._ch: Any = None
        self._pending: List[Tuple[int, bytes]] = []  # (delivery_tag, body)

    def _connect(self) -> None:
        try:
            import pika
        except ImportError as exc:  # pragma: no cover
            raise ImportError("RabbitMQBuffer needs: pip install 'richrelay[rabbitmq]'") from exc
        params = pika.URLParameters(self.url)
        params.socket_timeout = 5
        params.blocked_connection_timeout = 10
        self._conn = pika.BlockingConnection(params)
        self._ch = self._conn.channel()
        self._ch.queue_declare(
            queue=self.queue, durable=self.durable,
            arguments={
                "x-max-length": self.max_items,
                "x-overflow": "drop-head" if self.overflow == "drop_oldest" else "reject-publish",
            },
        )
        self._ch.confirm_delivery()

    def _reset(self) -> None:
        self._pending = []  # unacked messages return to the queue when the connection closes
        try:
            if self._conn is not None and self._conn.is_open:
                self._conn.close()
        except Exception:
            pass
        self._conn = self._ch = None

    def _call(self, fn: Any) -> Any:
        last: Optional[BaseException] = None
        for _ in range(2):  # one transparent reconnect (idle connections get closed)
            try:
                if self._conn is None or self._conn.is_closed:
                    self._pending = []
                    self._connect()
                return fn(self._ch)
            except SpoolError:
                raise
            except Exception as exc:
                last = exc
                self._reset()
        raise SpoolError(f"rabbitmq {self.queue}: {last}") from last

    def push(self, payload: bytes) -> bool:
        import pika

        def op(ch: Any) -> bool:
            try:
                ch.basic_publish(
                    exchange="", routing_key=self.queue, body=payload,
                    properties=pika.BasicProperties(delivery_mode=2 if self.durable else 1),
                    mandatory=False,
                )
                return True
            except (pika.exceptions.NackError, pika.exceptions.UnroutableError):
                self.dropped += 1  # reject-publish: queue full
                return False

        ok = self._call(op)
        if ok:
            self.known_len += 1
        return bool(ok)

    def peek(self, n: int) -> List[bytes]:
        def op(ch: Any) -> List[bytes]:
            while len(self._pending) < n:
                method, _props, body = ch.basic_get(queue=self.queue, auto_ack=False)
                if method is None:
                    break
                self._pending.append((method.delivery_tag, bytes(body)))
            return [b for _t, b in self._pending[:n]]

        return self._call(op)

    def discard(self, n: int) -> None:
        if n <= 0 or not self._pending:
            return

        def op(ch: Any) -> None:
            k = min(n, len(self._pending))
            ch.basic_ack(delivery_tag=self._pending[k - 1][0], multiple=True)
            self._pending = self._pending[k:]
            self.known_len = max(0, self.known_len - k)

        # no retry-by-reconnect here: a reconnect invalidates delivery tags
        try:
            if self._ch is None:
                return
            op(self._ch)
        except Exception as exc:
            self._reset()
            raise SpoolError(f"rabbitmq {self.queue}: ack failed ({exc}); messages will be redelivered") from exc

    def __len__(self) -> int:
        def op(ch: Any) -> int:
            return int(ch.queue_declare(queue=self.queue, passive=True).method.message_count)

        try:
            ready = self._call(op)
        except SpoolError:
            raise
        self.known_len = ready + len(self._pending)
        return self.known_len

    def close(self) -> None:
        self._reset()
