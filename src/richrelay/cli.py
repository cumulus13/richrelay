from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from ._version import __version__
from .relay import Relay
from .syslog import level_from_name
from .urls import parse_destination


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="richrelay", description="richrelay command line tools")
    ap.add_argument("--version", action="version", version=f"richrelay {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="send one message to one or more destination URLs")
    s.add_argument("-d", "--dest", action="append", required=True, metavar="URL")
    s.add_argument("-l", "--level", default="info")
    s.add_argument("--app", default="richrelay")
    s.add_argument("--facility", default="user")
    s.add_argument("message")

    d = sub.add_parser("drain", help="replay whatever is spooled for the destination URLs")
    d.add_argument("-d", "--dest", action="append", required=True, metavar="URL")
    d.add_argument("--timeout", type=float, default=30.0)

    ls = sub.add_parser("listen", help="run a small syslog receiver/viewer")
    ls.add_argument("--host", default="127.0.0.1")
    ls.add_argument("--udp", type=int, metavar="PORT")
    ls.add_argument("--tcp", type=int, metavar="PORT")
    ls.add_argument("--tls-cert")
    ls.add_argument("--tls-key")
    ls.add_argument("--json", action="store_true", dest="as_json")
    ls.add_argument("--raw", action="store_true")

    a = ap.parse_args(argv)
    if a.cmd == "listen":
        if a.udp is None and a.tcp is None:
            ap.error("give --udp and/or --tcp")
        from .listener import run_viewer

        run_viewer(a.host, a.udp, a.tcp, certfile=a.tls_cert, keyfile=a.tls_key,
                   as_json=a.as_json, raw=a.raw)
        return 0

    relay = Relay([parse_destination(u) for u in a.dest],
                  app_name=getattr(a, "app", None), facility=getattr(a, "facility", "user"))
    try:
        if a.cmd == "send":
            relay.log(level_from_name(a.level), a.message, source="cli")
            relay.flush()
            ok = True
        else:
            ok = relay.wait_drained(a.timeout)
        print(json.dumps(relay.stats(), indent=2, default=str))
        return 0 if ok else 1
    finally:
        relay.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
