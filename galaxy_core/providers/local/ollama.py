"""Ollama Local Model Provider for Galaxy.

Provides $0 local inference with zero token limits:
- Qwen 2.5 Coder / Qwen 3 Coder
- Devstral Small / Codestral
- Gemma 2 / CodeGemma
- DeepSeek-Coder
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

from galaxy_core.providers.http.openai_compatible import OpenAICompatibleProvider


class OllamaProvider(OpenAICompatibleProvider):
    """Local Ollama backend for on-device model execution."""

    def __init__(
        self,
        name: str = "ollama",
        base_url: str = "http://localhost:11434/v1",
        default_model: str = "qwen2.5-coder:14b",
        **kwargs: Any,
    ) -> None:
        super().__init__(
            name=name,
            base_url=base_url,
            default_model=default_model,
            input_cost_per_million=0.0,
            output_cost_per_million=0.0,
            context_window=kwargs.get("context_window", 65536),
            **kwargs,
        )

    def list_installed_models(self) -> list[str]:
        """Fetch list of local models downloaded in Ollama."""
        ollama_native_url = self.base_url.rsplit("/v1", 1)[0] + "/api/tags"
        try:
            req = urllib.request.Request(ollama_native_url, headers={"User-Agent": "Galaxy/2.1"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                models = data.get("models") or []
                return [m.get("name") for m in models if "name" in m]
        except Exception:
            return []
