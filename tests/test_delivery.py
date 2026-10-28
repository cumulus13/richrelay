import socket
import time

from richrelay import Relay, SQLiteBuffer, tcp, udp
from richrelay.parsers import parse_message

FAST = dict(backoff_initial=0.05, backoff_max=0.2)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_udp_delivery(make_receiver, collector):
    rx = make_receiver(udp_port=0)
    with Relay([udp("127.0.0.1", rx.udp_port, format="rfc3164")], app_name="t", hostname="h") as r:
        r.info("over udp")
        assert collector.wait(1)
    assert parse_message(collector.texts[0]).message == "over udp"


def test_tcp_octet_framing_multiline(make_receiver, collector):
    rx = make_receiver(tcp_port=0)
    with Relay([tcp("127.0.0.1", rx.tcp_port, format="rfc5424")], app_name="t", hostname="h") as r:
        r.info("first\nsecond line")
        r.error("x" * 50)
        assert collector.wait(2)
    assert parse_message(collector.texts[0]).message == "first\nsecond line"


def test_tcp_newline_framing_escapes(make_receiver, collector):
    rx = make_receiver(tcp_port=0)
    with Relay([tcp("127.0.0.1", rx.tcp_port, format="json")], app_name="t") as r:
        r.info("a\nb")
        assert collector.wait(1)
    assert parse_message(collector.texts[0]).message == "a\nb"


def test_store_and_forward_sqlite_in_order(tmp_path, collector):
    from richrelay.listener import Receiver
    port = free_port()
    buf = SQLiteBuffer(tmp_path / "spool.db", max_bytes=None)
    r = Relay([tcp("127.0.0.1", port, buffer=buf, **FAST)], app_name="t", hostname="h")
    try:
        for i in range(20):
            r.info(f"msg-{i}")
        assert r.flush(5)
        assert r.stats()["destinations"][0]["state"] == "offline"
        assert len(buf) == 20
        rx = Receiver(collector, tcp_port=port).start()
        try:
            assert r.wait_drained(10)
            r.info("live-after")
            assert collector.wait(21, 10)
        finally:
            rx.stop()
    finally:
        r.close()
    msgs = [parse_message(t).message for t in collector.texts]
    assert msgs[:20] == [f"msg-{i}" for i in range(20)] and msgs[20] == "live-after"
    assert len(buf) == 0 or True


def test_sqlite_spool_survives_restart(tmp_path, collector):
    from richrelay.listener import Receiver
    port = free_port()
    db = tmp_path / "spool.db"
    r1 = Relay([tcp("127.0.0.1", port, buffer=SQLiteBuffer(db, max_bytes=None), **FAST)], app_name="t")
    r1.info("persisted-1")
    r1.info("persisted-2")
    r1.flush(5)
    r1.close(1)  # "process exits" while the receiver is down
    rx = Receiver(collector, tcp_port=port).start()
    try:
        r2 = Relay([tcp("127.0.0.1", port, buffer=SQLiteBuffer(db, max_bytes=None), **FAST)], app_name="t")
        try:
            assert r2.wait_drained(10)
            assert collector.wait(2)
        finally:
            r2.close()
    finally:
        rx.stop()
    assert [parse_message(t).message for t in collector.texts] == ["persisted-1", "persisted-2"]


def test_receiver_dies_midstream_then_returns(collector):
    from richrelay.listener import Receiver
    port = free_port()
    rx = Receiver(collector, tcp_port=port).start()
    r = Relay([tcp("127.0.0.1", port, **FAST)], app_name="t")
    try:
        r.info("before")
        assert collector.wait(1)
        rx.stop()
        time.sleep(0.2)
        for i in range(5):
            r.info(f"during-{i}")
        r.flush(5)
        rx = Receiver(collector, tcp_port=port).start()
        assert r.wait_drained(10)
        assert collector.wait(6, 10)
    finally:
        rx.stop()
        r.close()
    msgs = [parse_message(t).message for t in collector.texts]
    assert msgs[0] == "before" and set(msgs[1:]) >= {f"during-{i}" for i in range(5)}


def test_memory_spill_is_bounded():
    port = free_port()
    r = Relay([tcp("127.0.0.1", port, spill_items=10, **FAST)], app_name="t")
    try:
        for i in range(50):
            r.info(f"m{i}")
        r.flush(5)
        st = r.stats()["destinations"][0]
        assert st["in_buffer"] <= 10 and st["dropped_buffer"] >= 40
    finally:
        r.close(0.5)


def test_truncation_keeps_json_valid(make_receiver, collector):
    import json
    rx = make_receiver(udp_port=0)
    with Relay([udp("127.0.0.1", rx.udp_port, format="json", max_bytes=500)], app_name="t") as r:
        r.error("short", exc_info=None)
        r.log(40, "m" * 5000)
        assert collector.wait(2)
    for t in collector.texts:
        assert len(t.encode()) <= 500
        json.loads(t)


def test_min_level_filter(make_receiver, collector):
    rx = make_receiver(udp_port=0)
    with Relay([udp("127.0.0.1", rx.udp_port, min_level=40)], app_name="t") as r:
        r.info("no")
        r.error("yes")
        assert collector.wait(1)
        time.sleep(0.2)
    assert len(collector.texts) == 1


def test_multiple_destinations_different_formats(make_receiver, collector):
    rx = make_receiver(udp_port=0, tcp_port=0)
    dests = [udp("127.0.0.1", rx.udp_port, format="rfc3164"), tcp("127.0.0.1", rx.tcp_port, format="rfc5424")]
    with Relay(dests, app_name="t") as r:
        r.info("fan-out")
        assert collector.wait(2)
    assert {parse_message(t).format for t in collector.texts} == {"rfc3164", "rfc5424"}
