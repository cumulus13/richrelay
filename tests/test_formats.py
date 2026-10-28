import json
import re

from richrelay.event import Event
from richrelay.formats import make_formatter
from richrelay.parsers import parse_message


def ev(**kw):
    kw.setdefault("message", "hello world")
    kw.setdefault("logger", "app.mod")
    return Event(**kw)


def test_rfc5424_roundtrip():
    f = make_formatter("rfc5424", hostname="host1", app_name="myapp", facility="local0")
    e = ev(level=40, timestamp=1_700_000_000.123456, pid=4242, extra={"user": 'a"b]c\\d'})
    text = f.format(e)
    assert text.startswith("<131>1 2023-11-14T22:13:20.123456Z host1 myapp 4242 - [richrelay@32473 ")
    p = parse_message(text)
    assert (p.format, p.facility, p.severity, p.app, p.procid) == ("rfc5424", 16, 3, "myapp", "4242")
    assert p.message == "hello world"
    assert p.structured_data["richrelay@32473"]["user"] == 'a"b]c\\d'


def test_rfc5424_multiline_and_exception():
    f = make_formatter("rfc5424", hostname="h", app_name="a")
    text = f.format(ev(message="boom", exc_text="Traceback...\nValueError: x"))
    assert parse_message(text).message == "boom\nTraceback...\nValueError: x"


def test_rfc5424_header_sanitising():
    f = make_formatter("rfc5424", hostname="my host", app_name="my app")
    text = f.format(ev())
    assert " my_host my_app " in text


def test_rfc3164_roundtrip():
    f = make_formatter("rfc3164", hostname="h1", app_name="svc", facility="daemon")
    text = f.format(ev(level=30, pid=7))
    assert re.match(r"^<28>[A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d h1 svc\[7\]: hello world$", text)
    p = parse_message(text)
    assert (p.format, p.facility, p.severity, p.app, p.procid) == ("rfc3164", 3, 4, "svc", "7")


def test_json_and_gelf():
    j = json.loads(make_formatter("json", hostname="h", app_name="a").format(ev(extra={"k": 1})))
    assert j["message"] == "hello world" and j["k"] == 1 and j["level"] == "INFO"
    g = json.loads(make_formatter("gelf", hostname="h", app_name="a").format(
        ev(message="line1\nline2", extra={"id": 5, "x": 2})))
    assert g["version"] == "1.1" and g["short_message"] == "line1" and g["_x"] == 2
    assert "_id" not in g and g["full_message"] == "line1\nline2"


def test_logfmt_plain_and_unknown():
    assert 'msg="hello world"' in make_formatter("logfmt", hostname="h", app_name="a").format(ev())
    assert "INFO" in make_formatter("plain").format(ev())
    import pytest
    with pytest.raises(ValueError):
        make_formatter("nope")


def test_severity_mapping():
    from richrelay.syslog import severity
    assert [severity(x) for x in (10, 20, 25, 30, 40, 50)] == [7, 6, 5, 4, 3, 2]
