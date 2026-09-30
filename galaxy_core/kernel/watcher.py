"""Portable polling watcher with debounce for the kernel projector."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from galaxy_core.world.scanner import ProjectScanner

from .projector import KernelProjector


class KernelWatcher:
    def __init__(self, projector: KernelProjector, *, interval: float = 1.0,
                 debounce: float = 0.5):
        if interval <= 0 or debounce < 0:
            raise ValueError("interval must be positive and debounce nonnegative")
        self.projector = projector
        self.interval = interval
        self.debounce = debounce
        self._signature: dict[str, tuple[int, int, int, int] | None] | None = None
        self._pending_at: float | None = None

    def _stamp(self) -> dict[str, tuple[int, int, int, int] | None]:
        root = self.projector.project_root
        result: dict[str, tuple[int, int, int, int] | None] = {}
        for path in ProjectScanner(root, self.projector.project)._files():
            rel = path.relative_to(root).as_posix()
            try:
                stat = path.stat()
                result[rel] = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
            except OSError:
                result[rel] = None
        return result

    def start(self, *, now: float | None = None) -> dict[str, Any]:
        before = self._stamp()
        result = self.projector.observe()
        after = self._stamp()
        self._signature = after
        self._pending_at = (time.monotonic() if now is None else now) if after != before else None
        return result

    def tick(self, *, now: float | None = None) -> dict[str, Any] | None:
        if self._signature is None:
            raise RuntimeError("start the watcher before ticking")
        now = time.monotonic() if now is None else now
        observed = self._stamp()
        if observed != self._signature:
            self._signature = observed
            self._pending_at = now
            return None
        if self._pending_at is None or now - self._pending_at < self.debounce:
            return None
        # Keep the pending marker if projection raises, so a later tick retries.
        result = self.projector.observe()
        after = self._stamp()
        self._signature = after
        self._pending_at = now if after != observed else None
        return result

    def run(self, *, stop_event: threading.Event | None = None,
            on_update: Callable[[dict[str, Any]], None] | None = None,
            on_error: Callable[[Exception], None] | None = None) -> None:
        stop = stop_event or threading.Event()
        first = self.start()
        if on_update:
            on_update(first)
        while not stop.wait(self.interval):
            try:
                result = self.tick()
                if result is not None and on_update:
                    on_update(result)
            except Exception as exc:
                if on_error is None:
                    raise
                on_error(exc)
