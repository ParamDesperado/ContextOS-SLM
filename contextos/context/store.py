"""SQLite time-series store for snapshots. It works as a bounded ring buffer on disk."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from ..config import RETENTION_S

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot(
    ts REAL PRIMARY KEY, cpu REAL, mem REAL, activity TEXT, foreground TEXT, body TEXT
);
CREATE INDEX IF NOT EXISTS snapshot_activity ON snapshot(activity);
"""


class ContextStore:
    def __init__(self, path: Path | str) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def add(self, snap) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO snapshot VALUES(?,?,?,?,?,?)",
                (snap.ts, snap.cpu_percent, snap.mem_percent, snap.activity,
                 snap.foreground, json.dumps(snap.to_dict())),
            )
            self._db.execute("DELETE FROM snapshot WHERE ts < ?", (time.time() - RETENTION_S,))

    def history(self, seconds: float = 300, limit: int = 300) -> list[dict]:
        cutoff = time.time() - seconds
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, cpu, mem, activity FROM snapshot WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        return [{"ts": r[0], "cpu": r[1], "mem": r[2], "activity": r[3]} for r in reversed(rows)]

    def activity_minutes(self, seconds: float = 3600) -> dict[str, int]:
        """Number of samples spent per activity in the window."""
        with self._lock:
            rows = self._db.execute(
                "SELECT activity, COUNT(*) FROM snapshot WHERE ts >= ? GROUP BY activity",
                (time.time() - seconds,),
            ).fetchall()
        return {activity: count for activity, count in rows}

    def close(self) -> None:
        with self._lock:
            self._db.close()
