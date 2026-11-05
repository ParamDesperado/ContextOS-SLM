"""Append-only audit log. Every record stores the SHA-256 of the previous one, so an
edit or deletion anywhere in the file breaks the chain and verify() reports it."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

GENESIS = "0" * 64


def _digest(record: dict) -> str:
    body = json.dumps({k: v for k, v in record.items() if k != "hash"}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._last = GENESIS
        for record in self.read():
            self._last = record["hash"]

    def read(self, last: int | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        return records[-last:] if last else records

    def append(self, actor: str, action: str, args: dict, decision: str, outcome: str = "") -> dict:
        with self._lock:
            record = {"ts": time.time(), "actor": actor, "action": action, "args": args,
                      "decision": decision, "outcome": outcome, "prev": self._last}
            record["hash"] = _digest(record)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
            self._last = record["hash"]
            return record

    def verify(self) -> tuple[bool, int]:
        """Return (intact, number of records checked before any break)."""
        prev, count = GENESIS, 0
        for record in self.read():
            if record.get("prev") != prev or _digest(record) != record.get("hash"):
                return False, count
            prev = record["hash"]
            count += 1
        return True, count
