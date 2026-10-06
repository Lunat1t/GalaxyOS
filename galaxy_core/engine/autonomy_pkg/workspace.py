"""Workspace isolation and artifact promotion for autonomous DAG execution."""

from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from galaxy_core.engine.autonomy_pkg.models import (
    GENERATED_DIRS,
    NodeResult,
    WorkNode,
    _safe_rel,
)


def _file_hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _clone_or_copy(source: str, destination: str) -> str:
    """Prefer a copy-on-write reflink and safely fall back to a normal copy."""
    if os.name != "nt":
        try:
            import fcntl
            with open(source, "rb") as src, open(destination, "wb") as dst:
                fcntl.ioctl(dst.fileno(), 0x40049409, src.fileno())
            shutil.copystat(source, destination)
            return destination
        except (OSError, ImportError):
            Path(destination).unlink(missing_ok=True)
    return shutil.copy2(source, destination)


class NodeWorkspace:
    """Disposable node workspace with conflict-aware artifact promotion."""

    def __init__(self, root: str | Path, node: WorkNode):
        self.root = Path(root).resolve()
        self.node = node
        self.temp: tempfile.TemporaryDirectory[str] | None = None
        self.path: Path | None = None
        self.baseline: dict[str, str | None] = {}

    def __enter__(self) -> "NodeWorkspace":
        self.temp = tempfile.TemporaryDirectory(prefix=f"galaxy-v2-{self.node.id}-")
        self.path = Path(self.temp.name) / "workspace"

        def ignore(directory: str, names: list[str]) -> list[str]:
            return [
                name for name in names
                if name in GENERATED_DIRS
                or name.startswith(".env")
                or name.endswith((".pyc", ".secret"))
                or (Path(directory) / name).is_symlink()
            ]

        shutil.copytree(self.root, self.path, ignore=ignore, copy_function=_clone_or_copy)
        for scope in self.node.write_scopes:
            for candidate in self.root.glob(scope):
                if candidate.is_file():
                    rel = candidate.relative_to(self.root).as_posix()
                    self.baseline[rel] = _file_hash(candidate)
        return self

    def promote(self, result: NodeResult) -> list[str]:
        if self.node.risk != "write":
            return []
        if not result.artifacts and not result.deleted:
            raise ValueError(f"{self.node.id}: successful write node declared no artifacts")
        promoted: list[str] = []
        for rel in [*result.artifacts, *result.deleted]:
            rel = _safe_rel(rel)
            if not any(fnmatch.fnmatch(rel, scope) for scope in self.node.write_scopes):
                raise ValueError(f"{self.node.id}: artifact outside write scope: {rel}")
            canonical = self.root / rel
            expected = self.baseline.get(rel)
            current = _file_hash(canonical) if canonical.is_file() else None
            if current != expected:
                raise RuntimeError(f"write conflict while promoting {rel}")
        for rel in result.artifacts:
            rel = _safe_rel(rel)
            source = self.path / rel  # type: ignore[operator]
            if not source.is_file() or source.is_symlink():
                raise ValueError(f"declared artifact is missing: {rel}")
            destination = self.root / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=".galaxy-promote-", dir=destination.parent)
            os.close(fd)
            try:
                shutil.copy2(source, temp_name)
                os.replace(temp_name, destination)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            promoted.append(rel)
        for rel in result.deleted:
            destination = self.root / _safe_rel(rel)
            if destination.exists():
                destination.unlink()
            promoted.append(rel)
        return promoted

    def __exit__(self, *_: Any) -> None:
        if self.temp:
            self.temp.cleanup()
