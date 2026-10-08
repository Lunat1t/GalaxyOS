"""Streaming adapter for the user's authenticated Codex CLI installation."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Iterator


@dataclass(frozen=True)
class RunRequest:
    prompt: str
    workspace: Path
    model: str | None = None


@dataclass(frozen=True)
class ProviderEvent:
    """Normalized provider output; usage is unknown unless Codex reports it."""

    kind: str
    message: str | None = None
    result: dict | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error_code: str | None = None


class CodexRun:
    def __init__(self, process: subprocess.Popen[str]):
        self._process = process
        self._finished = False

    def cancel(self) -> None:
        """Stop this provider process. The caller records the cancellation event."""
        if self._process.poll() is None:
            self._process.terminate()

    def events(self) -> Iterator[ProviderEvent]:
        try:
            for line in self._process.stdout or ():
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    yield ProviderEvent("error", error_code="invalid_provider_event")
                    continue
                event = _normalize_event(raw)
                if event is not None:
                    yield event
            return_code = self._process.wait()
            if return_code == 0:
                yield ProviderEvent("completed")
            elif return_code < 0:
                yield ProviderEvent("cancelled")
            else:
                # stderr may contain paths or sensitive provider diagnostics; don't persist it.
                yield ProviderEvent(
                    "error",
                    message=f"Codex CLI завершился с кодом {return_code} без подробного сообщения.",
                    error_code="provider_failed",
                )
        finally:
            if self._process.stdout:
                self._process.stdout.close()
            if self._process.poll() is None:
                self.cancel()
                self._process.wait()


def _normalize_event(raw: object) -> ProviderEvent | None:
    if not isinstance(raw, dict):
        return ProviderEvent("error", error_code="invalid_provider_event")
    kind = raw.get("type")
    if kind == "item.completed":
        item = raw.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                return ProviderEvent("message", message=text)
    if kind == "turn.completed":
        usage = raw.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        result = raw.get("last_message")
        return ProviderEvent(
            "result",
            result={"text": result} if isinstance(result, str) else {},
            input_tokens=_nonnegative_int(usage.get("input_tokens")),
            output_tokens=_nonnegative_int(usage.get("output_tokens")),
        )
    if kind == "error" or kind == "turn.failed":
        detail = raw.get("error")
        if not isinstance(detail, dict):
            detail = raw
        message = detail.get("message")
        code = detail.get("code") or detail.get("codexErrorInfo")
        return ProviderEvent(
            "error",
            message=message[:2000] if isinstance(message, str) else None,
            error_code=code[:100] if isinstance(code, str) else "provider_failed",
        )
    return None


def is_git_repository(workspace: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


class CodexRunProvider:
    """Starts Codex CLI with its existing user auth and workspace-write sandbox."""

    name = "codex"

    def __init__(self, executable: str | None = None):
        self.executable = executable or shutil.which("codex")

    def available(self) -> bool:
        return bool(self.executable and Path(self.executable).is_file())

    def start(self, request: RunRequest) -> CodexRun:
        workspace = request.workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError("Рабочая папка не найдена.")
        if not request.prompt.strip():
            raise ValueError("Запрос агента не должен быть пустым.")
        executable = self.executable
        if not executable:
            raise RuntimeError("Codex CLI не установлен или не найден.")
        command = [executable, "exec", "--json", "--ephemeral"]
        command += ["--worktree"] if is_git_repository(workspace) else ["--skip-git-repo-check"]
        command += ["--sandbox", "workspace-write", "--cd", str(workspace)]
        if request.model:
            command += ["--model", request.model]
        command.append(request.prompt)
        process = subprocess.Popen(
            command,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=os.environ.copy(),  # reuse user CLI auth without passing it through Galaxy data
        )
        return CodexRun(process)
