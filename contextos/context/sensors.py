"""User-space sensors that read operating system state.

Window titles are deliberately never read: they often hold private text such as
e-mail subjects. Only the executable name of the foreground process is used.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

import psutil

from .activity import classify


@dataclass
class ProcInfo:
    pid: int
    name: str
    cpu: float        # percent of one core, so it can exceed 100
    mem_mb: float
    nice: int | None = None


@dataclass
class Snapshot:
    ts: float
    cpu_percent: float
    per_core: list[float]
    mem_percent: float
    mem_used_gb: float
    mem_total_gb: float
    swap_percent: float
    disk_percent: float
    disk_read_mb_s: float
    disk_write_mb_s: float
    net_kb_s: float
    battery_percent: float | None
    on_ac: bool | None
    idle_seconds: float
    foreground: str | None
    foreground_pid: int | None
    activity: str
    hour: int
    top_cpu: list[ProcInfo] = field(default_factory=list)
    top_mem: list[ProcInfo] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _foreground_windows() -> int | None:
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value) or None


def _foreground_linux() -> int | None:
    if not shutil.which("xdotool"):
        return None
    try:
        out = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowpid"],
            capture_output=True, text=True, timeout=1, check=True,
        )
        return int(out.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        return None


def foreground_pid() -> int | None:
    return _foreground_windows() if sys.platform == "win32" else _foreground_linux()


def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input (Windows only, otherwise 0)."""
    if sys.platform != "win32":
        return 0.0

    class LastInput(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LastInput()
    info.cbSize = ctypes.sizeof(LastInput)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    ticks = ctypes.windll.kernel32.GetTickCount()
    return ((ticks - info.dwTime) & 0xFFFFFFFF) / 1000.0


class Sampler:
    """Takes snapshots. psutil computes CPU use as a delta, so Process objects are cached."""

    IGNORED = {"system idle process", "idle"}
    SLOW_S = 0.0005     # a process that takes longer than this to read is "slow"
    SLOW_EVERY = 15     # slow processes are re-read every 15 samples (30 s at the default rate)

    def __init__(self) -> None:
        self._procs: dict[int, psutil.Process] = {}
        self._slow: set[int] = set()
        self._last_info: dict[int, ProcInfo] = {}
        self._tick = 0
        self._own_pid = os.getpid()
        self._last_disk = psutil.disk_io_counters()
        self._last_net = psutil.net_io_counters()
        self._last_t = time.monotonic()
        psutil.cpu_percent(percpu=True)

    def _scan_processes(self) -> list[ProcInfo]:
        seen, result = set(), []
        self._tick += 1
        for pid in psutil.pids():
            seen.add(pid)
            if pid == self._own_pid:   # the model's own memory must never look like a "hog"
                continue
            try:
                proc = self._procs.get(pid)
                if proc is None:
                    proc = psutil.Process(pid)
                    self._procs[pid] = proc
                    proc.cpu_percent(None)  # first call only primes the counter
                    continue
                # Two-tier sampling. Reading a protected process on Windows appears to fall back
                # to a scan of the whole system process table, which costs milliseconds per
                # process. Such slow processes are re-read only every SLOW_EVERY samples
                # and keep their last value in between. CPU use is a delta, so the value
                # stays correct when it is finally refreshed.
                if pid in self._slow and self._tick % self.SLOW_EVERY:
                    if pid in self._last_info:
                        result.append(self._last_info[pid])
                    continue
                begin = time.perf_counter()
                with proc.oneshot():
                    name = proc.name()
                    if name.lower() in self.IGNORED:
                        continue
                    info = ProcInfo(pid, name, proc.cpu_percent(None), proc.memory_info().rss / 1e6)
                if time.perf_counter() - begin > self.SLOW_S:
                    self._slow.add(pid)
                else:
                    self._slow.discard(pid)
                self._last_info[pid] = info
                result.append(info)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                self._procs.pop(pid, None)
        for pid in set(self._procs) - seen:
            self._procs.pop(pid, None)
            self._last_info.pop(pid, None)
            self._slow.discard(pid)
        return result

    def sample(self, top_n: int = 8) -> Snapshot:
        now = time.monotonic()
        elapsed = max(now - self._last_t, 1e-3)
        per_core = psutil.cpu_percent(percpu=True)
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk_io, net_io = psutil.disk_io_counters(), psutil.net_io_counters()

        read_rate = write_rate = 0.0
        if disk_io and self._last_disk:
            read_rate = (disk_io.read_bytes - self._last_disk.read_bytes) / elapsed / 1e6
            write_rate = (disk_io.write_bytes - self._last_disk.write_bytes) / elapsed / 1e6
        net_rate = 0.0
        if net_io and self._last_net:
            net_rate = ((net_io.bytes_recv + net_io.bytes_sent)
                        - (self._last_net.bytes_recv + self._last_net.bytes_sent)) / elapsed / 1e3
        self._last_disk, self._last_net, self._last_t = disk_io, net_io, now

        procs = self._scan_processes()
        fg_pid = foreground_pid()
        fg_name = None
        if fg_pid:
            try:
                fg_name = psutil.Process(fg_pid).name()
            except (psutil.Error, OSError):
                fg_pid = None

        battery = None
        if hasattr(psutil, "sensors_battery"):
            try:
                battery = psutil.sensors_battery()
            except (RuntimeError, OSError):
                battery = None

        idle = idle_seconds()
        moment = datetime.now()
        return Snapshot(
            ts=time.time(),
            cpu_percent=round(sum(per_core) / max(len(per_core), 1), 1),
            per_core=per_core,
            mem_percent=mem.percent,
            mem_used_gb=round((mem.total - mem.available) / 1e9, 2),
            mem_total_gb=round(mem.total / 1e9, 2),
            swap_percent=swap.percent,
            disk_percent=psutil.disk_usage(_root()).percent,
            disk_read_mb_s=round(max(read_rate, 0), 2),
            disk_write_mb_s=round(max(write_rate, 0), 2),
            net_kb_s=round(max(net_rate, 0), 1),
            battery_percent=battery.percent if battery else None,
            on_ac=battery.power_plugged if battery else None,
            idle_seconds=round(idle, 1),
            foreground=fg_name,
            foreground_pid=fg_pid,
            activity=classify(fg_name, idle),
            hour=moment.hour,
            top_cpu=sorted(procs, key=lambda p: p.cpu, reverse=True)[:top_n],
            top_mem=sorted(procs, key=lambda p: p.mem_mb, reverse=True)[:top_n],
        )


def _root() -> str:
    return "C:\\" if sys.platform == "win32" else "/"
