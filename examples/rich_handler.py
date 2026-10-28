import logging

from rich.console import Console

from richrelay import Relay, RichRelayHandler, tcp

relay = Relay([tcp("127.0.0.1", 5514, format="json")], app_name="svc")
logging.basicConfig(level="INFO", handlers=[RichRelayHandler(console=Console(), relay=relay, rich_tracebacks=True)])
logging.getLogger("svc").info("pretty on screen, JSON on the wire")
relay.flush()
