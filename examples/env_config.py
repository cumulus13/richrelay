"""Zero-config in code: destinations come from $RICHRELAY_URLS (see README, "Step 3").

    export RICHRELAY_URLS="tcp://10.0.0.5:514?format=rfc5424&buffer=redis%3A%2F%2Flocalhost%3A6379%2F0%3Fkey%3Dlogs%3Aspool"
    python examples/env_config.py
"""
import logging

from rich.console import Console

import richrelay

richrelay.setup(app_name="envapp", attach_logging=True)     # destinations come from $RICHRELAY_URLS
console = Console()
console.log("config loaded", {"env": "prod"})
try:
    _ = 1 / 0
except ZeroDivisionError:
    logging.getLogger("billing").exception("charge failed")
