"""Legacy CLI Model Providers for Codex and Antigravity (AGY).

Preserves 100% backwards-compatibility for existing environments.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from galaxy_core.providers.base import (
    GenerationRequest,
    GenerationResponse,
    ModelProvider,
)


class CodexCLIProvider(ModelProvider):
    """Executes commands via the local 'codex' CLI binary."""

    def __init__(self, name: str = "codex", default_model: str | None = None, **kwargs: Any) -> None:
        super().__init__(name=name, default_model=default_model, **kwargs)

    def health_check(self) -> bool:
        return shutil.which("codex") is not None

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        if not self.health_check():
            raise RuntimeError("provider binary not found: codex")

        model = request.model or self.default_model
        with tempfile.TemporaryDirectory(prefix="galaxy_codex_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            schema_path = tmp_path / "schema.json"
            final_path = tmp_path / "final.json"

            cmd = ["codex", "exec"]
            if model:
                cmd += ["--model", model]
            if request.schema:
                schema_path.write_text(json.dumps(request.schema), encoding="utf-8")
                cmd += ["--output-schema", str(schema_path), "--output-last-message", str(final_path)]
            cmd.append(request.prompt)

            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"codex exited with code {proc.returncode}: {proc.stderr}")

            content = proc.stdout
            json_data = None
            if final_path.exists():
                try:
                    json_data = json.loads(final_path.read_text(encoding="utf-8"))
                except Exception:
                    pass

            return GenerationResponse(
                content=content,
                json_data=json_data,
                model=model,
            )


class AntigravityCLIProvider(ModelProvider):
    """Executes commands via the local 'agy' CLI binary."""

    def __init__(self, name: str = "antigravity", default_model: str | None = None, **kwargs: Any) -> None:
        super().__init__(name=name, default_model=default_model, **kwargs)

    def health_check(self) -> bool:
        return shutil.which("agy") is not None

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        if not self.health_check():
            raise RuntimeError("provider binary not found: agy")

        model = request.model or self.default_model
        cmd = ["agy"]
        if model:
            cmd += ["--model", model, "--effort", "medium"]
        cmd += ["--dangerously-skip-permissions", "-p", request.prompt]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=request.timeout_seconds,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"agy exited with code {proc.returncode}: {proc.stderr}")

        return GenerationResponse(
            content=proc.stdout,
            model=model,
        )
