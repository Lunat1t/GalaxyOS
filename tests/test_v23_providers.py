"""Tests for Galaxy Agent Runtime Providers and Model Decoupling."""

import json
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

from galaxy_core.providers.base import (
    GenerationRequest,
    GenerationResponse,
    ToolDefinition,
)
from galaxy_core.providers.registry import ProviderRegistry, FakeProvider
from galaxy_core.providers.http.openai_compatible import OpenAICompatibleProvider
from galaxy_core.engine.autonomy import ModelProfile, AdaptiveModelRouter, WorkNode


class MockOpenAIServer(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers["Content-Length"])
        post_data = self.rfile.read(content_length)
        payload = json.loads(post_data.decode("utf-8"))

        response = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 123456789,
            "model": payload.get("model", "qwen2.5-coder"),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": '{"status": "success", "analysis": "all clear"}',
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 42,
                "completion_tokens": 18,
                "total_tokens": 60,
            },
        }

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode("utf-8"))

    def do_GET(self):
        # Health check endpoint
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"data": [{"id": "qwen2.5-coder"}]}).encode("utf-8"))

    def log_message(self, format, *args):
        pass


class ProviderRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), MockOpenAIServer)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_registry_contains_core_backends(self):
        providers = ProviderRegistry.list_providers()
        self.assertIn("ollama", providers)
        self.assertIn("openai-compatible", providers)
        self.assertIn("deepseek", providers)
        self.assertIn("openrouter", providers)
        self.assertIn("codex", providers)
        self.assertIn("antigravity", providers)

    def test_fake_provider_generation(self):
        fake = ProviderRegistry.get("fake")
        resp = fake.generate(GenerationRequest(prompt="test"))
        self.assertIsInstance(resp, GenerationResponse)
        self.assertEqual(resp.parsed_json(), {"status": "ok"})

    def test_openai_compatible_http_call_and_cost(self):
        base_url = f"http://127.0.0.1:{self.port}/v1"
        provider = OpenAICompatibleProvider(
            name="test-mock",
            base_url=base_url,
            default_model="qwen2.5-coder:14b",
            input_cost_per_million=0.14,
            output_cost_per_million=0.28,
        )

        self.assertTrue(provider.health_check())

        req = GenerationRequest(
            prompt="Analyze code security",
            model="qwen2.5-coder:14b",
            temperature=0.1,
            schema={"type": "object", "properties": {"status": {"type": "string"}}},
        )
        resp = provider.generate(req)

        self.assertEqual(resp.prompt_tokens, 42)
        self.assertEqual(resp.completion_tokens, 18)
        self.assertGreater(resp.cost_usd, 0.0)
        self.assertGreater(resp.latency_ms, 0.0)
        data = resp.parsed_json()
        self.assertEqual(data["status"], "success")

    def test_model_profiles_allow_local_and_byok(self):
        local_p = ModelProfile(
            name="qwen-local",
            provider="ollama",
            model="qwen2.5-coder:32b",
            capabilities=("general", "implementation"),
            quality=0.88,
            input_cost_per_million=0.0,
            output_cost_per_million=0.0,
        )
        local_p.validate()

        deepseek_p = ModelProfile(
            name="deepseek-cheap",
            provider="deepseek",
            model="deepseek-coder",
            capabilities=("planning", "review"),
            quality=0.92,
            input_cost_per_million=0.14,
            output_cost_per_million=0.28,
            base_url="https://api.deepseek.com/v1",
            api_key_env="DEEPSEEK_API_KEY",
        )
        deepseek_p.validate()

        router = AdaptiveModelRouter([local_p, deepseek_p])
        node = WorkNode(
            id="node-1",
            title="Implement real-time sync",
            role="Earth",
            capability="implementation",
            objective="Write Yjs adapter",
            estimated_input_tokens=1000,
            estimated_output_tokens=500,
        )

        route = router.select(node, remaining_cost=10.0)
        self.assertEqual(route.profile, "qwen-local")
        self.assertEqual(route.provider, "ollama")
        self.assertEqual(route.estimated_cost_usd, 0.0)


if __name__ == "__main__":
    unittest.main()
