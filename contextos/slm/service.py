"""Inference as a managed operating system resource.

The model is treated like any other scarce resource:
  * Scheduling: one worker thread and a priority queue, so an interactive request
    overtakes queued background work. Inference runs non-preemptively.
  * Memory: lazy loading on first use, eviction after an idle timeout.
  * Admission control: if free RAM is below the model's need, the request is refused
    and the caller falls back to the rule planner instead of pushing the OS into paging.
"""
from __future__ import annotations

import gc
import itertools
import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field

import psutil

from ..config import IDLE_UNLOAD_S

INTERACTIVE, BACKGROUND = 0, 1


class AdmissionDenied(RuntimeError):
    pass


@dataclass
class Metrics:
    requests: int = 0
    denied: int = 0
    loads: int = 0
    unloads: int = 0
    last_load_s: float = 0.0
    last_first_token_s: float = 0.0
    last_tokens_per_s: float = 0.0
    last_total_s: float = 0.0


@dataclass(order=True)
class _Job:
    priority: int
    order: int
    kwargs: dict = field(compare=False)
    future: Future = field(compare=False)


class InferenceService:
    def __init__(self, backend, min_free_ram_gb: float = 1.0, idle_unload_s: float = IDLE_UNLOAD_S) -> None:
        self.backend = backend
        self.min_free_ram_gb = min_free_ram_gb
        self.idle_unload_s = idle_unload_s
        self.metrics = Metrics()
        self._jobs: queue.PriorityQueue[_Job] = queue.PriorityQueue()
        self._order = itertools.count()
        self._last_used = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="slm-worker", daemon=True)
        self._thread.start()

    def submit(self, priority: int = INTERACTIVE, **kwargs) -> Future:
        future: Future = Future()
        self._jobs.put(_Job(priority, next(self._order), kwargs, future))
        return future

    def generate(self, timeout: float = 180, priority: int = INTERACTIVE, **kwargs):
        return self.submit(priority, **kwargs).result(timeout)

    def _admit(self) -> None:
        if self.backend.loaded():
            return
        free_gb = psutil.virtual_memory().available / 1e9
        if free_gb < self.min_free_ram_gb:
            self.metrics.denied += 1
            raise AdmissionDenied(f"only {free_gb:.1f} GB free, the model needs {self.min_free_ram_gb:.1f} GB")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=1.0)
            except queue.Empty:
                self._maybe_unload()
                continue
            try:
                self._admit()
                if not self.backend.loaded():
                    started = time.perf_counter()
                    self.backend.load()
                    self.metrics.loads += 1
                    self.metrics.last_load_s = time.perf_counter() - started
                result = self.backend.generate(**job.kwargs)
                self.metrics.requests += 1
                self.metrics.last_first_token_s = result.first_token_s
                self.metrics.last_tokens_per_s = result.tokens_per_s
                self.metrics.last_total_s = result.total_s
                self._last_used = time.monotonic()
                job.future.set_result(result)
            except Exception as exc:  # the future carries it back to the caller
                job.future.set_exception(exc)

    def _maybe_unload(self) -> None:
        if self.backend.loaded() and time.monotonic() - self._last_used > self.idle_unload_s:
            self.backend.unload()
            gc.collect()
            self.metrics.unloads += 1

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
