"""Reference monitor between the language model and the operating system.

The model's output is treated as untrusted input. It can only ask for a registered
tool, and every request passes through this policy before anything executes.
"""
from __future__ import annotations

import enum
import secrets
import threading
import time
from dataclasses import dataclass


class Risk(enum.IntEnum):
    READ = 0      # observes state only
    ADJUST = 1    # changes state, and can be undone
    DANGEROUS = 2 # cannot be undone


class Decision(str, enum.Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


# Stopping or demoting these can destabilise or crash the machine.
PROTECTED = {
    "system", "registry", "smss", "csrss", "wininit", "winlogon", "services", "lsass", "svchost",
    "dwm", "fontdrvhost", "explorer", "memcompression", "secure system", "spoolsv", "msmpeng",
    "securityhealthservice", "audiodg", "ctfmon", "sihost", "taskhostw", "conhost",
    "init", "systemd", "kthreadd", "dbus-daemon", "xorg", "gnome-shell", "sshd",
}


def normalize(name: str) -> str:
    return name.lower().strip().removesuffix(".exe")


def is_protected(name: str) -> bool:
    return normalize(name) in PROTECTED


@dataclass
class Verdict:
    decision: Decision
    reason: str


def evaluate(tool, args: dict) -> Verdict:
    """Decide what happens to a validated tool call."""
    target = args.get("process")
    if target and tool.risk >= Risk.ADJUST and is_protected(str(target)):
        return Verdict(Decision.DENY, f"'{target}' is a protected system process")
    if tool.risk == Risk.DANGEROUS:
        return Verdict(Decision.CONFIRM, f"{tool.name} cannot be undone, so it needs confirmation")
    return Verdict(Decision.ALLOW, f"risk level {tool.risk.name} is allowed")


class ConfirmationQueue:
    """One-time, expiring tokens. A dangerous action runs only if the human returns the token."""

    TTL_S = 120

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[float, str, dict]] = {}

    def create(self, tool_name: str, args: dict) -> str:
        token = secrets.token_urlsafe(12)
        with self._lock:
            self._pending[token] = (time.time() + self.TTL_S, tool_name, args)
        return token

    def consume(self, token: str) -> tuple[str, dict] | None:
        with self._lock:
            item = self._pending.pop(token, None)
        if item is None or item[0] < time.time():
            return None
        return item[1], item[2]

    def cancel(self, token: str) -> bool:
        with self._lock:
            return self._pending.pop(token, None) is not None
