"""Inference backends. All of them produce text from a system and a user prompt."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import requests


@dataclass
class Completion:
    text: str
    prompt_tokens: int = 0
    output_tokens: int = 0
    first_token_s: float = 0.0
    total_s: float = 0.0

    @property
    def tokens_per_s(self) -> float:
        gen_time = self.total_s - self.first_token_s
        return (self.output_tokens - 1) / gen_time if gen_time > 0 and self.output_tokens > 1 else 0.0


class Backend(Protocol):
    name: str
    def load(self) -> None: ...
    def unload(self) -> None: ...
    def loaded(self) -> bool: ...
    def generate(self, system: str, user: str, seed: str = "", max_tokens: int = 96,
                 stop: Callable[[str], bool] | None = None) -> Completion: ...


class OrtGenAIBackend:
    """Runs a quantized ONNX model in-process with ONNX Runtime GenAI (CPU or DirectML)."""

    def __init__(self, model_dir: str, name: str = "ort-genai") -> None:
        self.model_dir, self.name = str(model_dir), name
        self._model = self._tok = None

    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        if self._model is None:
            import onnxruntime_genai as og
            self._model = og.Model(self.model_dir)
            self._tok = og.Tokenizer(self._model)

    def unload(self) -> None:
        self._model = self._tok = None

    def generate(self, system, user, seed="", max_tokens=96, stop=None) -> Completion:
        import onnxruntime_genai as og
        self.load()
        prompt = f"<|system|>\n{system}<|end|>\n<|user|>\n{user}<|end|>\n<|assistant|>\n{seed}"
        ids = self._tok.encode(prompt)
        params = og.GeneratorParams(self._model)
        params.set_search_options(max_length=len(ids) + max_tokens, do_sample=False)
        started = time.perf_counter()
        generator = og.Generator(self._model, params)
        generator.append_tokens(ids)
        stream, text, count, first = self._tok.create_stream(), seed, 0, 0.0
        while not generator.is_done():
            generator.generate_next_token()
            piece = stream.decode(generator.get_next_tokens()[0])
            count += 1
            if count == 1:
                first = time.perf_counter() - started
            text += piece
            if stop and stop(text):
                break
        del generator
        return Completion(text, len(ids), count, first, time.perf_counter() - started)


class HttpBackend:
    """Any OpenAI-compatible local server, for example Ollama or llama-server."""

    def __init__(self, url: str, model: str, name: str = "http") -> None:
        self.url, self.model, self.name = url.rstrip("/"), model, name

    def loaded(self) -> bool:
        return True

    def load(self) -> None:
        pass

    def unload(self) -> None:
        pass

    def generate(self, system, user, seed="", max_tokens=96, stop=None) -> Completion:
        started = time.perf_counter()
        resp = requests.post(f"{self.url}/v1/chat/completions", timeout=120, json={
            "model": self.model, "temperature": 0, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        })
        resp.raise_for_status()
        body = resp.json()
        usage = body.get("usage", {})
        elapsed = time.perf_counter() - started
        return Completion(body["choices"][0]["message"]["content"], usage.get("prompt_tokens", 0),
                          usage.get("completion_tokens", 0), elapsed, elapsed)


class ScriptedBackend:
    """Deterministic stand-in used by the unit tests and on machines without a model."""

    name = "scripted"

    def __init__(self, reply: Callable[[str, str], str] | str) -> None:
        self.reply = reply
        self._loaded = False
        self.calls = 0

    def loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def generate(self, system, user, seed="", max_tokens=96, stop=None) -> Completion:
        self.load()
        self.calls += 1
        text = self.reply(system, user) if callable(self.reply) else self.reply
        return Completion(text if isinstance(text, str) else json.dumps(text), 10, 10, 0.001, 0.002)
