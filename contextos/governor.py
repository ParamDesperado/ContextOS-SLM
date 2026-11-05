"""Context governor: the fast path of the system.

A deterministic feedback loop. When the CPU is contended and the user is active in a
foreground app, busy background processes are demoted. When the pressure ends, the
original priorities come back. The two thresholds and consecutive-sample counters
give hysteresis, which stops the governor oscillating when load hovers around a limit.

The language model is never consulted here: a control loop must react in milliseconds
and must keep working when the model is unloaded.
"""
from __future__ import annotations

import os
from typing import Callable

import psutil

from . import config
from .security.policy import is_protected
from .tools import PRIORITY, PriorityLedger

ACTIVE_STATES = {"coding", "browsing", "meeting", "media", "gaming", "documents", "other"}


class Governor:
    def __init__(self, ledger: PriorityLedger, audit=None,
                 demote: Callable[[int], None] | None = None) -> None:
        self.ledger = ledger
        self.audit = audit
        self.enabled = True
        self.engaged = False
        self._high = 0
        self._low = 0
        self._demote_override = demote

    def _demote(self, pid: int) -> None:
        if self._demote_override:
            self._demote_override(pid)
        else:
            self.ledger.demote(psutil.Process(pid), PRIORITY["below_normal"])

    def _log(self, action: str, args: dict) -> None:
        if self.audit:
            self.audit.append("governor", action, args, "allow", "done")

    def victims(self, snap: dict) -> list[dict]:
        """Busy background processes that may safely be demoted."""
        fg_name, fg_pid = snap.get("foreground"), snap.get("foreground_pid")
        out = []
        for row in snap["top_cpu"]:
            if (row["cpu"] >= config.GOV_MIN_PROC_CPU and row["name"] != fg_name
                    and row["pid"] not in (fg_pid, os.getpid()) and not is_protected(row["name"])):
                out.append(row)
        return out

    def step(self, snap: dict) -> dict | None:
        """Process one sample. Returns a description of any action taken."""
        if not self.enabled:
            return None
        cpu = snap["cpu_percent"]
        self._high = self._high + 1 if cpu >= config.GOV_CPU_HIGH else 0
        self._low = self._low + 1 if cpu <= config.GOV_CPU_LOW else 0

        if (self._high >= config.GOV_TRIGGER_SAMPLES and snap.get("activity") in ACTIVE_STATES):
            demoted = []
            already = self.ledger.pids()
            for row in self.victims(snap):
                if row["pid"] in already:
                    continue
                try:
                    self._demote(row["pid"])
                    demoted.append(row["name"])
                except (psutil.Error, OSError):
                    continue
            if demoted:
                self.engaged = True
                event = {"event": "demote", "processes": sorted(set(demoted)), "cpu": cpu,
                         "protecting": snap.get("foreground")}
                self._log("governor_demote", event)
                return event

        if self.engaged and self._low >= config.GOV_RELEASE_SAMPLES:
            restored = self.ledger.restore_all()
            self.engaged = False
            event = {"event": "restore", "processes": sorted(set(restored)), "cpu": cpu}
            self._log("governor_restore", event)
            return event
        return None

    def shutdown(self) -> None:
        self.ledger.restore_all()
        self.engaged = False
