"""Live LLM Integration Tests for Galaxy Agent Runtime.

Tests real model inference (Ollama, DeepSeek, OpenRouter, OpenAI) when
available in the runtime environment. Automatically discovers reachable
endpoints or skips gracefully in offline/sandboxed CI environments.
"""

from __future__ import annotations

import os
import unittest

from galaxy_core.providers.base import GenerationRequest, ModelProvider
from galaxy_core.providers.http.openai_compatible import OpenAICompatibleProvider
from galaxy_core.providers.local.ollama import OllamaProvider
from galaxy_core.providers.registry import ProviderRegistry


def detect_live_provider() -> tuple[ModelProvider | None, str]:
    """Detect if any live LLM backend is accessible right now."""
    # 1. Check OpenRouter / DeepSeek / OpenAI API keys
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    if openrouter_key:
        p = OpenAICompatibleProvider(
            name="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key=openrouter_key,
            default_model=os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct"),
        )
        return p, "OpenRouter Cloud"

    deepseek_key = os.getenv("DEEPSEEK_API_KEY")
    if deepseek_key:
        p = OpenAICompatibleProvider(
            name="deepseek",
            base_url="https://api.deepseek.com/v1",
            api_key=deepseek_key,
            default_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        )
        return p, "DeepSeek Cloud"

    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        p = OpenAICompatibleProvider(
            name="openai",
            base_url="https://api.openai.com/v1",
            api_key=openai_key,
            default_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        )
        return p, "OpenAI API"

    # 2. Check local Ollama
    ollama_host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    ollama = OllamaProvider(base_url=ollama_host, default_model="qwen2.5-coder:7b")
    try:
        if ollama.health_check():
            return ollama, f"Local Ollama at {ollama_host}"
    except Exception:
        pass

    return None, "No reachable live LLM endpoint found"


class LiveLLMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider, cls.provider_desc = detect_live_provider()

    def test_live_llm_status_reported(self):
        """Always reports the live LLM detection status."""
        self.assertIsNotNone(self.provider_desc)

    def test_live_llm_inference_e2e(self):
        """If a live provider is available, executes real inference."""
        if not self.provider:
            self.skipTest(
                f"Skipping live inference: {self.provider_desc}. Set DEEPSEEK_API_KEY, "
                "OPENROUTER_API_KEY, or run Ollama locally to enable live LLM tests."
            )

        req = GenerationRequest(
            prompt="Reply with the exact word 'GALAXY_ACK' and nothing else.",
            temperature=0.0,
            max_tokens=20,
        )
        resp = self.provider.generate(req)
        self.assertTrue(bool(resp.content.strip()))
        self.assertIn("GALAXY_ACK", resp.content.upper())


if __name__ == "__main__":
    unittest.main()
