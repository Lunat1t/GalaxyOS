"""Galaxy Agent Runtime: Unified Model Provider Interface.

Decouples Galaxy core (memory, DAG planner, agents) from concrete model backends
(Ollama, vLLM, DeepSeek, OpenRouter, Qwen, Devstral, Gemma, Codex, Antigravity).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class GenerationRequest:
    prompt: str
    system_prompt: str | None = None
    model: str | None = None
    temperature: float = 0.2
    max_tokens: int | None = None
    schema: dict[str, Any] | None = None
    tools: list[ToolDefinition] = field(default_factory=list)
    extra_headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 60.0


@dataclass
class GenerationResponse:
    content: str
    json_data: dict[str, Any] | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    model: str | None = None
    raw_response: dict[str, Any] | None = None

    def parsed_json(self) -> dict[str, Any]:
        if self.json_data is not None:
            return self.json_data
        try:
            return json.loads(self.content)
        except Exception:
            text = self.content.strip()
            if "```json" in text:
                text = text.split("```json", 1)[1].split("```", 1)[0].strip()
            elif "```" in text:
                text = text.split("```", 1)[1].split("```", 1)[0].strip()
            return json.loads(text)


class ModelProvider(ABC):
    """Abstract base class for all Galaxy model execution backends."""

    def __init__(self, name: str, default_model: str | None = None, **kwargs: Any) -> None:
        self.name = name
        self.default_model = default_model
        self.config = kwargs

    @abstractmethod
    def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Execute a synchronous generation request."""
        pass

    @abstractmethod
    def health_check(self) -> bool:
        """Check if provider backend is reachable."""
        pass

    @property
    def context_limit(self) -> int:
        """Maximum context window in tokens."""
        return 32768

    @property
    def cost_per_million(self) -> tuple[float, float]:
        """Cost in USD per 1M (input_tokens, output_tokens)."""
        return (0.0, 0.0)
