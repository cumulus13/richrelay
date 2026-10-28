# Contributing

```bash
pip install -e ".[dev]"
ruff check . && pytest
```

Redis / RabbitMQ tests run against fakeredis by default. To use real servers:

```bash
RICHRELAY_TEST_REDIS=redis://127.0.0.1:6379/0 pytest tests/test_redis.py
RICHRELAY_TEST_AMQP='amqp://guest:guest@127.0.0.1:5672/%2F' pytest tests/test_rabbitmq.py
```

Pull requests are welcome. Please add a test for every behavior change.
