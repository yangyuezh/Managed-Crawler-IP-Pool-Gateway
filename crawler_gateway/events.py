from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


class RotatingEventSink:
    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
    ) -> None:
        path = path.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(f"crawler_gateway.events.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(handler)

    def __call__(self, event: dict[str, Any]) -> None:
        payload = {
            "logged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **event,
        }
        self.logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))
