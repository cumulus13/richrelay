import pytest

from richrelay.buffers import MemoryBuffer, SQLiteBuffer


@pytest.fixture(params=["memory", "sqlite"])
def make(request, tmp_path):
    counter = iter(range(1000))

    def factory(**kw):
        if request.param == "memory":
            return MemoryBuffer(**kw)
        kw.setdefault("max_bytes", None)
        return SQLiteBuffer(tmp_path / f"s{next(counter)}.db", **kw)
    return factory


def test_fifo_peek_discard(make):
    b = make(max_items=100)
    for i in range(5):
        b.push(f"m{i}".encode())
    assert b.peek(3) == [b"m0", b"m1", b"m2"]
    assert b.peek(3) == [b"m0", b"m1", b"m2"]  # idempotent until discard
    b.discard(2)
    assert b.peek(10) == [b"m2", b"m3", b"m4"] and len(b) == 3


def test_drop_oldest_and_newest(make):
    b = make(max_items=3)
    for i in range(5):
        b.push(f"m{i}".encode())
    assert b.peek(10) == [b"m2", b"m3", b"m4"] and b.dropped == 2
    n = make(max_items=2, overflow="drop_newest")
    for i in range(4):
        n.push(f"m{i}".encode())
    assert n.peek(10) == [b"m0", b"m1"] and n.dropped == 2


def test_byte_limit():
    b = MemoryBuffer(max_items=100, max_bytes=10)
    for _ in range(4):
        b.push(b"xxxx")
    assert len(b) == 2 and b.dropped == 2
    assert b.push(b"y" * 11) is False


def test_sqlite_persists_and_counts(tmp_path):
    p = tmp_path / "spool.db"
    b = SQLiteBuffer(p, max_bytes=None)
    for i in range(3):
        b.push(f"m{i}".encode())
    b.close()
    b2 = SQLiteBuffer(p, max_bytes=None)
    assert len(b2) == 3 and b2.peek(5) == [b"m0", b"m1", b"m2"]
    b2.discard(3)
    assert len(b2) == 0


def test_sqlite_byte_eviction(tmp_path):
    b = SQLiteBuffer(tmp_path / "s.db", max_items=1000, max_bytes=20)
    for _ in range(10):
        b.push(b"12345678")
    assert len(b) == 2 and b.dropped == 8
