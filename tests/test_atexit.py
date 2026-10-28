"""Regression: events must reach the buffer when the process just exits (no flush/shutdown).

Found with a real Redis: redis-py registers an atexit hook when a client is created, so a client
created lazily inside the worker thread failed during exit and the events were silently lost.
"""

import os
import socket
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _run(buffer_expr, port):
    code = textwrap.dedent(f"""
        import logging
        from rich.console import Console
        import richrelay
        from richrelay import *
        richrelay.setup([tcp("127.0.0.1", {port}, buffer={buffer_expr})], app_name="exit-test",
                        attach_logging="x")
        Console().log("via console.log")
        logging.getLogger("x").error("via logging")
        try:
            {{}}["k"]
        except KeyError:
            Console().print_exception()
        # no flush(), no shutdown(): relies on the atexit handler
    """)
    env = dict(os.environ, PYTHONPATH=SRC)
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    return r.stderr.decode("utf-8", "replace")


def test_sqlite_buffer_survives_plain_exit(tmp_path):
    db = tmp_path / "spool.db"
    err = _run(f'SQLiteBuffer(r"{db}")', _free_port())
    assert "were lost" not in err
    from richrelay import SQLiteBuffer
    assert len(SQLiteBuffer(db)) == 3


def test_redis_buffer_survives_plain_exit():
    url = os.environ.get("RICHRELAY_TEST_REDIS")
    if not url:
        pytest.skip("RICHRELAY_TEST_REDIS not set")
    import redis
    key = f"richrelay:test:{uuid.uuid4().hex[:8]}"
    err = _run(f'RedisBuffer("{url}", key="{key}")', _free_port())
    c = redis.Redis.from_url(url)
    try:
        assert "were lost" not in err
        assert c.llen(key) == 3
    finally:
        c.delete(key)


def test_rabbitmq_buffer_survives_plain_exit():
    url = os.environ.get("RICHRELAY_TEST_AMQP")
    if not url:
        pytest.skip("RICHRELAY_TEST_AMQP not set")
    import pika
    q = f"richrelay.test.{uuid.uuid4().hex[:8]}"
    err = _run(f'RabbitMQBuffer("{url}", queue="{q}")', _free_port())
    conn = pika.BlockingConnection(pika.URLParameters(url))
    try:
        assert "were lost" not in err
        assert conn.channel().queue_declare(queue=q, passive=True).method.message_count == 3
    finally:
        conn.channel().queue_delete(queue=q)
        conn.close()


def test_redis_client_is_created_eagerly():
    pytest.importorskip("redis")
    from richrelay import RedisBuffer, RedisCache
    assert RedisBuffer("redis://127.0.0.1:1/0")._client is not None  # no network I/O at this point
    assert RedisCache("redis://127.0.0.1:1/0")._client is not None


def test_lost_events_are_reported_not_silent(tmp_path):
    """A buffer backend that is unusable at exit must produce a warning, not silent loss."""
    pytest.importorskip("redis")
    err = _run('RedisBuffer("redis://127.0.0.1:1/0", socket_timeout=0.2)', _free_port())
    assert "were lost at exit" in err
