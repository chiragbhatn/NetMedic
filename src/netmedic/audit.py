"""Append-only JSON Lines audit log of every tool call."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)

    def record(
        self,
        tool: str,
        arguments: dict,
        device: str | None,
        result_chars: int,
        approved: bool | None = None,
    ) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "arguments": arguments,
            "device": device,
            "result_chars": result_chars,
            "approved": approved,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
