"""Needs a real broker: set RICHRELAY_TEST_AMQP=amqp://guest:guest@localhost:5672/%2F"""

import os
import socket
import uuid

import pytest

from richrelay import RabbitMQBuffer, Relay, tcp
from richrelay.parsers import parse_message

URL = os.environ.get("RICHRELAY_TEST_AMQP")
pytestmark = pytest.mark.skipif(not URL, reason="RICHRELAY_TEST_AMQP not set")


@pytest.fixture
def queue():
    name = f"richrelay.test.{uuid.uuid4().hex[:8]}"
    yield name
    import pika
    conn = pika.BlockingConnection(pika.URLParameters(URL))
    conn.channel().queue_delete(queue=name)
    conn.close()


def test_fifo_ack_and_bounds(queue):
    b = RabbitMQBuffer(URL, queue=queue, max_items=3)
    for i in range(5):
        b.push(f"m{i}".encode())
    assert b.peek(10) == [b"m2", b"m3", b"m4"]  # broker drop-head
    assert b.peek(10) == [b"m2", b"m3", b"m4"]  # idempotent until discard
    b.discard(2)
    assert b.peek(10) == [b"m4"]
    b.close()  # unacked m4 goes back to the queue
    b2 = RabbitMQBuffer(URL, queue=queue, max_items=3)
    assert len(b2) == 1 and b2.peek(5) == [b"m4"]
    b2.close()


def test_reject_publish_counts_drops(queue):
    b = RabbitMQBuffer(URL, queue=queue, max_items=2, overflow="drop_newest")
    for i in range(4):
        b.push(f"m{i}".encode())
    assert b.dropped == 2 and b.peek(10) == [b"m0", b"m1"]
    b.close()


def test_queue_when_receiver_down(queue, collector):
    from richrelay.listener import Receiver
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    buf = RabbitMQBuffer(URL, queue=queue)
    r = Relay([tcp("127.0.0.1", port, buffer=buf, backoff_initial=0.05, backoff_max=0.2)], app_name="t")
    try:
        for i in range(10):
            r.info(f"q{i}")
        assert r.flush(10)
        rx = Receiver(collector, tcp_port=port).start()
        try:
            assert r.wait_drained(15) and collector.wait(10, 10)
        finally:
            rx.stop()
    finally:
        r.close()
    assert [parse_message(t).message for t in collector.texts] == [f"q{i}" for i in range(10)]
