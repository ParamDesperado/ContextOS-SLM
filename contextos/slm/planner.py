"""Turn a natural-language request plus system context into one validated tool call."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

SYSTEM_RULES = """You are the planner inside an operating system. Pick the ONE tool that fulfils the request.
Reply with JSON only: {"tool": "<name>", "args": {...}}

Tools:
%s

Rules:
- CONTEXT is live system state. If the request says "this app", "that thing", "whatever is hogging memory" or names no process, take the real process name from CONTEXT.
- "this", "this app" or "the current app" means foreground_app.
- A process eating memory or RAM is memory_hog. A process eating CPU, or making the machine hot or noisy, is cpu_hog. These are often not the foreground_app.
- Use set_priority to slow down or throttle a process, terminate_process only to close or kill it.
- Use the answer tool for anything unrelated to this computer.

Examples:
CONTEXT foreground_app: word, memory_hog: chrome | REQUEST: kill whatever is eating my ram
{"tool": "terminate_process", "args": {"process": "chrome"}}
CONTEXT foreground_app: word, cpu_hog: ffmpeg | REQUEST: slow down the thing using my cpu
{"tool": "set_priority", "args": {"process": "ffmpeg", "level": "low"}}
REQUEST: find my resume
{"tool": "find_files", "args": {"query": "resume"}}
REQUEST: what is the capital of France
{"tool": "answer", "args": {"message": "I only handle tasks on this computer."}}"""


@dataclass
class Plan:
    tool: str
    args: dict = field(default_factory=dict)
    source: str = "slm"
    raw: str = ""
    error: str = ""
    latency_s: float = 0.0
    prompt_tokens: int = 0
    output_tokens: int = 0
    tokens_per_s: float = 0.0


def catalog(tools: dict) -> str:
    lines = []
    for tool in tools.values():
        params = ", ".join(f"{k}: {v}" for k, v in tool.params.items())
        lines.append(f"- {tool.name}({params}): {tool.description}")
    return "\n".join(lines)


def balanced_json(text: str) -> str | None:
    """Return the first complete top-level JSON object in text, or None."""
    start = text.find("{")
    if start < 0:
        return None
    depth, in_string, escaped = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def validate(obj: object, tools: dict) -> tuple[str, dict]:
    """Schema check of untrusted model output. Raises ValueError when it does not conform."""
    if not isinstance(obj, dict) or not isinstance(obj.get("tool"), str):
        raise ValueError("output is not an object with a 'tool' string")
    tool = tools.get(obj["tool"])
    if tool is None:
        raise ValueError(f"unknown tool '{obj['tool']}'")
    raw_args = obj.get("args") or {}
    if not isinstance(raw_args, dict):
        raise ValueError("'args' is not an object")
    args = {k: v for k, v in raw_args.items() if k in tool.params and v not in (None, "")}
    for name in tool.required:
        if name not in args:
            raise ValueError(f"tool '{tool.name}' is missing '{name}'")
    for name, allowed in (tool.enums or {}).items():
        if name in args and str(args[name]).lower() not in allowed:
            if name in tool.required:
                raise ValueError(f"'{args[name]}' is not valid for {name}")
            args.pop(name)
    for name, value in args.items():
        args[name] = str(value).strip() if not isinstance(value, (int, float)) else value
    return tool.name, args


class SLMPlanner:
    def __init__(self, service, tools: dict, use_context: bool = True) -> None:
        self.service, self.tools, self.use_context = service, tools, use_context
        self.system = SYSTEM_RULES.replace("%s", catalog(tools), 1)

    def plan(self, request: str, context: str = "") -> Plan:
        user = f"CONTEXT:\n{context}\n\nREQUEST: {request}" if self.use_context and context else f"REQUEST: {request}"
        result = self.service.generate(system=self.system, user=user, seed='{"tool": "', max_tokens=64,
                                       stop=lambda text: balanced_json(text) is not None)
        plan = Plan(tool="", raw=result.text, latency_s=result.total_s, prompt_tokens=result.prompt_tokens,
                    output_tokens=result.output_tokens, tokens_per_s=result.tokens_per_s)
        try:
            payload = balanced_json(result.text)
            if payload is None:
                raise ValueError("no complete JSON object in the output")
            plan.tool, plan.args = validate(json.loads(payload), self.tools)
        except ValueError as exc:  # json.JSONDecodeError is a ValueError
            plan.error = str(exc)
        return plan


# ---------------------------------------------------------------------------
# Rule planner: a keyword baseline for evaluation, and the fallback when the
# model is unavailable. It cannot see context, so it cannot resolve references.
# ---------------------------------------------------------------------------
_NAME = r"([A-Za-z0-9_.\-]+)"
_NOT_A_NAME = {"it", "this", "that", "the", "whatever", "something", "my", "thing", "app", "everything", "all"}


def _named(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    if match and match.group(1).lower() not in _NOT_A_NAME:
        return match.group(1)
    return None


class RulePlanner:
    def plan(self, request: str, context: str = "") -> Plan:
        text = request.lower().strip()
        tool, args = self._match(text)
        return Plan(tool=tool, args=args, source="rules")

    @staticmethod
    def _match(text: str) -> tuple[str, dict]:
        name = _named(rf"\b(?:kill|terminate|close|stop|quit|end)\s+{_NAME}", text)
        if name:
            return "terminate_process", {"process": name}
        name = _named(rf"\b(?:throttle|slow down|deprioriti[sz]e|lower(?: the)? priority of)\s+{_NAME}", text)
        if name:
            return "set_priority", {"process": name, "level": "below_normal"}
        if re.search(r"\b(restore|undo|reset)\b.*\b(priorit|change)", text):
            return "restore_priorities", {}
        if "focus" in text or "do not disturb" in text:
            return "focus_mode", {"state": "off" if re.search(r"\b(off|stop|disable|end)\b", text) else "on"}
        if re.search(r"\b(slow|lag|laggy|hang|hot|freez)", text):
            return "explain_slowdown", {}
        if re.search(r"\b(disk|storage|space)\b", text):
            return "disk_usage", {}
        if re.search(r"\b(recent|recently|change|changed|modified|edited)\b", text):
            return "recent_files", {}
        match = re.search(rf"\b(?:find|search for|locate|where is)\s+(?:my\s+|the\s+)?{_NAME}", text)
        if match:
            return "find_files", {"query": match.group(1)}
        match = re.search(rf"\b(?:open|launch|start)\s+(?:the\s+)?([a-z ]+)$", text)
        if match:
            return "launch_app", {"app": match.group(1).strip()}
        if re.search(r"\b(process|processes|using|eating|hogging|consuming)\b", text):
            return "top_processes", {"sort_by": "memory" if re.search(r"\b(memory|ram)\b", text) else "cpu"}
        if re.search(r"\b(status|cpu|memory|ram|battery|health|how is)\b", text):
            return "system_status", {}
        return "answer", {"message": ""}
