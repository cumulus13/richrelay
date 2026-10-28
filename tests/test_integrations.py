import io
import logging
import sys
import threading

import pytest
from rich.console import Console

import richrelay
from conftest import FakeRelay
from richrelay.integrations import RelayHandler, RichRelayHandler, install_excepthook, patch_rich


@pytest.fixture
def relay():
    return FakeRelay()


def test_rich_print_log_and_exception_are_relayed(relay):
    out = io.StringIO()
    console = Console(file=out, width=80)
    with patch_rich(relay):
        console.print("[bold]hello[/bold] [red]world[/red]")
        console.log("logged", 123)
        try:
            _ = 1 / 0
        except ZeroDivisionError:
            console.print_exception()
    assert "hello world" in out.getvalue()  # terminal output unchanged
    kinds = [(e.source, e.level) for e in relay.events]
    assert kinds == [("rich.print", 20), ("rich.log", 20), ("rich.exception", 40)]
    assert relay.events[0].message == "hello world"
    assert relay.events[1].message == "logged 123"
    assert "ZeroDivisionError" in relay.events[2].exc_text
    assert "test_integrations.py" in relay.events[0].extra["file"]


def test_log_shows_correct_caller_on_console(relay):
    out = io.StringIO()
    console = Console(file=out, width=100, force_terminal=False)
    with patch_rich(relay):
        console.log("where am i")
    assert "test_integrations.py" in out.getvalue()


def test_rich_dot_print_function(relay):
    import rich
    with patch_rich(relay):
        rich.print("[green]ok[/green]", {"a": 1})
    assert len(relay.events) == 1 and relay.events[0].message == "ok\n{'a': 1}"


def test_no_double_relay_and_restore(relay):
    orig = Console.print
    p = patch_rich(relay)
    assert Console.print is not orig
    Console(file=io.StringIO()).print_json('{"a": 1}')  # internally calls print
    assert len(relay.events) == 1
    p.restore()
    assert Console.print is orig


def test_empty_output_not_relayed(relay):
    with patch_rich(relay):
        Console(file=io.StringIO()).print("")
        Console(file=io.StringIO()).print("   ")
    assert relay.events == []


def test_progress_does_not_spam(relay):
    from rich.progress import Progress
    with patch_rich(relay):
        with Progress(console=Console(file=io.StringIO(), force_terminal=True)) as pg:
            t = pg.add_task("x", total=3)
            for _ in range(3):
                pg.advance(t)
    assert all(e.message.strip() for e in relay.events) and len(relay.events) <= 2


def test_relay_handler_and_rich_handler(relay):
    lg = logging.getLogger("t.handler")
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    h1 = RelayHandler(relay)
    h2 = RichRelayHandler(console=Console(file=io.StringIO()), relay=relay)
    lg.addHandler(h1)
    lg.addHandler(h2)
    try:
        lg.warning("careful %s", "now", extra={"request_id": "abc"})
        try:
            raise KeyError("k")
        except KeyError:
            lg.exception("failed")
    finally:
        lg.removeHandler(h1)
        lg.removeHandler(h2)
    assert len(relay.events) == 4
    e = relay.events[0]
    assert e.message == "careful now" and e.level == 30 and e.extra["request_id"] == "abc"
    assert "KeyError" in relay.events[2].exc_text and relay.events[2].level == 40


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_excepthook_reports_and_chains(relay, capsys):
    called = []
    prev = sys.excepthook
    sys.excepthook = lambda *a: called.append(a)
    handle = install_excepthook(relay)
    try:
        try:
            raise RuntimeError("fatal")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
        t = threading.Thread(target=lambda: 1 / 0)
        t.start()
        t.join()
    finally:
        handle.restore()
        sys.excepthook = prev
    assert called and called[0][0] is RuntimeError
    msgs = [e.message for e in relay.events]
    assert any("RuntimeError: fatal" in m for m in msgs)
    assert any("in thread" in m and "ZeroDivisionError" in m for m in msgs)
    assert all(e.level == 50 for e in relay.events)


def test_setup_end_to_end(make_receiver, collector):
    rx = make_receiver(udp_port=0)
    relay = richrelay.setup([f"udp://127.0.0.1:{rx.udp_port}?format=rfc5424"], app_name="e2e",
                            attach_logging="e2e.log")
    try:
        import rich
        rich.print("[bold]via rich[/bold]")
        logging.getLogger("e2e.log").error("via logging")
        relay.info("via api")
        assert collector.wait(3)
    finally:
        richrelay.shutdown()
    from richrelay.parsers import parse_message
    assert {parse_message(t).message for t in collector.texts} == {"via rich", "via logging", "via api"}
