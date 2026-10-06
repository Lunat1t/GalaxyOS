"""Universal OpenAI-compatible HTTP Model Provider for Galaxy.

Connects Galaxy Agent Runtime to any OpenAI-standard endpoint:
- Local ($0): Ollama, vLLM, llama.cpp server, LocalAI, LM Studio
- Cloud BYOK: DeepSeek, Groq, OpenRouter, Mistral, OpenAI
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from galaxy_core.providers.base import (
    GenerationRequest,
    GenerationResponse,
    ModelProvider,
)


class OpenAICompatibleProvider(ModelProvider):
    """Universal HTTP provider speaking the OpenAI chat completions REST protocol."""

    def __init__(
        self,
        name: str = "openai-compatible",
        base_url: str = "http://localhost:11434/v1",
        default_model: str = "qwen2.5-coder:14b",
        api_key: str | None = None,
        api_key_env: str | None = None,
        input_cost_per_million: float = 0.0,
        output_cost_per_million: float = 0.0,
        context_window: int = 32768,
        **kwargs: Any,
    ) -> None:
        super().__init__(name=name, default_model=default_model, **kwargs)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_key_env = api_key_env
        self._input_cost = input_cost_per_million
        self._output_cost = output_cost_per_million
        self._context_window = context_window

    def _get_api_key(self) -> str | None:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env)
        # Default fallbacks
        for env_var in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"):
            val = os.environ.get(env_var)
            if val:
                return val
        return None

    def _get_opener(self) -> urllib.request.OpenerDirector:
        # Avoid routing local addresses (Ollama, vLLM, mock test servers) through external HTTP proxy
        if "127.0.0.1" in self.base_url or "localhost" in self.base_url:
            return urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener()

    def health_check(self) -> bool:
        """Check if base_url is reachable."""
        url = f"{self.base_url}/models"
        req = urllib.request.Request(url, headers={"User-Agent": "Galaxy-Agent-Runtime/2.1"})
        api_key = self._get_api_key()
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        try:
            opener = self._get_opener()
            with opener.open(req, timeout=3.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        model = request.model or self.default_model or "default"
        endpoint = f"{self.base_url}/chat/completions"

        messages: list[dict[str, str]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.prompt})

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": request.temperature,
        }
        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens

        # JSON / Structured outputs
        if request.schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_response",
                    "schema": request.schema,
                },
            }

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Galaxy-Agent-Runtime/2.1",
        }
        api_key = self._get_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if request.extra_headers:
            headers.update(request.extra_headers)

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")

        start_t = time.perf_counter()
        opener = self._get_opener()
        try:
            with opener.open(req, timeout=request.timeout_seconds) as resp:
                latency_ms = (time.perf_counter() - start_t) * 1000.0
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} from {endpoint}: {err_body}") from exc
        except Exception as exc:
            raise RuntimeError(f"Network error calling {endpoint}: {exc}") from exc

        choices = body.get("choices") or []
        if not choices:
            raise ValueError(f"empty response choices from {endpoint}: {body}")

        message_content = choices[0].get("message", {}).get("content", "")
        usage = body.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)

        cost_usd = (
            (prompt_tokens * self._input_cost + completion_tokens * self._output_cost)
            / 1_000_000
        )

        return GenerationResponse(
            content=message_content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            model=model,
            raw_response=body,
        )

    @property
    def context_limit(self) -> int:
        return self._context_window

    @property
    def cost_per_million(self) -> tuple[float, float]:
        return (self._input_cost, self._output_cost)
