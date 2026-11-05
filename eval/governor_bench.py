"""Does context-driven scheduling really help the foreground app? Measure it.

A foreground process repeats a short CPU task and then waits, like an interactive app.
Background "hog" processes spin on every logical core, twice over. Three conditions:
  idle:         no hogs, the best case
  contended:    hogs running, governor off
  governed:     hogs running, the real Governor driven by real Sampler data

Reported: response time of each foreground task (mean, p50, p95, p99).
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import statistics
import threading
import time
from pathlib import Path

import psutil

from contextos.context.sensors import Sampler
from contextos.governor import Governor
from contextos.tools import PriorityLedger

RESULTS = Path(__file__).parent / "results"
TASKS = 400
WORK = 60_000        # loop iterations per foreground task
WAIT_S = 0.02
WARMUP_S = 6         # lets the governor see the load before measuring
REPEATS = 3


def hog() -> None:
    while True:
        sum(i * i for i in range(10_000))


def foreground(result: mp.Queue, start: mp.Event) -> None:
    result.put(os.getpid())
    start.wait()
    times = []
    for _ in range(TASKS):
        begin = time.perf_counter()
        total = 0
        for i in range(WORK):
            total += i * i
        times.append((time.perf_counter() - begin) * 1000)
        time.sleep(WAIT_S)
    result.put(times)


def run(condition: str) -> dict:
    results: mp.Queue = mp.Queue()
    start = mp.Event()
    fg = mp.Process(target=foreground, args=(results, start))
    fg.start()
    fg_pid = results.get()
    hogs = []
    if condition != "idle":
        hogs = [mp.Process(target=hog, daemon=True) for _ in range(2 * psutil.cpu_count())]
        for proc in hogs:
            proc.start()

    stop = threading.Event()
    governor = Governor(PriorityLedger())
    actions = []

    def control_loop() -> None:
        sampler = Sampler()
        sampler.sample()
        while not stop.wait(1.0):
            snap = sampler.sample(top_n=len(hogs) + 8).to_dict()
            # This benchmark has no window, so the foreground identity is supplied.
            snap["foreground"], snap["foreground_pid"], snap["activity"] = "bench-foreground", fg_pid, "coding"
            event = governor.step(snap)
            if event:
                actions.append(event["event"])

    thread = None
    if condition == "governed":
        thread = threading.Thread(target=control_loop, daemon=True)
        thread.start()
    time.sleep(WARMUP_S if hogs else 1)
    cpu_seen = psutil.cpu_percent(interval=0.5)
    start.set()
    times = results.get()
    fg.join()
    stop.set()
    if thread:
        thread.join(timeout=3)
    demoted = len(governor.ledger.pids())
    governor.shutdown()
    for proc in hogs:
        proc.terminate()
    for proc in hogs:
        proc.join()
    ordered = sorted(times)
    pick = lambda q: round(ordered[min(int(len(ordered) * q), len(ordered) - 1)], 2)
    return {"condition": condition, "tasks": TASKS, "mean_ms": round(statistics.mean(times), 2),
            "p50_ms": pick(.5), "p95_ms": pick(.95), "p99_ms": pick(.99), "max_ms": round(ordered[-1], 2),
            "cpu_percent_before": cpu_seen, "hogs": len(hogs), "demoted": demoted, "actions": actions}


def main() -> None:
    runs = []
    for repeat in range(REPEATS):
        for condition in ("idle", "contended", "governed"):
            outcome = run(condition)
            outcome["repeat"] = repeat
            runs.append(outcome)
            print(json.dumps(outcome), flush=True)
            time.sleep(3)
    summary = {}
    for condition in ("idle", "contended", "governed"):
        rows = [r for r in runs if r["condition"] == condition]
        summary[condition] = {k: round(statistics.mean(r[k] for r in rows), 2) for k in ("mean_ms", "p50_ms", "p95_ms", "p99_ms")}
        summary[condition]["mean_ms_stdev"] = round(statistics.stdev(r["mean_ms"] for r in rows), 2)
        summary[condition]["p95_ms_stdev"] = round(statistics.stdev(r["p95_ms"] for r in rows), 2)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "governor_bench.json").write_text(json.dumps({"summary": summary, "runs": runs,
        "logical_cores": psutil.cpu_count(), "note": "priority class change only, no process stopped"}, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
