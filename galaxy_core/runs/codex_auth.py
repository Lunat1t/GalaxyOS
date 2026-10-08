"""Manage Codex CLI account status without exposing its credentials to Galaxy."""
from __future__ import annotations

import shutil
import subprocess
import threading


class CodexLogin:
    """Use Codex CLI's supported browser login and status commands."""

    def __init__(self, executable: str | None = None):
        self.executable = executable or shutil.which("codex")
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None

    def status(self) -> dict[str, str | bool]:
        if not self.executable:
            return {"state": "unavailable", "connected": False}
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return {"state": "connecting", "connected": False}
            self._process = None
        try:
            result = subprocess.run(
                [self.executable, "login", "status"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"state": "unavailable", "connected": False}
        if result.returncode == 0:
            return {"state": "connected", "connected": True}
        return {"state": "disconnected", "connected": False}

    def login(self) -> dict[str, str | bool]:
        if not self.executable:
            raise RuntimeError("Codex CLI не установлен или не найден.")
        current = self.status()
        if current["connected"]:
            return current
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return {"state": "connecting", "connected": False}
            try:
                self._process = subprocess.Popen(
                    [self.executable, "login"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
            except OSError as exc:
                raise RuntimeError("Не удалось открыть вход Codex в браузере.") from exc
        return {"state": "connecting", "connected": False}
