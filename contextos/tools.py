"""The system call table of the context layer: the only actions the model can request."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import psutil

from .security.policy import Risk, is_protected, normalize

IS_WIN = sys.platform == "win32"

# Priority levels mapped to each platform's mechanism.
PRIORITY = {
    "low": psutil.IDLE_PRIORITY_CLASS if IS_WIN else 19,
    "below_normal": psutil.BELOW_NORMAL_PRIORITY_CLASS if IS_WIN else 10,
    "normal": psutil.NORMAL_PRIORITY_CLASS if IS_WIN else 0,
    "high": psutil.ABOVE_NORMAL_PRIORITY_CLASS if IS_WIN else -5,
}

SKIP_DIRS = {"appdata", ".git", "node_modules", "__pycache__", "venv", ".venv", ".ssh", ".gnupg", "$recycle.bin"}
LAUNCHABLE = {
    "notepad": "notepad", "calculator": "calc", "calc": "calc", "paint": "mspaint",
    "file explorer": "explorer", "explorer": "explorer", "task manager": "taskmgr",
    "terminal": "wt" if IS_WIN else "x-terminal-emulator",
}


@dataclass
class Tool:
    name: str
    description: str
    params: dict[str, str]      # parameter name -> short description
    required: tuple[str, ...]
    risk: Risk
    func: Callable[..., dict]
    enums: dict[str, tuple[str, ...]] | None = None


class PriorityLedger:
    """Remembers original priorities, so every change can be reverted. A PID can be reused
    by a new process, so the creation time is checked before restoring."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._saved: dict[int, tuple[float, int, str]] = {}

    def demote(self, proc: psutil.Process, value: int) -> None:
        with self._lock:
            if proc.pid not in self._saved:
                self._saved[proc.pid] = (proc.create_time(), proc.nice(), proc.name())
        proc.nice(value)

    def restore_all(self) -> list[str]:
        with self._lock:
            saved, self._saved = self._saved, {}
        restored = []
        for pid, (created, value, name) in saved.items():
            try:
                proc = psutil.Process(pid)
                if abs(proc.create_time() - created) < 1:
                    proc.nice(value)
                    restored.append(name)
            except (psutil.Error, OSError):
                continue
        return restored

    def pids(self) -> set[int]:
        with self._lock:
            return set(self._saved)

    def names(self) -> list[str]:
        with self._lock:
            return sorted({item[2] for item in self._saved.values()})


def find_processes(name: str) -> list[psutil.Process]:
    wanted = normalize(name)
    found = []
    for proc in psutil.process_iter(["name"]):
        try:
            if normalize(proc.info["name"] or "") == wanted:
                found.append(proc)
        except (psutil.Error, OSError):
            continue
    return found


