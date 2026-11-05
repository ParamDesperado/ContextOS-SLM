"""Composes the subsystems into one running ContextOS instance."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from . import config
from .context.sensors import Sampler
from .context.store import ContextStore
from .context.summary import summarize
from .governor import Governor
from .security.audit import AuditLog
from .security.policy import ConfirmationQueue, Decision, evaluate
from .slm.backends import Backend, HttpBackend, OrtGenAIBackend
from .slm.planner import Plan, RulePlanner, SLMPlanner
from .slm.service import AdmissionDenied, InferenceService
from .tools import PriorityLedger, Toolbox


def default_backend() -> tuple[Backend | None, float]:
    """Pick a backend from the environment or from the installed models."""
    import os
    from .setup_models import is_installed, model_path
    url = os.environ.get("CONTEXTOS_LLM_URL")
    if url:
        return HttpBackend(url, os.environ.get("CONTEXTOS_LLM_MODEL", "local")), 0.5
    for key in (config.DEFAULT_MODEL, *config.MODELS):
        if is_installed(key):
            return OrtGenAIBackend(str(model_path(key)), key), config.MODELS[key].min_free_ram_gb
    return None, 0.0


TOP_N = 16  # processes kept per sample; the governor needs more than the prompt does


class ContextOS:
    def __init__(self, backend: Backend | None = None, min_free_ram_gb: float = 1.0,
                 home: Path | None = None, search_root: Path | None = None,
                 start_sampler: bool = True) -> None:
        home = Path(home or config.data_dir())
        home.mkdir(parents=True, exist_ok=True)
        self.audit = AuditLog(home / "audit.jsonl")
        self.store = ContextStore(home / "context.db")
        self.sampler = Sampler()
        self.sampler.sample()   # primes the CPU counters, which are computed as deltas
        time.sleep(0.5)
        self._latest = self.sampler.sample(TOP_N).to_dict()
        self.ledger = PriorityLedger()
        self.toolbox = Toolbox(self.snapshot, self.ledger, search_root)
        self.governor = Governor(self.ledger, self.audit)
        self.confirmations = ConfirmationQueue()
        self.rules = RulePlanner()
        self.service = InferenceService(backend, min_free_ram_gb) if backend else None
        self.planner = SLMPlanner(self.service, self.toolbox.tools) if backend else None
        self.events: list[dict] = []
        self.turns: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="context-sampler", daemon=True)
        if start_sampler:
            self._thread.start()

    def snapshot(self) -> dict:
        return self._latest

    def _loop(self) -> None:
        while not self._stop.wait(config.SAMPLE_INTERVAL_S):
            snap = self.sampler.sample(TOP_N)
            self._latest = snap.to_dict()
            self.store.add(snap)
            event = self.governor.step(self._latest)
            if event:
                event["ts"] = time.time()
                self.events = (self.events + [event])[-30:]

    def make_plan(self, request: str) -> Plan:
        """Slow path: ask the model. Fall back to rules if it is absent, refused or invalid."""
        if self.planner:
            try:
                plan = self.planner.plan(request, summarize(self.snapshot()))
                if not plan.error:
                    return plan
                fallback = self.rules.plan(request)
                fallback.error, fallback.raw = plan.error, plan.raw
                fallback.latency_s = plan.latency_s
                return fallback
            except (AdmissionDenied, TimeoutError, OSError, RuntimeError) as exc:
                fallback = self.rules.plan(request)
                fallback.error = str(exc)
                return fallback
        return self.rules.plan(request)

    def _execute(self, tool_name: str, args: dict, actor: str) -> dict:
        tool = self.toolbox.tools[tool_name]
        try:
            result = tool.func(**args)
            self.audit.append(actor, tool_name, args, "allow", result["text"][:200])
            return result
        except (TypeError, ValueError, OSError) as exc:
            self.audit.append(actor, tool_name, args, "allow", f"error: {exc}")
            return {"text": f"{tool_name} failed: {exc}", "data": {}}

    def ask(self, request: str) -> dict:
        started = time.perf_counter()
        plan = self.make_plan(request)
        response = {
            "request": request, "tool": plan.tool, "args": plan.args, "planner": plan.source,
            "slm_seconds": round(plan.latency_s, 2), "tokens_per_s": round(plan.tokens_per_s, 1),
            "planner_note": plan.error,
        }
        tool = self.toolbox.tools[plan.tool]
        verdict = evaluate(tool, plan.args)
        response["decision"], response["reason"] = verdict.decision.value, verdict.reason
        if verdict.decision is Decision.DENY:
            self.audit.append("slm", plan.tool, plan.args, "deny", verdict.reason)
            response["text"] = f"Refused: {verdict.reason}."
        elif verdict.decision is Decision.CONFIRM:
            token = self.confirmations.create(plan.tool, plan.args)
            self.audit.append("slm", plan.tool, plan.args, "confirm", "waiting for the user")
            response["token"] = token
            response["text"] = f"Please confirm: {plan.tool} {plan.args}. {verdict.reason}."
        else:
            result = self._execute(plan.tool, plan.args, "slm" if plan.source == "slm" else "rules")
            response["text"], response["data"] = result["text"], result["data"]
        response["total_seconds"] = round(time.perf_counter() - started, 2)
        self.turns = (self.turns + [{k: v for k, v in response.items() if k != "data"}])[-10:]
        return response

    def confirm(self, token: str, approve: bool = True) -> dict:
        if not approve:
            self.confirmations.cancel(token)
            return {"text": "Cancelled."}
        item = self.confirmations.consume(token)
        if item is None:
            return {"text": "That confirmation has expired or was already used."}
        tool_name, args = item
        verdict = evaluate(self.toolbox.tools[tool_name], args)
        if verdict.decision is Decision.DENY:
            return {"text": f"Refused: {verdict.reason}."}
        return self._execute(tool_name, args, "user-confirmed")

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=config.SAMPLE_INTERVAL_S + 3)
        self.governor.shutdown()
        if self.service:
            self.service.stop()
        self.store.close()
