"""Download a small language model from Hugging Face into the data folder.

Only model weights, tokenizer and configuration data are fetched. Python files
shipped in the model repositories are skipped, because nothing here needs them.
"""
from __future__ import annotations

import sys
from pathlib import Path

import requests

from .config import MODELS, data_dir

SKIP_SUFFIXES = (".py", ".md")


def model_path(key: str) -> Path:
    return data_dir() / "models" / key


def is_installed(key: str) -> bool:
    return (model_path(key) / "genai_config.json").exists() and any(model_path(key).glob("*.onnx.data"))


def _download(url: str, dest: Path, expected: int) -> None:
    if dest.exists() and dest.stat().st_size == expected:
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    start = tmp.stat().st_size if tmp.exists() else 0
    headers = {"Range": f"bytes={start}-"} if start else {}
    with requests.get(url, stream=True, timeout=60, headers=headers) as resp:
        resp.raise_for_status()
        mode = "ab" if resp.status_code == 206 else "wb"
        with open(tmp, mode) as handle:
            for chunk in resp.iter_content(1 << 20):
                handle.write(chunk)
    if tmp.stat().st_size != expected:
        raise IOError(f"incomplete download for {dest.name}: {tmp.stat().st_size} of {expected} bytes")
    tmp.replace(dest)


def install(key: str) -> Path:
    spec = MODELS[key]
    target = model_path(key)
    target.mkdir(parents=True, exist_ok=True)
    api = f"https://huggingface.co/api/models/{spec.repo}/tree/main/{spec.subfolder}?recursive=true"
    listing = requests.get(api, timeout=60)
    listing.raise_for_status()
    for item in listing.json():
        if item["type"] != "file" or item["path"].endswith(SKIP_SUFFIXES):
            continue
        name = item["path"].rsplit("/", 1)[-1]
        print(f"  {name} ({item['size'] / 1e6:.1f} MB)", flush=True)
        url = f"https://huggingface.co/{spec.repo}/resolve/main/{item['path']}"
        _download(url, target / name, item["size"])
    print(f"Installed {spec.label} in {target}", flush=True)
    return target


if __name__ == "__main__":
    for model_key in sys.argv[1:]:
        install(model_key)
