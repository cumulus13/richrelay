import threading
import time

import pytest

from richrelay.listener import Receiver


class Collector:
    def __init__(self):
        self.items = []
        self.lock = threading.Lock()

    def __call__(self, data, peer, proto):
        with self.lock:
            self.items.append((data, proto))

    def wait(self, n, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                if len(self.items) >= n:
                    return True
            time.sleep(0.02)
        return False

    @property
    def texts(self):
        with self.lock:
            return [d.decode() for d, _ in self.items]


@pytest.fixture
def collector():
    return Collector()


@pytest.fixture
def make_receiver(collector):
    started = []

    def make(**kw):
        rx = Receiver(collector, **kw).start()
        started.append(rx)
        return rx

    yield make
    for rx in started:
        rx.stop()


class FakeRelay:
    """Duck-typed relay that just records events."""

    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)

    def flush(self, timeout=0):
        return True
