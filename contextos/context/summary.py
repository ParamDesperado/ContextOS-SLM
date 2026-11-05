"""Compress a snapshot into a short text block for the prompt.

A small model has a small context window (4096 tokens for Phi-3-mini), and every
prompt token adds latency. The block is capped at a few hundred characters and
holds only facts that could change the choice of action.
"""
from __future__ import annotations


def _clean(name: str) -> str:
    return name.removesuffix(".exe")


def summarize(snap: dict, top_n: int = 3) -> str:
    """snap is the dictionary form of a Snapshot."""
    lines = [
        f"activity: {snap.get('activity', 'unknown')}",
        f"foreground_app: {_clean(snap.get('foreground') or 'none')}",
        f"cpu: {snap.get('cpu_percent', 0):.0f}%  memory: {snap.get('mem_percent', 0):.0f}%"
        f"  disk: {snap.get('disk_percent', 0):.0f}%",
    ]
    if snap.get("battery_percent") is not None:
        state = "charging" if snap.get("on_ac") else "on battery"
        lines.append(f"battery: {snap['battery_percent']:.0f}% {state}")
    top_cpu = [p for p in snap.get("top_cpu", []) if p["cpu"] >= 1][:top_n]
    if top_cpu:
        lines.append(f"cpu_hog: {_clean(top_cpu[0]['name'])}")
        lines.append("top_cpu: " + ", ".join(f"{_clean(p['name'])} {p['cpu']:.0f}%" for p in top_cpu))
    top_mem = snap.get("top_mem", [])[:top_n]
    if top_mem:
        lines.append(f"memory_hog: {_clean(top_mem[0]['name'])}")
        lines.append("top_memory: " + ", ".join(f"{_clean(p['name'])} {p['mem_mb']:.0f}MB" for p in top_mem))
    return "\n".join(lines)
