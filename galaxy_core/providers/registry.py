"""Provider Registry for Galaxy Agent Runtime.

Manages instantiation and lifecycle of model backends.
"""

from __future__ import annotations

from typing import Any, Callable

from galaxy_core.providers.base import ModelProvider
from galaxy_core.providers.http.openai_compatible import OpenAICompatibleProvider
from galaxy_core.providers.legacy_cli.adapters import (
    AntigravityCLIProvider,
    CodexCLIProvider,
)
from galaxy_core.providers.local.ollama import OllamaProvider


class FakeProvider(ModelProvider):
    """Deterministic fake provider for fast offline unit tests."""

    def __init__(self, name: str = "fake", response_text: str = '{"status": "ok"}', **kwargs: Any) -> None:
        super().__init__(name=name, **kwargs)
        self.response_text = response_text

    def health_check(self) -> bool:
        return True

    def generate(self, request: Any) -> Any:
        from galaxy_core.providers.base import GenerationResponse
        return GenerationResponse(content=self.response_text)


class ProviderRegistry:
    """Registry and factory for Galaxy model execution backends."""

    _factories: dict[str, Callable[..., ModelProvider]] = {}
    _instances: dict[str, ModelProvider] = {}

    @classmethod
    def register(cls, name: str, factory: Callable[..., ModelProvider]) -> None:
        cls._factories[name.lower()] = factory

    @classmethod
    def get(cls, name: str, **kwargs: Any) -> ModelProvider:
        key = name.lower()
        if key not in cls._factories:
            # Fallback for generic openai-compatible
            if "openai" in key or "deepseek" in key or "openrouter" in key or "groq" in key:
                return OpenAICompatibleProvider(name=name, **kwargs)
            raise ValueError(f"Unknown provider: {name!r}. Registered: {list(cls._factories.keys())}")
        return cls._factories[key](**kwargs)

    @classmethod
    def list_providers(cls) -> list[str]:
        return sorted(list(cls._factories.keys()))


# Register built-in providers
ProviderRegistry.register("ollama", lambda **kw: OllamaProvider(**kw))
ProviderRegistry.register("openai-compatible", lambda **kw: OpenAICompatibleProvider(**kw))
ProviderRegistry.register(
    "deepseek",
    lambda **kw: OpenAICompatibleProvider(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-coder",
        api_key_env="DEEPSEEK_API_KEY",
        input_cost_per_million=0.14,
        output_cost_per_million=0.28,
        **kw,
    ),
)
ProviderRegistry.register(
    "openrouter",
    lambda **kw: OpenAICompatibleProvider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        default_model="qwen/qwen-2.5-coder-32b-instruct",
        api_key_env="OPENROUTER_API_KEY",
        **kw,
    ),
)
ProviderRegistry.register("codex", lambda **kw: CodexCLIProvider(**kw))
ProviderRegistry.register("antigravity", lambda **kw: AntigravityCLIProvider(**kw))
ProviderRegistry.register("fake", lambda **kw: FakeProvider(**kw))
