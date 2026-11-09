"""Bounded on-disk outbox for frames that could not be sent while offline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_MAX_ENTRIES = 10_000


class Outbox:
    """Append-only JSON Lines spool; overflow drops the oldest entries and records a gap."""

    def __init__(self, path: Path, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self.path = path
        self.max_entries = max_entries
        self._entries: list[dict[str, Any]] = []
        self.gaps = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._entries = self._read()

    def _read(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                document = json.loads(line)
            except ValueError:
                continue
            if isinstance(document, dict):
                entries.append(document)
        start = max(0, len(entries) - self.max_entries)
        return entries[start:]

    def append(self, frame: dict[str, Any]) -> None:
        self._entries.append(dict(frame))
        if len(self._entries) > self.max_entries:
            dropped = len(self._entries) - self.max_entries
            self.gaps += dropped
            start = max(0, len(self._entries) - self.max_entries)
            self._entries = self._entries[start:]
        self._write()

    def drain(self) -> list[dict[str, Any]]:
        entries = list(self._entries)
        self._entries.clear()
        self._write()
        return entries

    def __len__(self) -> int:
        return len(self._entries)

    def _write(self) -> None:
        payload = "".join(
            json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
            for item in self._entries
        )
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self.path)


__all__ = ["DEFAULT_MAX_ENTRIES", "Outbox"]
