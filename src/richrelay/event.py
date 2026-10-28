"""The neutral event structure every integration produces."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Event:
    """A single log event, independent of wire format."""

    message: str
    level: int = logging.INFO
    timestamp: float = field(default_factory=time.time)
    logger: str = "root"
    exc_text: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)
    pid: int = field(default_factory=os.getpid)
    thread: Optional[str] = field(default_factory=lambda: threading.current_thread().name)
    source: str = "api"
    msgid: Optional[str] = None
