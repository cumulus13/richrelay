# Changelog

## 0.1.0

- Initial release.
- Hooks: `rich.print`, `Console.print`, `Console.log`, `Console.print_exception`, `logging`
  (`RelayHandler`, `RichRelayHandler`), `sys.excepthook` / `threading.excepthook`.
- Formats: RFC 5424, RFC 3164, JSON, GELF 1.1, logfmt, plain.
- Transports: UDP, TCP (octet-counting / newline / NUL framing), TLS, Unix datagram.
- Store-and-forward buffers: memory, SQLite (durable), Redis list, RabbitMQ queue.
- `RedisCache` rolling cache of recent events.
- Bundled receiver / viewer and CLI (`richrelay send | drain | listen`).
