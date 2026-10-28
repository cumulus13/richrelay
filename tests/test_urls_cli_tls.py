import shutil
import subprocess

import pytest

from richrelay import MemoryBuffer, Relay, SQLiteBuffer, parse_destination, tcp
from richrelay.cli import main
from richrelay.parsers import parse_message
from richrelay.transports import TCPTransport, UDPTransport


def test_parse_destination_variants(tmp_path):
    d = parse_destination("udp://10.0.0.5:5140?format=rfc3164&facility=local3&level=warning")
    assert isinstance(d.transport, UDPTransport) and d.format == "rfc3164"
    assert d.facility == "local3" and d.min_level == 30
    d = parse_destination("tls://h?verify=0&framing=newline")
    assert isinstance(d.transport, TCPTransport) and d.transport.port == 6514 and not d.transport.verify
    from urllib.parse import quote
    d = parse_destination("tcp://h:514?buffer=" + quote(f"sqlite:///{tmp_path}/x.db?max_mb=1&max_items=7"))
    assert isinstance(d.buffer, SQLiteBuffer) and d.buffer.max_items == 7
    assert isinstance(parse_destination("tcp://h?buffer=memory%3A%2F%2F%3Fmax_items%3D5").buffer, MemoryBuffer)
    with pytest.raises(ValueError):
        parse_destination("ftp://x")


def test_cli_send_and_listen_parse(make_receiver, collector, capsys):
    rx = make_receiver(udp_port=0)
    rc = main(["send", "-d", f"udp://127.0.0.1:{rx.udp_port}?format=rfc3164", "-l", "warning", "hi cli"])
    assert rc == 0 and collector.wait(1)
    p = parse_message(collector.texts[0])
    assert p.message == "hi cli" and p.severity == 4


def test_cli_drain_exit_code(tmp_path):
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    db = tmp_path / "s.db"
    buf = SQLiteBuffer(db, max_bytes=None); buf.push(b"<14>1 x"); buf.close()
    from urllib.parse import quote
    url = f"tcp://127.0.0.1:{port}?buffer={quote(f'sqlite:///{db}')}&framing=octet"
    assert main(["drain", "-d", url, "--timeout", "1"]) == 1


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not available")
def test_tls_delivery(tmp_path, make_receiver, collector):
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt),
         "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        check=True, capture_output=True)
    rx = make_receiver(tcp_port=0, certfile=str(crt), keyfile=str(key))
    with Relay([tcp("127.0.0.1", rx.tcp_port, tls=True, ca_file=str(crt), format="rfc5424")], app_name="t") as r:
        r.info("secure hello")
        assert collector.wait(1)
    assert parse_message(collector.texts[0]).message == "secure hello"
    # verification must fail against an unknown CA, and the event must be buffered, not lost
    with Relay([tcp("127.0.0.1", rx.tcp_port, tls=True, backoff_initial=0.05)], app_name="t") as r:
        r.info("untrusted")
        r.flush(5)
        st = r.stats()["destinations"][0]
        assert st["state"] == "offline" and st["in_buffer"] == 1 and "CERTIFICATE" in st["last_error"].upper()
