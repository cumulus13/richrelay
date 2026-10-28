# richrelay

[![CI](https://github.com/cumulus13/richrelay/actions/workflows/ci.yml/badge.svg)](https://github.com/cumulus13/richrelay/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/richrelay.svg)](https://pypi.org/project/richrelay/)
[![Python](https://img.shields.io/pypi/pyversions/richrelay.svg)](https://pypi.org/project/richrelay/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**Ship [Rich](https://github.com/Textualize/rich) prints to syslog and other receivers, without losing logs when the receiver is down.**

`richrelay` is an extension for Rich, not a fork. Your terminal output stays exactly as it is; every
`rich.print`, `Console.print`, `Console.log`, `Console.print_exception`, `logging` record and uncaught
exception is *also* sent to one or more remote receivers, formatted as RFC 5424, RFC 3164, JSON, GELF,
logfmt or plain text.

- **Hooks:** `rich.print`, `Console.print`, `Console.log`, `Console.print_exception`, `RichHandler` (drop-in
  `RichRelayHandler`), any `logging` logger, `sys.excepthook`, `threading.excepthook`.
- **Formats:** RFC 5424 (with structured data), RFC 3164 (BSD), JSON, GELF 1.1, logfmt, plain.
- **Transports:** UDP, TCP (octet-counting / newline / NUL framing), TLS, Unix datagram (`/dev/log`).
- **Store-and-forward** while the receiver is down, with hard size limits:
  in-memory, **SQLite (durable, no extra dependency)**, **Redis** list, **RabbitMQ** queue.
- **Redis cache:** rolling list of the most recent events, independent of delivery.
- Multiple destinations at once, each with its own format, level filter and buffer.
- Non-blocking: your code only appends to an in-memory queue; a background thread does the I/O.
- Built-in receiver/viewer and CLI for testing (`richrelay listen`, `send`, `drain`).

> Why an extension? Rich has no log-shipping story and is not open to this kind of change right now.
> `richrelay` works purely by wrapping public Rich entry points, so it needs nothing from upstream.

## Install

```bash
pip install richrelay                 # core: SQLite + memory buffers, UDP/TCP/TLS
pip install "richrelay[redis]"        # + RedisBuffer / RedisCache
pip install "richrelay[rabbitmq]"     # + RabbitMQBuffer
pip install "richrelay[all]"
```

Python 3.9+, Rich 12+. Windows, Linux and macOS.

## Quick start

```python
import logging
import rich
from rich.console import Console
import richrelay

relay = richrelay.setup(
    [
        "udp://127.0.0.1:514?format=rfc3164",
        # TCP + durable SQLite spool: nothing is lost while 10.0.0.5 is down
        "tcp://10.0.0.5:514?format=rfc5424&buffer=sqlite%3A%2F%2F%2Fspool.db%3Fmax_mb%3D50",
    ],
    app_name="myapp",
    facility="local0",
    attach_logging=True,        # also forward the root logger
)

rich.print("[bold green]started[/bold green]")          # terminal + syslog
Console().log("loaded config", {"debug": True})          # terminal + syslog
logging.getLogger("db").error("connection lost")         # forwarded
try:
    1 / 0
except ZeroDivisionError:
    Console().print_exception()                          # full traceback relayed (ERROR)
relay.info("direct API", user="alice")                   # extra keys -> structured data
# uncaught exceptions are relayed at CRITICAL and flushed before the interpreter exits
```

Try it locally with the bundled viewer:

```bash
richrelay listen --udp 5514 --tcp 5514          # terminal 1: colored live viewer
richrelay send -d "udp://127.0.0.1:5514?format=rfc5424" -l warning "hello"   # terminal 2
```

`setup()` with no arguments reads whitespace-separated URLs from `$RICHRELAY_URLS`.

> New to the buffers? Redis/RabbitMQ are **not** used by default: see
> [Where do Redis and RabbitMQ fit?](#where-do-redis-and-rabbitmq-fit-read-this-first).

## Where do Redis and RabbitMQ fit? (read this first)

**By default, nothing goes to Redis or RabbitMQ.** When you call `Console().log(...)`, `rich.print(...)`,
`Console().print_exception()` or `logger.exception(...)`, richrelay sends the message **straight to your
receiver** (rsyslog, Graylog, Logstash, ...). Redis/RabbitMQ are a **waiting room**:

- A message is placed there **only when sending to the receiver fails** (receiver down, network cut).
- When the receiver is reachable again, the messages are taken out and delivered **in the original order**,
  and only then removed from Redis/RabbitMQ.
- You opt in by attaching a `buffer=` to a destination. Without a `buffer=`, failed messages are kept in a
  bounded in-memory list that is **lost when the process exits**.

So if you look into Redis or RabbitMQ while the receiver is healthy, they are empty. That is expected.

```mermaid
flowchart TD
    A["Console().log(...)<br/>rich.print(...)<br/>Console().print_exception()<br/>logger.exception(...)<br/>uncaught exception"] --> E(["Event<br/>(message + traceback)"])
    E --> Q{"Receiver reachable<br/>and buffer empty?"}
    Q -- "yes" --> RX["Receiver<br/>rsyslog / Graylog / ..."]
    Q -- "no (send failed)" --> BUF[("Buffer you configured<br/>Redis list / RabbitMQ queue / SQLite")]
    BUF -- "retry with backoff;<br/>when the receiver is back" --> RX
    RX -. "accepted: remove from buffer" .-> BUF
```

[![Where do Redis and RabbitMQ fit?](https://files.catbox.moe/v94qmd.jpg)](https://files.catbox.moe/v94qmd.jpg)

A destination has **one** buffer. For two independent queues use two destinations (each gets every event and
has its own receiver and its own buffer).

### Step 1: run Redis / RabbitMQ

Docker (file included in this repo):

```bash
docker compose up -d          # redis on :6379, rabbitmq on :5672, RabbitMQ UI on http://localhost:15672
```

Or natively (Debian/Ubuntu shown; macOS: `brew install redis rabbitmq`; on Windows use Docker or WSL):

```bash
sudo apt install redis-server rabbitmq-server
redis-cli ping                                   # -> PONG
sudo rabbitmqctl add_user richrelay change-me
sudo rabbitmqctl set_permissions -p / richrelay ".*" ".*" ".*"
```

The default RabbitMQ `guest` user can only log in from `localhost`; create your own user for anything else.
Redis with a password: `redis://:PASSWORD@host:6379/0`. TLS: `rediss://...` and `amqps://...` (standard
redis-py / pika URL forms; not covered by this project's tests).

### Step 2: install the client libraries

```bash
pip install "richrelay[redis]"       # RedisBuffer / RedisCache
pip install "richrelay[rabbitmq]"    # RabbitMQBuffer
```

A missing library fails immediately at construction with a clear message, not later in the background.

### Step 3: attach the buffer to a destination

Pick **one** of the three styles. They are equivalent.

**A. Python objects (clearest):**

```python
import richrelay
from richrelay import tcp, RedisBuffer, RabbitMQBuffer

richrelay.setup(
    [
        # RFC 5424 to rsyslog; while it is down, messages wait in a Redis list
        tcp("10.0.0.5", 514, format="rfc5424",
            buffer=RedisBuffer("redis://localhost:6379/0", key="logs:spool", max_items=200_000)),

        # JSON to Logstash; while it is down, messages wait in a durable RabbitMQ queue
        tcp("10.0.0.6", 5000, format="json",
            buffer=RabbitMQBuffer("amqp://richrelay:change-me@localhost:5672/%2F", queue="logs.spool")),
    ],
    app_name="myapp",
    attach_logging=True,          # also forward the logging module
)
```

**B. URL strings.** The buffer URL goes in the `buffer=` query value and must be URL-encoded
(`quote(url, safe="")`), because it contains `?`, `&` and `/`:

```python
from urllib.parse import quote
buf = quote("redis://localhost:6379/0?key=logs:spool&max_items=200000", safe="")
richrelay.setup([f"tcp://10.0.0.5:514?format=rfc5424&buffer={buf}"], app_name="myapp")
```

Buffer URL options: Redis `key`, `max_items`, `overflow`; RabbitMQ `queue`, `max_items`, `overflow`;
SQLite `sqlite:///path/spool.db?max_mb=50&max_items=100000`.

**C. Environment variable, nothing in code but `setup()`** (`examples/env_config.py`):

```bash
export RICHRELAY_URLS="tcp://10.0.0.5:514?format=rfc5424&buffer=redis%3A%2F%2Flocalhost%3A6379%2F0%3Fkey%3Dlogs%3Aspool"
```

```python
richrelay.setup(app_name="myapp", attach_logging=True)   # reads $RICHRELAY_URLS (space separated)
```

### Step 4: write your code as usual

Nothing here mentions Redis or RabbitMQ. The buffer is chosen once in `setup()`:

```python
import logging
from rich.console import Console

console = Console()
console.log("payment accepted", {"order": 1001})            # relayed, INFO
console.print("[bold]hello[/bold]")                          # relayed, INFO

try:
    1 / 0
except ZeroDivisionError:
    console.print_exception()                                # relayed with the full traceback, ERROR
    logging.getLogger("billing").exception("charge failed")  # relayed with the traceback, ERROR

raise RuntimeError("boom")                                   # uncaught: relayed (CRITICAL) before exit
```

Consoles created before or after `setup()` are both covered (the hook is on the `Console` class).
`from rich import print` also works. Use `richrelay.shutdown()` or let the `atexit` hook run; both store
anything still unsent into the configured buffer before the process ends.

### Step 5: see it work (receiver down)

Run the app above with the receiver stopped, then look inside the queues:

```console
$ redis-cli LLEN logs:spool
2
$ rabbitmqctl list_queues name messages
name         messages
logs.spool   2
```

A traceback is stored **inside the message**, after the first line. This is a real item from Redis
(RFC 5424):

```text
<11>1 2026-10-28T10:54:01.336830Z vm envapp 1960 - [richrelay@32473 logger="billing" source="logging" thread="MainThread" file="/tmp/env_app.py:10" func="<module>"] charge failed
Traceback (most recent call last):
  File "/tmp/env_app.py", line 8, in <module>
    1 / 0
    ~~^~~
ZeroDivisionError: division by zero
```

and the matching RabbitMQ message with `format="json"` (traceback in the `exception` field, shortened here):

```json
{"timestamp":"2026-10-28T10:54:01.336735Z","level":"INFO","severity":6,"app":"envapp","logger":"rich.log",
 "source":"rich.log","message":"config loaded\n{'env': 'prod'}","file":"/tmp/env_app.py:6","func":"<module>"}
```

Start the receiver (`richrelay listen --tcp 5514` or your real syslog server). The queues drain in order and
`LLEN` / `messages` go back to `0`. Everything above is runnable as
`python examples/store_and_forward_demo.py sqlite|redis|rabbitmq`.

| Where the traceback ends up | Format |
|---|---|
| After the first line of the message | `rfc5424`, `rfc3164`, `logfmt` (`exc=`), `plain` |
| `exception` field | `json` |
| `full_message` field | `gelf` |

### Troubleshooting

| Symptom | Cause / check |
|---|---|
| Redis/RabbitMQ stays empty | The receiver is up (expected), or the destination has no `buffer=`, or `setup()` was not called. Check `relay.stats()["destinations"][0]`: `state`, `buffered`, `last_error`. |
| `WARNING: N event(s) ... were lost at exit` on stderr | The buffer itself was unusable (Redis/RabbitMQ down) *and* the receiver was down. Messages were only in memory. Fix the buffer connection; see `last_error`. |
| `last_error: buffer: ... redis ...` | Wrong URL/password, or Redis unreachable. Delivery still works while the receiver is up. |
| `PRECONDITION_FAILED` (RabbitMQ) | The queue exists with different `max_items`/`overflow`. Delete the queue or use a new name. |
| RabbitMQ access / authentication error | The default `guest` user only works from localhost. Create your own user (Step 1) and put it in the URL. |
| Messages arrive twice after a crash | Replay is at-least-once: an item is removed only after the receiver accepted it. |

**Not supported in 0.1.0:** sending *every* message through Redis/RabbitMQ even while the receiver is healthy
(queue as the transport, with a separate consumer process shipping to syslog). The buffers are used for
store-and-forward only.

## Architecture

```mermaid
flowchart LR
    subgraph app["Your application"]
        A1["rich.print / Console.print"]
        A2["Console.log"]
        A3["Console.print_exception"]
        A4["logging / RichHandler"]
        A5["sys.excepthook / threading.excepthook"]
        A6["relay.info(...)"]
    end
    A1 & A2 & A3 --> P["patch_rich()<br/>plain-text capture"]
    A4 --> H["RelayHandler / RichRelayHandler"]
    A5 --> X["install_excepthook()"]
    P & H & X & A6 --> E(["Event"])
    E --> R{{"Relay<br/>fan-out"}}
    R --> C1["Channel 1<br/>format: RFC 5424"]
    R --> C2["Channel 2<br/>format: RFC 3164"]
    R --> C3["Channel 3<br/>format: JSON"]
    R -. "every event" .-> RC[("RedisCache<br/>recent N")]
    C1 --> T1["TCP / TLS"]
    C2 --> T2["UDP"]
    C3 --> T3["TCP"]
    C1 -. "receiver down" .-> B1[("SQLite spool")]
    C3 -. "receiver down" .-> B3[("Redis list or<br/>RabbitMQ queue")]
    T1 --> S1["rsyslog / syslog-ng / Graylog / ..."]
    T2 --> S2["any UDP receiver"]
    T3 --> S3["Logstash / Vector / custom"]
```

[![Architecture](https://files.catbox.moe/1cemxp.jpg)](https://files.catbox.moe/1cemxp.jpg)

Every destination is an independent **channel** (own thread, formatter, transport, buffer), so one dead
receiver never delays another.

### Store-and-forward

```mermaid
sequenceDiagram
    autonumber
    participant App as Your code
    participant Q as In-memory queue
    participant W as Channel worker
    participant RX as Receiver
    participant BUF as Buffer (SQLite / Redis / RabbitMQ)

    App->>Q: emit(event)  (non-blocking)
    Q->>W: next event
    W->>RX: send(payload)
    RX--xW: connection refused / reset
    W->>BUF: push(payload)
    Note over W: state = offline, retry with exponential backoff + jitter
    App->>Q: more events
    W->>BUF: push (keeps ordering)
    Note over RX: receiver comes back
    W->>BUF: peek(batch)
    W->>RX: send oldest first
    RX-->>W: ok
    W->>BUF: discard(batch)  (ack only after sending)
    Note over W: buffer empty, state = online, live sending resumes
```

[![Store-and-forward](https://files.catbox.moe/uaprl2.jpg)](https://files.catbox.moe/uaprl2.jpg)

### Channel state machine

```mermaid
stateDiagram-v2
    [*] --> Online
    Online --> Offline: send fails
    Offline --> Offline: retry fails and backoff doubles
    Offline --> Draining: retry timer fires
    Draining --> Offline: send fails again
    Draining --> Online: buffer empty
    Online --> [*]: close() flushes, then stores whatever is unsent
```

[![Channel state machine](https://files.catbox.moe/seo27a.jpg)](https://files.catbox.moe/seo27a.jpg)

If the *buffer itself* fails (Redis down, broker down, disk error), events fall back to a bounded
in-memory spill list and are still delivered as soon as the receiver is reachable.

## Hooking Rich

| What you call | What is relayed | Default level |
|---|---|---|
| `rich.print(...)`, `Console.print(...)`, `print_json`, `rule` | Rendered as plain text (no ANSI, 200 columns) | INFO |
| `Console.log(...)` | Plain text; `file`/`func` of the real caller added | INFO |
| `Console.print_exception()` | `Type: message` + full Python traceback | ERROR |
| `logging` (via `RelayHandler` / `RichRelayHandler`) | `record.getMessage()`, traceback, `extra=` fields | record level |
| Uncaught exception (main thread or thread) | Message + traceback, then flush | CRITICAL |

```python
richrelay.setup([...], print_level=logging.DEBUG, log_level=logging.INFO,
                exception_level=logging.ERROR, capture_width=120,
                console_filter=lambda console: not console.is_jupyter,
                rich_traceback=True)   # also calls rich.traceback.install()
```

Output that renders to nothing (for example `Live` / `Progress` control codes) is not relayed. Patching is
reversible: `with richrelay.patch_rich(relay): ...` or `richrelay.shutdown()`.

Use `RichRelayHandler` instead of `RichHandler`:

```python
logging.basicConfig(level="INFO", handlers=[RichRelayHandler(rich_tracebacks=True, relay=relay)])
```

## Formats

| `format=` | Wire format | TCP framing (default) |
|---|---|---|
| `rfc5424` (`5424`, `syslog`) | `<PRI>1 TIMESTAMP HOST APP PROCID MSGID [SD] MSG`, UTC microsecond timestamps | octet-counting (RFC 6587) |
| `rfc3164` (`3164`, `bsd`) | `<PRI>Mmm dd hh:mm:ss HOST TAG[PID]: MSG`, local time | newline (LF in message becomes `#012`) |
| `json` | one JSON object per message | newline |
| `gelf` | GELF 1.1 JSON (Graylog) | NUL-terminated |
| `logfmt` | `ts=... level=info msg="..."` | newline |
| `plain` | `timestamp LEVEL logger: message` | newline |

Override with `?framing=octet|newline|null|none`. RFC 5424 structured data uses the SD-ID
`richrelay@32473`; 32473 is the IANA number reserved for **documentation**. For production, use your own
Private Enterprise Number: `make_formatter("rfc5424", sd_id="myapp@<PEN>")`.

Facility is configurable per destination (`facility="local0"`), severity is derived from the level
(DEBUG 7, INFO 6, 25 = NOTICE 5, WARNING 4, ERROR 3, CRITICAL 2).

Messages are shrunk to `max_bytes` (UDP/Unix 8192, TCP 1 MiB; override per destination) by trimming the
traceback first, then the message, so JSON stays valid and the header intact.

## Destinations

```python
from richrelay import Relay, udp, tcp, tls, unix

Relay([
    udp("syslog.lan", 514, format="rfc3164", facility="daemon"),
    tcp("10.0.0.5", 514, format="rfc5424", min_level=30),
    tls("logs.example.com", 6514, ca_file="ca.pem", cert_file="client.pem", key_file="client.key"),
    unix("/dev/log"),                                   # POSIX only
])
```

URL form (also usable in `$RICHRELAY_URLS`):

```
udp://host:514?format=rfc3164&facility=local3&level=warning&max_bytes=4096
tcp://host:514?format=json&framing=newline&name=main
tls://host:6514?verify=1&ca=/etc/ssl/ca.pem&cert=client.pem&key=client.key
unix:///dev/log
...&buffer=<URL-encoded buffer URL>
```

## Buffers reference (what happens while the receiver is down)

| Buffer | Survives restart | Bound | Extra dependency | Use it for |
|---|---|---|---|---|
| `MemoryBuffer` (default spill) | no | items, bytes | none | short outages |
| `SQLiteBuffer` | **yes** | items, bytes | none (stdlib) | the non-Redis/RabbitMQ option |
| `RedisBuffer` | yes (per Redis persistence) | items | `redis` | shared queue, several producers |
| `RabbitMQBuffer` | yes (durable queue) | items (broker-side) | `pika` | existing RabbitMQ infrastructure |

```python
from richrelay import Relay, tcp, SQLiteBuffer, RedisBuffer, RabbitMQBuffer

tcp("10.0.0.5", 514, buffer=SQLiteBuffer("spool.db", max_items=100_000, max_bytes=64*1024*1024,
                                            overflow="drop_oldest"))
tcp("10.0.0.5", 514, buffer=RedisBuffer("redis://localhost:6379/0", key="logs:spool", max_items=200_000))
tcp("10.0.0.5", 514, buffer=RabbitMQBuffer("amqp://guest:guest@localhost/%2F", queue="logs.spool"))
```

- **Order** is preserved: buffered events are always replayed before new ones.
- **`overflow`**: `drop_oldest` (default, keep the newest) or `drop_newest` (keep the oldest). Dropped
  counts are in `relay.stats()` (not tracked for RabbitMQ `drop_oldest`, where the broker discards).
- **Backoff**: `backoff_initial=1.0` doubling to `backoff_max=60.0` seconds, with jitter.
- **Ack-after-send**: Redis/RabbitMQ/SQLite entries are removed only after the receiver accepted them.
  A crash mid-replay re-sends (at-least-once, so duplicates are possible).
- A message that can never be sent (e.g. a datagram over the path MTU limit) is dropped and counted in
  `dropped_permanent` instead of blocking the queue forever.
- RabbitMQ: the queue is declared with `x-max-length`/`x-overflow`; changing them for an existing queue
  needs the queue deleted first (RabbitMQ rejects redeclaring with different arguments).

### Redis as a cache

```python
from richrelay import Relay, RedisCache
cache = RedisCache("redis://localhost:6379/0", key="logs:recent", max_items=1000, ttl=3600)
relay = Relay([...], caches=[cache])
cache.recent(20)      # newest first, JSON strings
```

Cache writes are best effort and never retried; failures are counted in `relay.stats()["caches"]`.

## Delivery guarantees and limits (read this)

- **UDP is fire and forget.** The sender only learns the receiver is gone when an ICMP "port unreachable"
  comes back, which surfaces on a *later* send (Linux: `ECONNREFUSED`, Windows: `WSAECONNRESET`), and
  firewalls often drop ICMP entirely. Buffering works for UDP only on a best-effort basis. For lossless
  forwarding use TCP or TLS.
- **TCP/syslog has no application-level ack.** richrelay detects a peer that closed the connection
  before sending and reconnects, but bytes written just before an undetected failure may be lost. This is
  inherent to syslog over TCP, not specific to this library.
- **MemoryBuffer is lost on exit.** Use SQLite/Redis/RabbitMQ if that matters. On exit, `close()` makes a
  last delivery attempt; anything still undeliverable stays in the persistent buffer.
- **GELF** is sent uncompressed and **without UDP chunking**; keep GELF-over-UDP messages small or use TCP.
- **`close()` and slow networks**: `close(timeout)` waits at most `timeout` for the final delivery attempt. If the
  worker is still stuck connecting, it is told to stop and its transport is cancelled, so it never sends after
  `close()` returned and the next owner of the same spool will not receive duplicates from it.
- **SQLite**: use one process per spool file (size counters are kept in memory).
- **fork()**: worker threads do not survive a fork; create the relay after forking (e.g. in each
  gunicorn/multiprocessing worker).
- `richrelay` does not log through `logging` internally; internal errors are counted in `stats()` and a
  rate-limited line goes to stderr, so attaching `RelayHandler` to the root logger cannot loop.

## Operations

```python
relay.flush(5)          # everything handed over (sent or buffered)
relay.wait_drained(30)  # retry now, wait until buffers are empty
relay.stats()           # sent / buffered / replayed / dropped / state / last_error per destination
relay.close()           # also registered with atexit
```

CLI:

```bash
richrelay send  -d "tcp://host:514?buffer=sqlite%3A%2F%2F%2Ftmp%2Fs.db" -l error "disk full"
richrelay drain -d "tcp://host:514?buffer=sqlite%3A%2F%2F%2Ftmp%2Fs.db" --timeout 60   # exit 1 if not empty
richrelay listen --udp 5514 --tcp 5514 [--tls-cert c.pem --tls-key k.pem] [--json | --raw]
```

## Development

```bash
pip install -e ".[dev]"
ruff check . && pytest
```

CI runs on Linux/Windows/macOS for Python 3.9 to 3.13 and against real Redis and RabbitMQ services.
Releases: bump `src/richrelay/_version.py`, tag `vX.Y.Z`, and the **Publish to PyPI** workflow uploads
using PyPI Trusted Publishing (one-time setup is described at the top of `.github/workflows/publish.yml`).

## License

[MIT](LICENSE)

---

## 👤 Author
        
[Hadi Cahyadi](mailto:cumulus13@gmail.com)
    

[![Buy Me a Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/cumulus13)

[![Donate via Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/cumulus13)
 
[Support me on Patreon](https://www.patreon.com/cumulus13)
