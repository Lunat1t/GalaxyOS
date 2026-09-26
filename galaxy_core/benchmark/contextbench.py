"""Galaxy retrieval adapter for ContextBench's independent dataset and evaluator."""
from __future__ import annotations

import io
import json
import math
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from typing import Any

from galaxy_core.attention import AttentionEngine
from galaxy_core.world.scanner import ProjectScanner


def _rows(path: Path) -> list[dict[str, Any]]:
    keys = ("instance_id", "repo", "base_commit", "problem_statement")
    if path.suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise RuntimeError("Parquet input requires pyarrow: pip install pyarrow") from exc
        names = set(pq.read_schema(path).names)
        if not set(keys) <= names:
            raise ValueError(f"missing ContextBench columns: {sorted(set(keys) - names)}")
        # Gold context, hints and solution patch are never loaded by Galaxy.
        return pq.read_table(path, columns=list(keys)).to_pylist()
    if path.suffix == ".jsonl":
        with path.open(encoding="utf-8") as stream:
            return [{key: row.get(key) for key in keys}
                    for line in stream if line.strip() for row in [json.loads(line)]]
    raise ValueError("ContextBench dataset must be .parquet or .jsonl")


def _snapshot(repo: Path, commit: str, destination: Path) -> None:
    if len(commit) != 40 or any(c not in "0123456789abcdefABCDEF" for c in commit):
        raise ValueError("base_commit must be a 40-character Git SHA")
    proc = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode(errors="replace").strip())
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as archive:
        root = destination.resolve()
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if ((target != root and root not in target.parents)
                    or not (member.isfile() or member.isdir())):
                raise ValueError(f"unsafe archive member: {member.name}")
        try:
            archive.extractall(destination, filter="data")
        except TypeError:
            archive.extractall(destination)


def _repo_path(repo_name: str, repos_dir: Path | None, repo: Path | None) -> Path:
    if repo is not None:
        return repo
    parts = repo_name.split("/")
    if len(parts) != 2 or any(p in {"", ".", ".."} or "\\" in p for p in parts):
        raise ValueError(f"invalid repository name: {repo_name}")
    if repos_dir is None:
        raise ValueError("provide --repo for one repository or --repos-dir for a collection")
    return repos_dir / parts[0] / parts[1]


def export_predictions(dataset: str | Path, output: str | Path, *, repo: str | Path | None = None,
                       repos_dir: str | Path | None = None, limit: int = 0,
                       token_budget: int = 10_000, max_files: int = 20) -> dict[str, Any]:
    """Write ContextBench prediction JSONL; never inspect gold labels."""
    if token_budget < 1 or max_files < 1 or limit < 0:
        raise ValueError("token_budget and max_files must be positive; limit must be nonnegative")
    rows = _rows(Path(dataset))
    if limit:
        rows = rows[:limit]
    repo_path = Path(repo).resolve() if repo is not None else None
    repos_path = Path(repos_dir).resolve() if repos_dir is not None else None
    if repo_path is None and repos_path is None:
        raise ValueError("provide repo or repos_dir")
    predictions: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        instance_id = str(row.get("instance_id") or "")
        try:
            if not instance_id or instance_id in seen:
                raise ValueError("missing or duplicate instance_id")
            seen.add(instance_id)
            query = str(row.get("problem_statement") or "")
            if not query.strip():
                raise ValueError("missing problem_statement")
            source = _repo_path(str(row.get("repo") or ""), repos_path, repo_path)
            if not source.is_dir():
                raise FileNotFoundError(f"repository not found: {source}")
            with tempfile.TemporaryDirectory(prefix="galaxy-contextbench-") as temp:
                root = Path(temp)
                _snapshot(source, str(row.get("base_commit") or ""), root)
                snapshot = ProjectScanner(root, instance_id).scan()
                # Galaxy allocates 56% of its total budget to files.
                result = AttentionEngine(root, instance_id).build(
                    query, snapshot, budget_tokens=max(1200, math.ceil(token_budget / .56)),
                    max_candidates=max(72, max_files * 4), max_files=max_files)
                files = [item.path for item in result.selected]
                # Excerpts do not carry reliable source line offsets. Only claim file retrieval.
                predictions.append({"instance_id": instance_id, "traj_data": {"pred_files": files}})
        except Exception as exc:
            failures.append({"instance_id": instance_id, "error": f"{type(exc).__name__}: {exc}"})
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as stream:
        for prediction in predictions:
            stream.write(json.dumps(prediction, ensure_ascii=False) + "\n")
    return {"requested": len(rows), "completed": len(predictions), "predictions": str(target), "failures": failures,
            "note": "file-level retrieval only; score with the independent ContextBench evaluator"}


def evaluate_predictions(gold: str | Path, predictions: str | Path, output: str | Path,
                         *, cache: str | Path | None = None) -> Path:
    """Delegate scoring unchanged to the installed upstream ContextBench package."""
    command = [sys.executable, "-m", "contextbench.evaluate", "--gold", str(gold),
               "--pred", str(predictions), "--out", str(output)]
    if cache is not None:
        command.extend(["--cache", str(cache)])
    subprocess.run(command, check=True)
    return Path(output)