class Toolbox:
    def __init__(self, sampler_snapshot: Callable[[], dict], ledger: PriorityLedger | None = None,
                 search_root: Path | None = None) -> None:
        self.snapshot = sampler_snapshot
        self.ledger = ledger or PriorityLedger()
        self.search_root = Path(search_root or Path.home())
        self.focus_active = False
        self.tools: dict[str, Tool] = {t.name: t for t in self._build()}

    # ---- read-only tools -------------------------------------------------
    def system_status(self) -> dict:
        s = self.snapshot()
        text = (f"CPU {s['cpu_percent']:.0f}%, memory {s['mem_percent']:.0f}% "
                f"({s['mem_used_gb']:.1f} of {s['mem_total_gb']:.1f} GB), disk {s['disk_percent']:.0f}% full. "
                f"You appear to be {s['activity']} in {s.get('foreground') or 'no app'}.")
        if s.get("battery_percent") is not None:
            text += f" Battery {s['battery_percent']:.0f}%, {'charging' if s['on_ac'] else 'on battery'}."
        return {"text": text, "data": {k: s[k] for k in ("cpu_percent", "mem_percent", "disk_percent", "activity")}}

    def top_processes(self, sort_by: str = "cpu", limit: int = 5) -> dict:
        s = self.snapshot()
        limit = max(1, min(int(limit), 8))
        rows = s["top_mem"] if sort_by == "memory" else s["top_cpu"]
        rows = rows[:limit]
        unit = (lambda p: f"{p['mem_mb']:.0f} MB") if sort_by == "memory" else (lambda p: f"{p['cpu']:.0f}% CPU")
        text = f"Top {len(rows)} by {sort_by}: " + ", ".join(f"{p['name']} ({unit(p)})" for p in rows)
        return {"text": text, "data": rows}

    def explain_slowdown(self) -> dict:
        s = self.snapshot()
        causes = []
        if s["cpu_percent"] > 75:
            top = s["top_cpu"][0] if s["top_cpu"] else None
            causes.append(f"CPU is at {s['cpu_percent']:.0f}%" + (f", led by {top['name']} at {top['cpu']:.0f}%" if top else ""))
        if s["mem_percent"] > 85:
            top = s["top_mem"][0] if s["top_mem"] else None
            causes.append(f"memory is {s['mem_percent']:.0f}% full" + (f", mostly {top['name']} at {top['mem_mb']:.0f} MB" if top else ""))
        if s["swap_percent"] > 50:
            causes.append(f"swap is {s['swap_percent']:.0f}% used, so the system may be paging")
        if s["disk_read_mb_s"] + s["disk_write_mb_s"] > 150:
            causes.append(f"the disk is busy at {s['disk_read_mb_s'] + s['disk_write_mb_s']:.0f} MB/s")
        if s["disk_percent"] > 92:
            causes.append(f"the system drive is {s['disk_percent']:.0f}% full")
        if not causes:
            top = s["top_cpu"][0]["name"] if s["top_cpu"] else "nothing notable"
            return {"text": f"No resource pressure right now (CPU {s['cpu_percent']:.0f}%, memory "
                            f"{s['mem_percent']:.0f}%). The busiest process is {top}.", "data": {"causes": []}}
        return {"text": "Likely causes: " + "; ".join(causes) + ".", "data": {"causes": causes}}

    def find_files(self, query: str, limit: int = 8) -> dict:
        query_lower, hits, scanned = query.lower().strip(), [], 0
        deadline = time.monotonic() + 3.0
        for root, dirs, files in os.walk(self.search_root):
            dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")]
            for name in files:
                scanned += 1
                if query_lower in name.lower():
                    hits.append(str(Path(root) / name))
                    if len(hits) >= limit:
                        break
            if len(hits) >= limit or time.monotonic() > deadline:
                break
        text = f"Found {len(hits)} file(s) matching '{query}'" + (": " + "; ".join(hits) if hits else ".")
        return {"text": text, "data": {"hits": hits, "scanned": scanned}}

    def recent_files(self, hours: int = 24, limit: int = 8) -> dict:
        cutoff, found = time.time() - float(hours) * 3600, []
        deadline = time.monotonic() + 3.0
        for root, dirs, files in os.walk(self.search_root):
            dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")]
            for name in files:
                path = Path(root) / name
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if mtime >= cutoff:
                    found.append((mtime, str(path)))
            if time.monotonic() > deadline:
                break
        found.sort(reverse=True)
        paths = [p for _, p in found[:limit]]
        return {"text": f"{len(found)} file(s) changed in the last {hours} h. Latest: " + "; ".join(paths),
                "data": {"paths": paths}}

    def disk_usage(self) -> dict:
        rows = []
        for part in psutil.disk_partitions(all=False):
            try:
                use = psutil.disk_usage(part.mountpoint)
            except OSError:
                continue
            rows.append({"mount": part.mountpoint, "percent": use.percent, "free_gb": round(use.free / 1e9, 1)})
        text = "; ".join(f"{r['mount']} {r['percent']:.0f}% used, {r['free_gb']} GB free" for r in rows)
        return {"text": text, "data": rows}

    def answer(self, message: str = "") -> dict:
        return {"text": message or "I can only help with this computer's state, processes and files.", "data": {}}

    # ---- state-changing tools --------------------------------------------
    def set_priority(self, process: str, level: str = "below_normal") -> dict:
        procs = find_processes(process)
        if not procs:
            return {"text": f"No running process is called '{process}'.", "data": {"changed": 0}}
        changed = 0
        for proc in procs:
            if is_protected(proc.name()):
                continue
            try:
                self.ledger.demote(proc, PRIORITY[level])
                changed += 1
            except (psutil.Error, OSError):
                continue
        return {"text": f"Set {changed} '{process}' process(es) to {level} priority.", "data": {"changed": changed}}

    def restore_priorities(self) -> dict:
        names = self.ledger.restore_all()
        self.focus_active = False
        text = f"Restored the original priority of {len(names)} process(es)." if names else "Nothing to restore."
        return {"text": text, "data": {"restored": names}}

    def focus_mode(self, state: str = "on") -> dict:
        if state == "off":
            return self.restore_priorities()
        s, demoted = self.snapshot(), []
        for row in s["top_cpu"]:
            if row["pid"] == s.get("foreground_pid") or row["pid"] == os.getpid():
                continue
            if row["cpu"] < 5 or is_protected(row["name"]) or row["name"] == s.get("foreground"):
                continue
            try:
                self.ledger.demote(psutil.Process(row["pid"]), PRIORITY["below_normal"])
                demoted.append(row["name"])
            except (psutil.Error, OSError):
                continue
        self.focus_active = True
        text = (f"Focus mode on for {s.get('foreground') or 'the current app'}. Demoted: "
                + (", ".join(sorted(set(demoted))) or "nothing, no background process is busy") + ".")
        return {"text": text, "data": {"demoted": demoted}}

    def terminate_process(self, process: str) -> dict:
        procs = find_processes(process)
        if not procs:
            return {"text": f"No running process is called '{process}'.", "data": {"terminated": 0}}
        count = 0
        for proc in procs:
            if proc.pid == os.getpid() or is_protected(proc.name()):
                continue
            try:
                proc.terminate()
                count += 1
            except (psutil.Error, OSError):
                continue
        return {"text": f"Asked {count} '{process}' process(es) to exit.", "data": {"terminated": count}}

    def launch_app(self, app: str) -> dict:
        command = LAUNCHABLE.get(app.lower().strip())
        if command is None:
            return {"text": f"'{app}' is not on the allowed launch list: {', '.join(sorted(LAUNCHABLE))}.",
                    "data": {"launched": False}}
        if shutil.which(command) is None:
            return {"text": f"'{app}' is not available on this system.", "data": {"launched": False}}
        subprocess.Popen([command], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"text": f"Launched {app}.", "data": {"launched": True}}

    # ---- table -----------------------------------------------------------
    def _build(self) -> list[Tool]:
        return [
            Tool("system_status", "overall CPU, memory, disk, battery and current activity", {}, (), Risk.READ, self.system_status),
            Tool("top_processes", "list the processes using the most CPU or memory",
                 {"sort_by": "cpu or memory", "limit": "how many, 1-8"}, (), Risk.READ, self.top_processes,
                 {"sort_by": ("cpu", "memory")}),
            Tool("explain_slowdown", "diagnose why the computer is slow, laggy or hot", {}, (), Risk.READ, self.explain_slowdown),
            Tool("find_files", "search the user's files by name", {"query": "word in the file name"}, ("query",), Risk.READ, self.find_files),
            Tool("recent_files", "list files changed recently", {"hours": "look-back window"}, (), Risk.READ, self.recent_files),
            Tool("disk_usage", "free and used space on each drive", {}, (), Risk.READ, self.disk_usage),
            Tool("set_priority", "change the scheduling priority of a named process",
                 {"process": "process name", "level": "low, below_normal, normal or high"}, ("process",), Risk.ADJUST, self.set_priority,
                 {"level": tuple(PRIORITY)}),
            Tool("restore_priorities", "undo all priority changes", {}, (), Risk.ADJUST, self.restore_priorities),
            Tool("focus_mode", "favour the foreground app by demoting busy background apps",
                 {"state": "on or off"}, (), Risk.ADJUST, self.focus_mode, {"state": ("on", "off")}),
            Tool("launch_app", "open an application", {"app": "application name"}, ("app",), Risk.ADJUST, self.launch_app),
            Tool("terminate_process", "close or kill a named process", {"process": "process name"}, ("process",), Risk.DANGEROUS, self.terminate_process),
            Tool("answer", "no system action fits: reply with a short message", {"message": "the reply"}, (), Risk.READ, self.answer),
        ]
