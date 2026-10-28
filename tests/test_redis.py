"""Redis tests: use $RICHRELAY_TEST_REDIS (real server) if set, else fakeredis, else skip."""

import os
import socket

import pytest

from richrelay import RedisBuffer, RedisCache, Relay, tcp
from richrelay.buffers import SpoolError
from richrelay.parsers import parse_message


def _client():
    url = os.environ.get("RICHRELAY_TEST_REDIS")
    if url:
        import redis
        c = redis.Redis.from_url(url)
        c.flushdb()
        return c, url
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.FakeRedis(), "redis://fake"


@pytest.fixture
def rc():
    return _client()


def test_redis_buffer_fifo_and_bounds(rc):
    client, url = rc
    b = RedisBuffer(url, key="t:q", max_items=3, client=client)
    for i in range(5):
        b.push(f"m{i}".encode())
    assert b.peek(10) == [b"m2", b"m3", b"m4"] and b.dropped == 2 and len(b) == 3
    b.discard(2)
    assert b.peek(10) == [b"m4"]
    n = RedisBuffer(url, key="t:n", max_items=2, overflow="drop_newest", client=client)
    for i in range(4):
        n.push(f"x{i}".encode())
    assert n.peek(10) == [b"x0", b"x1"] and n.dropped == 2


def test_redis_buffer_error_is_spoolerror():
    b = RedisBuffer("redis://127.0.0.1:1/0", socket_timeout=0.2)
    pytest.importorskip("redis")
    with pytest.raises(SpoolError):
        b.push(b"x")


def test_redis_cache_keeps_recent(rc):
    client, url = rc
    cache = RedisCache(url, key="t:recent", max_items=3, ttl=60, client=client)
    with Relay([], app_name="t", caches=[cache]) as r:
        for i in range(5):
            r.info(f"e{i}")
        assert r.flush(5)
    recent = [parse_message(x).message for x in cache.recent(10)]
    assert recent == ["e4", "e3", "e2"]  # newest first, capped
    assert 0 < client.ttl("t:recent") <= 60


def test_redis_as_queue_when_receiver_down(rc, collector):
    from richrelay.listener import Receiver
    client, url = rc
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    buf = RedisBuffer(url, key="t:spool", client=client)
    r = Relay([tcp("127.0.0.1", port, buffer=buf, backoff_initial=0.05, backoff_max=0.2, connect_timeout=0.3)], app_name="t")
    try:
        for i in range(10):
            r.info(f"q{i}")
        assert r.flush(5)
        assert client.llen("t:spool") == 10
        rx = Receiver(collector, tcp_port=port).start()
        try:
            assert r.wait_drained(10) and collector.wait(10, 10)
        finally:
            rx.stop()
    finally:
        r.close()
    assert client.llen("t:spool") == 0
    assert [parse_message(t).message for t in collector.texts] == [f"q{i}" for i in range(10)]


def test_redis_down_falls_back_to_memory_spill(collector):
    pytest.importorskip("redis")
    from richrelay.listener import Receiver
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    dead = RedisBuffer("redis://127.0.0.1:1/0", socket_timeout=0.2)
    r = Relay([tcp("127.0.0.1", port, buffer=dead, backoff_initial=0.05, backoff_max=0.2, connect_timeout=0.3)], app_name="t")
    try:
        r.info("a"); r.info("b")
        r.flush(5)
        rx = Receiver(collector, tcp_port=port).start()
        try:
            assert collector.wait(2, 10)
        finally:
            rx.stop()
    finally:
        r.close()
    assert sorted(parse_message(t).message for t in collector.texts) == ["a", "b"]
