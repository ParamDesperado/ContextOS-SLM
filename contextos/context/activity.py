"""Infer what the user is doing from the foreground process name.

This is the fast path: a dictionary lookup that costs microseconds, so it can run
on every sample without waking the language model.
"""
from __future__ import annotations

import os

IDLE_AFTER_S = float(os.environ.get("CONTEXTOS_IDLE_AFTER_S", "180"))

_RULES: dict[str, tuple[str, ...]] = {
    "coding": ("code", "devenv", "pycharm", "idea", "clion", "vim", "nvim", "emacs", "sublime",
               "antigravity", "windowsterminal", "powershell", "cmd", "bash", "gnome-terminal", "konsole"),
    "browsing": ("chrome", "msedge", "firefox", "brave", "opera", "safari"),
    "meeting": ("zoom", "teams", "ms-teams", "webex", "slack", "discord", "skype"),
    "media": ("vlc", "spotify", "mpv", "wmplayer", "applemusic", "netflix"),
    "gaming": ("gta", "witcher", "steam", "epicgames", "rockstar", "acblackflag", "valorant", "minecraft"),
    "documents": ("winword", "excel", "powerpnt", "acrobat", "acrord32", "notepad", "soffice", "obsidian"),
}


def classify(foreground: str | None, idle_seconds: float = 0.0) -> str:
    if idle_seconds >= IDLE_AFTER_S:
        return "idle"
    if not foreground:
        return "unknown"
    stem = foreground.lower().removesuffix(".exe")
    for activity, names in _RULES.items():
        if any(stem == name or stem.startswith(name) for name in names):
            return activity
    return "other"
