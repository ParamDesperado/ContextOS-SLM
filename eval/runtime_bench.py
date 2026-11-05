"""Cost of the system itself: sampler overhead, model load time, memory, throughput.

    python -m eval.runtime_bench sampler
    python -m eval.runtime_bench model phi3-gpu
"""
from __future__ import annotations

import gc
import json
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

import psutil

from contextos.context.activity import classify
from contextos.context.sensors import Sampler
from contextos.setup_models import model_path
from contextos.slm.backends import OrtGenAIBackend
from contextos.slm.planner import SYSTEM_RULES, catalog
from contextos.tools import Toolbox

RESULTS = Path(__file__).parent / "results"
ME = psutil.Process()


def gpu_mib() -> int | None:
    if not shutil.which("nvidia-smi"):
        return None
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True)
    return int(out.stdout.split()[0]) if out.returncode == 0 else None


def sampler_bench() -> dict:
    sampler = Sampler()
    sampler.sample()
    durations = []
    for _ in range(50):
        time.sleep(0.2)
        begin = time.perf_counter()
        sampler.sample(16)
        durations.append((time.perf_counter() - begin) * 1000)
    begin = time.perf_counter()
    for _ in range(100_000):
        classify("chrome.exe")
    classify_us = (time.perf_counter() - begin) / 100_000 * 1e6
    interval = 2.0
    return {"sample_mean_ms": round(statistics.mean(durations), 1),
            "sample_p95_ms": round(sorted(durations)[47], 1),
            "duty_cycle_percent_at_2s": round(statistics.mean(durations) / (interval * 1000) * 100, 2),
            "classify_us": round(classify_us, 2),
            "process_rss_mb": round(ME.memory_info().rss / 1e6, 1),
            "processes_visible": len(psutil.pids())}


def model_bench(key: str) -> dict:
    tools = Toolbox(lambda: {}).tools
    system = SYSTEM_RULES.replace("%s", catalog(tools), 1)
    user = "CONTEXT:\nactivity: coding\nforeground_app: Code\ncpu: 91%  memory: 88%  disk: 61%\ncpu_hog: blender\n\nREQUEST: why is my laptop slow"
    rss_before, gpu_before = ME.memory_info().rss / 1e6, gpu_mib()
    backend = OrtGenAIBackend(str(model_path(key)), key)
    begin = time.perf_counter()
    backend.load()
    load_s = time.perf_counter() - begin
    rss_loaded, gpu_loaded = ME.memory_info().rss / 1e6, gpu_mib()

    cold = backend.generate(system, user, max_tokens=8)
    runs = [backend.generate(system, user, max_tokens=64) for _ in range(5)]
    rss_peak, gpu_peak = ME.memory_info().rss / 1e6, gpu_mib()

    begin = time.perf_counter()
    backend.unload()
    gc.collect()
    unload_s = time.perf_counter() - begin
    time.sleep(1)
    rss_after, gpu_after = ME.memory_info().rss / 1e6, gpu_mib()
    return {
        "model": key, "disk_gb": round(sum(f.stat().st_size for f in model_path(key).iterdir()) / 1e9, 2),
        "load_s": round(load_s, 2), "unload_s": round(unload_s, 2),
        "prompt_tokens": runs[0].prompt_tokens,
        "first_token_cold_s": round(cold.first_token_s, 2),
        "first_token_s": round(statistics.mean(r.first_token_s for r in runs), 2),
        "tokens_per_s": round(statistics.mean(r.tokens_per_s for r in runs), 1),
        "rss_before_mb": round(rss_before), "rss_loaded_mb": round(rss_loaded), "rss_peak_mb": round(rss_peak),
        "rss_after_unload_mb": round(rss_after),
        "gpu_before_mib": gpu_before, "gpu_loaded_mib": gpu_loaded, "gpu_peak_mib": gpu_peak, "gpu_after_unload_mib": gpu_after,
    }


def main() -> None:
    mode = sys.argv[1]
    out = sampler_bench() if mode == "sampler" else model_bench(sys.argv[2])
    RESULTS.mkdir(exist_ok=True)
    name = "runtime_sampler" if mode == "sampler" else f"runtime_{sys.argv[2]}"
    (RESULTS / f"{name}.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
