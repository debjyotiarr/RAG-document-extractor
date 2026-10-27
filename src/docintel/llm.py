"""Provider-agnostic LLM client with Ollama and Anthropic drivers (BLUEPRINT §4).

Implemented so far: the Ollama driver with JSON-schema-constrained output, used
by classification and field extraction (M2). The Anthropic driver and tool
calling arrive with the agent in M4.
"""

from __future__ import annotations

import json
from typing import Protocol

from docintel.config import Config


class LLMUnavailable(Exception):
    """The model cannot be reached or is not installed. Work needing it is postponed, not failed."""


class LLMError(Exception):
    """The model answered, but not with usable output."""


class LLMClient(Protocol):
    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        """Return the model's answer as a dict conforming to the JSON `schema`."""
        ...


class OllamaClient:
    """A local model served by Ollama. Deterministic settings (temperature 0)."""

    def __init__(self, model: str, base_url: str, *, timeout: float = 600, num_ctx: int = 8192):
        self.model = model
        self.base_url = base_url
        self.timeout = timeout  # generous: a 7B model on CPU is slow
        self.num_ctx = num_ctx
        self._unavailable: str | None = None  # once unreachable, fail fast for the rest of the run

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        import ollama  # imported lazily so the package is only needed when an LLM call is made

        if self._unavailable:
            raise LLMUnavailable(self._unavailable)
        client = ollama.Client(host=self.base_url, timeout=self.timeout)
        try:
            response = client.chat(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                format=schema,
                options={"temperature": 0, "num_ctx": self.num_ctx},
            )
        except ConnectionError as exc:
            self._unavailable = f"cannot reach Ollama at {self.base_url} (is it running?): {exc}"
            raise LLMUnavailable(self._unavailable) from exc
        except ollama.ResponseError as exc:
            if exc.status_code == 404:
                self._unavailable = f"model {self.model} is not installed; run: ollama pull {self.model}"
                raise LLMUnavailable(self._unavailable) from exc
            raise LLMError(f"Ollama error: {exc}") from exc
        content = response.message.content or ""
        try:
            answer = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"model returned invalid JSON: {content[:200]!r}") from exc
        if not isinstance(answer, dict):
            raise LLMError(f"model returned JSON that is not an object: {content[:200]!r}")
        return answer


class _NotYetAvailable:
    def __init__(self, reason: str):
        self.reason = reason

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        raise LLMUnavailable(self.reason)


def make_client(config: Config) -> LLMClient:
    if config.llm.provider == "ollama":
        return OllamaClient(config.llm.ollama.model, config.llm.ollama.base_url)
    return _NotYetAvailable("the anthropic driver is not implemented yet (milestone M4); use provider: ollama")
