"""One call hooks rich.print, Console.log, print_exception, logging and uncaught exceptions."""
import logging

import rich
from rich.console import Console

import richrelay

relay = richrelay.setup(
    [
        "udp://127.0.0.1:5514?format=rfc3164",
        # durable spool: survives restarts, replayed in order when the receiver is back
        "tcp://127.0.0.1:5514?format=rfc5424&buffer=sqlite%3A%2F%2F%2Fspool.db%3Fmax_mb%3D50",
    ],
    app_name="demo", facility="local0", attach_logging=True,
)
console = Console()

rich.print("[bold green]hello[/bold green] from rich.print")
console.log("a Console.log line", {"answer": 42})
logging.getLogger("demo").warning("a logging record", extra={"request_id": "abc123"})
try:
    _ = 1 / 0
except ZeroDivisionError:
    console.print_exception()          # relayed at ERROR with the full traceback
relay.info("direct API", user="alice")  # extra keys become RFC 5424 structured data
# run `richrelay listen --udp 5514 --tcp 5514` in another terminal to watch
