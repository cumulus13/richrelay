"""Runnable demo: the receiver is DOWN while the app logs, then comes back.

    python examples/store_and_forward_demo.py sqlite
    python examples/store_and_forward_demo.py redis    [--redis-url redis://localhost:6379/0]
    python examples/store_and_forward_demo.py rabbitmq [--amqp-url amqp://guest:guest@localhost:5672/%2F]

What you will see:
  1. app code uses rich.print / Console.log / print_exception / logging as usual
  2. the receiver is not running, so events pile up in the chosen buffer (count printed)
  3. a receiver is started, the buffer drains in order, the received messages are printed

Status lines use plain print() on purpose: Console.print is patched and would be relayed too.
"""

import argparse
import logging
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path

import rich
from rich.console import Console

import richrelay
from richrelay.listener import Receiver
from richrelay.parsers import parse_message

p = argparse.ArgumentParser()
p.add_argument("buffer", choices=["sqlite", "redis", "rabbitmq"])
p.add_argument("--redis-url", default="redis://localhost:6379/0")
p.add_argument("--amqp-url", default="amqp://guest:guest@localhost:5672/%2F")
args = p.parse_args()

name = f"demo-{uuid.uuid4().hex[:6]}"          # unique key/queue so reruns never collide
with socket.socket() as s:                      # a free port, with NOTHING listening on it yet
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]

# --- 1. choose the buffer -------------------------------------------------------------------------
if args.buffer == "sqlite":
    buffer = richrelay.SQLiteBuffer(Path(tempfile.gettempdir()) / f"{name}.db")

    def server_count():
        return len(richrelay.SQLiteBuffer(Path(tempfile.gettempdir()) / f"{name}.db"))
elif args.buffer == "redis":
    buffer = richrelay.RedisBuffer(args.redis_url, key=f"richrelay:{name}")

    def server_count():  # independent connection (never share the worker's connection)
        import redis
        return redis.Redis.from_url(args.redis_url).llen(f"richrelay:{name}")
else:
    buffer = richrelay.RabbitMQBuffer(args.amqp_url, queue=f"richrelay.{name}")

    def server_count():  # independent connection: pika connections are not thread-safe
        import pika
        conn = pika.BlockingConnection(pika.URLParameters(args.amqp_url))
        try:
            return conn.channel().queue_declare(queue=f"richrelay.{name}", passive=True).method.message_count
        finally:
            conn.close()

# --- 2. one-time setup: hooks rich.print / Console.log / print_exception / logging / excepthook ----
relay = richrelay.setup(
    [richrelay.tcp("127.0.0.1", port, format="rfc5424", buffer=buffer,
                   backoff_initial=0.2, backoff_max=1.0)],   # short backoff only to keep the demo fast
    app_name="demo", facility="local0", attach_logging="demo",
)

# --- 3. normal application code (this is all you write) -------------------------------------------
console = Console()
rich.print("[bold green]1. rich.print[/bold green] service starting")
console.log("2. Console.log", {"workers": 4, "debug": False})
logging.getLogger("demo.db").warning("3. logging warning", extra={"request_id": "r-42"})
try:
    {}["missing-key"]
except KeyError:
    console.print_exception()                    # 4. traceback is printed AND relayed (ERROR)
try:
    int("not a number")
except ValueError:
    logging.getLogger("demo").exception("5. logging.exception")   # traceback relayed too
relay.info("6. direct API", user="alice")

# --- 4. receiver is down: events are in the buffer ------------------------------------------------
relay.flush(10)
st = relay.stats()["destinations"][0]
print(f"\n[receiver DOWN] state={st['state']}  waiting in {args.buffer} = {server_count()}"
      f"  last_error={st['last_error']}")

# --- 5. receiver comes back: buffer drains in order -----------------------------------------------
got, lock = [], threading.Lock()


def on_message(data, peer, proto):
    with lock:
        got.append(parse_message(data))


rx = Receiver(on_message, tcp_port=port).start()
print(f"[receiver UP on 127.0.0.1:{port}] draining ...")
ok = relay.wait_drained(20)
time.sleep(0.3)
st = relay.stats()["destinations"][0]
print(f"drained={ok}  state={st['state']}  replayed={st['replayed']}  waiting in {args.buffer} = {server_count()}\n")
with lock:
    for m in got:
        first = m.message.splitlines()[0]
        extra = f"  (+{len(m.message.splitlines()) - 1} more lines)" if "\n" in m.message else ""
        print(f"  {m.severity_name:<7} {m.app}[{m.procid}] {first}{extra}")

rx.stop()
richrelay.shutdown()
if args.buffer == "sqlite":
    (Path(tempfile.gettempdir()) / f"{name}.db").unlink(missing_ok=True)
elif args.buffer == "redis":
    import redis
    redis.Redis.from_url(args.redis_url).delete(f"richrelay:{name}")
else:
    import pika
    c = pika.BlockingConnection(pika.URLParameters(args.amqp_url))
    c.channel().queue_delete(queue=f"richrelay.{name}")
    c.close()
