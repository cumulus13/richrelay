"""richrelay - ship ``rich`` output, logging and exceptions to syslog and friends."""

from ._version import __version__
from .buffers import (
    Buffer,
    MemoryBuffer,
    RabbitMQBuffer,
    RedisBuffer,
    RedisCache,
    SpoolError,
    SQLiteBuffer,
)
from .event import Event
from .formats import FORMATS, Formatter, make_formatter
from .integrations import (
    RelayHandler,
    RichRelayHandler,
    event_from_record,
    install_excepthook,
    patch_rich,
)
from .relay import Destination, Relay, get_relay, setup, shutdown, tcp, tls, udp, unix
from .transports import TCPTransport, Transport, TransportError, UDPTransport, UnixTransport
from .urls import parse_buffer, parse_destination

__all__ = [
    "__version__", "Event", "Relay", "Destination", "setup", "shutdown", "get_relay",
    "udp", "tcp", "tls", "unix", "parse_destination", "parse_buffer",
    "RelayHandler", "RichRelayHandler", "patch_rich", "install_excepthook", "event_from_record",
    "Buffer", "MemoryBuffer", "SQLiteBuffer", "RedisBuffer", "RedisCache", "RabbitMQBuffer",
    "SpoolError", "Formatter", "FORMATS", "make_formatter",
    "Transport", "TransportError", "UDPTransport", "TCPTransport", "UnixTransport",
]  # fmt: skip
