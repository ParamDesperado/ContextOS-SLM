"""Central configuration. Everything is overridable through environment variables."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def data_dir() -> Path:
    """Return the per-user data folder (models, SQLite store, audit log)."""
    override = os.environ.get("CONTEXTOS_HOME")
    if override:
        path = Path(override)
    elif sys.platform == "win32":
        path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ContextOS"
    else:
        path = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "contextos"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class ModelSpec:
    key: str
    label: str
    repo: str
    subfolder: str
    approx_size_gb: float
    min_free_ram_gb: float


# Small language models the prototype knows how to fetch and run.
MODELS: dict[str, ModelSpec] = {
    "phi3-gpu": ModelSpec(
        "phi3-gpu", "Phi-3-mini 4k (INT4, DirectML GPU)",
        "microsoft/Phi-3-mini-4k-instruct-onnx",
        "directml/directml-int4-awq-block-128", 2.1, 1.0,
    ),
    "phi3-cpu": ModelSpec(
        "phi3-cpu", "Phi-3-mini 4k (INT4, CPU)",
        "microsoft/Phi-3-mini-4k-instruct-onnx",
        "cpu_and_mobile/cpu-int4-rtn-block-32-acc-level-4", 2.7, 3.5,
    ),
    "phi4-gpu": ModelSpec(
        "phi4-gpu", "Phi-4-mini (INT4; ran on CPU with the DirectML build)",
        "microsoft/Phi-4-mini-instruct-onnx",
        "gpu/gpu-int4-rtn-block-32", 3.4, 1.0,
    ),
}

DEFAULT_MODEL = os.environ.get("CONTEXTOS_MODEL", "phi3-gpu")

# Context engine
SAMPLE_INTERVAL_S = float(os.environ.get("CONTEXTOS_SAMPLE_S", "2"))
RETENTION_S = 6 * 3600

# SLM service
IDLE_UNLOAD_S = float(os.environ.get("CONTEXTOS_IDLE_UNLOAD_S", "300"))

# Context governor
GOV_CPU_HIGH = 75.0       # system CPU %% that counts as contention
GOV_CPU_LOW = 45.0        # system CPU %% that counts as relief (hysteresis)
GOV_TRIGGER_SAMPLES = 2   # consecutive high samples before acting
GOV_RELEASE_SAMPLES = 3   # consecutive low samples before restoring
GOV_MIN_PROC_CPU = 10.0   # a background process must use this much CPU (one core = 100)

# Web dashboard
HOST = "127.0.0.1"
PORT = int(os.environ.get("CONTEXTOS_PORT", "8765"))
