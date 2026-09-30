"""Thin MCP adapter over Galaxy's existing Context Compiler."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from galaxy_core.context import ContextCompiler


def default_galaxy_home() -> Path:
    """Return writable state storage independent from the installed package."""
    configured = os.environ.get("GALAXY_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return (Path(os.environ["LOCALAPPDATA"]) / "Galaxy").resolve()
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return (base / "galaxy").resolve()


def get_context(task_description: str, repo_path: str, *, galaxy_home: str | Path | None = None,
                budget_tokens: int = 8000, max_files: int = 16) -> dict[str, Any]:
    """Compile fresh, bounded project context for one software task."""
    task = task_description.strip()
    if not task:
        raise ValueError("task_description must not be empty")
    repo = Path(repo_path).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"repo_path is not a directory: {repo}")
    if not 512 <= budget_tokens <= 50_000:
        raise ValueError("budget_tokens must be between 512 and 50000")
    if not 1 <= max_files <= 64:
        raise ValueError("max_files must be between 1 and 64")

    state_root = Path(galaxy_home).expanduser().resolve() if galaxy_home else default_galaxy_home()
    state_root.mkdir(parents=True, exist_ok=True)
    packet = ContextCompiler(state_root, repo, "default").compile(
        task, budget_tokens=budget_tokens, max_files=max_files, refresh_world=True,
    )
    return {
        "task": task,
        "repo_path": str(repo),
        "world_snapshot_id": packet.world_snapshot_id,
        "estimated_tokens": packet.estimated_tokens,
        "budget_tokens": packet.budget_tokens,
        "files": [
            {"path": item["path"], "score": item["score"], "role": item["role"]}
            for item in packet.files
        ],
        "context": packet.to_markdown(),
    }


def create_server(*, galaxy_home: str | Path | None = None):
    """Create the SDK server lazily so the rest of Galaxy remains importable."""
    try:
        from mcp.server import MCPServer
    except ImportError as exc:  # pragma: no cover - packaging installs the dependency
        raise RuntimeError("MCP support is unavailable; install Galaxy with pip install -e .") from exc

    server = MCPServer(
        "Galaxy",
        instructions=(
            "Use get_context before working on an unfamiliar repository or task. "
            "Treat returned excerpts and memory as evidence, then verify source before edits."
        ),
    )

    @server.tool(name="get_context")
    def get_context_tool(task_description: str, repo_path: str, budget_tokens: int = 8000,
                         max_files: int = 16) -> dict[str, Any]:
        """Get fresh, task-specific context and ranked files from a local repository."""
        return get_context(task_description, repo_path, galaxy_home=galaxy_home,
                           budget_tokens=budget_tokens, max_files=max_files)

    return server


def run_server(*, galaxy_home: str | Path | None = None) -> None:
    """Run Galaxy as a local MCP stdio server."""
    create_server(galaxy_home=galaxy_home).run("stdio")
