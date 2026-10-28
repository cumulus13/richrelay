"""Redis/RabbitMQ as the queue that holds logs while the receiver is down, Redis as a cache."""
from richrelay import RabbitMQBuffer, RedisBuffer, RedisCache, Relay, tcp, udp

relay = Relay(
    [
        # queue in Redis (list), replayed in order when 10.0.0.5:6514 is reachable again
        tcp("10.0.0.5", 6514, tls=True, format="rfc5424",
            buffer=RedisBuffer("redis://localhost:6379/0", key="logs:spool", max_items=200_000)),
        # durable queue in RabbitMQ; messages are acked only after the receiver took them
        tcp("10.0.0.6", 514, format="rfc5424",
            buffer=RabbitMQBuffer("amqp://guest:guest@localhost:5672/%2F", queue="logs.spool")),
        udp("10.0.0.7", 514, format="rfc3164"),
    ],
    app_name="svc",
    caches=[RedisCache("redis://localhost:6379/0", key="logs:recent", max_items=1000, ttl=3600)],
)
relay.info("hello")
print(relay.stats())
print(RedisCache("redis://localhost:6379/0", key="logs:recent").recent(5))
