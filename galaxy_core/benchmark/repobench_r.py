"""Adapter for RepoBench-R candidate-snippet retrieval in JSONL form."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import statistics
import tempfile
import time
from typing import Any

from galaxy_core.attention import AttentionEngine
from galaxy_core.world.scanner import ProjectScanner


def run_repobench_r(dataset: str | Path, output_dir: str | Path, *,
                    limit: int = 0, semantic_mode: str = "auto") -> dict[str, Any]:
    """Rank each supplied RepoBench-R snippet pool without using next_line as a query."""
    source = Path(dataset)
    if source.suffix.lower() != ".jsonl":
        raise ValueError("RepoBench-R adapter expects JSONL exported from the official Hugging Face split")
    if limit < 0 or semantic_mode not in {"auto", "exact"}:
        raise ValueError("limit must be nonnegative and semantic_mode must be auto or exact")
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
    if limit:
        rows = rows[:limit]

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    predictions_path = target / "predictions.jsonl"
    predictions: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    reciprocal_ranks: list[float] = []
    hit_counts = {1: 0, 5: 0, 10: 0}
    eligible_counts = {1: 0, 5: 0, 10: 0}
    build_times: list[float] = []
    semantic_times: list[float] = []
    candidate_total = 0

    for line_no, row in enumerate(rows, start=1):
        instance_id = str(row.get("instance_id") or f"{row.get('repo_name', 'repo')}:{row.get('file_path', 'file')}:{line_no}")
        try:
            snippets = row.get("context")
            if isinstance(snippets, str):
                snippets = json.loads(snippets)
            if not isinstance(snippets, list) or not snippets or not all(isinstance(x, str) for x in snippets):
                raise ValueError("context must be a nonempty list of candidate code snippets")
            gold = row.get("gold_snippet_index", row.get("gold_snippet_idex"))
            if not isinstance(gold, int) or isinstance(gold, bool) or not 0 <= gold < len(snippets):
                raise ValueError("gold snippet index is missing or outside the candidate list")

            # The masked next-line target is intentionally excluded from this query.
            query = "\n".join(str(row.get(key) or "") for key in ("file_path", "import_statement", "code"))
            if not query.strip():
                raise ValueError("RepoBench-R query fields are empty")
            suffix = ".java" if str(row.get("file_path") or "").endswith(".java") else ".py"
            with tempfile.TemporaryDirectory(prefix="galaxy-repobench-r-") as temp:
                root = Path(temp)
                candidate_paths: list[str] = []
                for idx, snippet in enumerate(snippets):
                    relative = f"candidate_{idx:05d}{suffix}"
                    (root / relative).write_text(snippet, encoding="utf-8")
                    candidate_paths.append(relative)
                snapshot = ProjectScanner(root, "repobench-r").scan()
                # RepoBench-R provides the candidate pool; disable graph edges between synthetic files.
                snapshot.edges = []
                started = time.perf_counter()
                result = AttentionEngine(root, "repobench-r").build(
                    query, snapshot, budget_tokens=20_000,
                    max_candidates=len(candidate_paths), max_files=1,
                    semantic_mode=semantic_mode,
                )
                build_ms = (time.perf_counter() - started) * 1000
                build_times.append(build_ms)
                semantic_times.append(float(result.trace.get("semantic_search", {}).get("elapsed_ms") or 0))
                ranked_paths = list(result.trace.get("ranked_paths") or [])
                rank = {path: index for index, path in enumerate(ranked_paths)}
                ordered_indices = sorted(range(len(candidate_paths)),
                                         key=lambda index: (rank.get(candidate_paths[index], len(rank) + index), index))
                gold_rank = ordered_indices.index(gold) + 1

            candidate_total += len(snippets)
            reciprocal_ranks.append(1.0 / gold_rank)
            for k in hit_counts:
                if len(snippets) >= k:
                    eligible_counts[k] += 1
                    hit_counts[k] += gold_rank <= k
            predictions.append({"instance_id": instance_id,
                                "ranked_candidate_indices": ordered_indices,
                                "gold_rank": gold_rank,
                                "candidate_count": len(snippets)})
        except Exception as exc:
            failures.append({"instance_id": instance_id, "error": f"{type(exc).__name__}: {exc}"})

    with predictions_path.open("w", encoding="utf-8") as stream:
        for prediction in predictions:
            stream.write(json.dumps(prediction, ensure_ascii=False) + "\n")

    completed = len(predictions)
    sorted_build = sorted(build_times)
    p95_index = max(0, math.ceil(0.95 * len(sorted_build)) - 1)
    result: dict[str, Any] = {
        "benchmark": "RepoBench-R",
        "dataset": str(source),
        "dataset_sha256": _sha256_file(source),
        "requested": len(rows),
        "completed": completed,
        "candidate_count_mean": round(candidate_total / max(1, completed), 3),
        "metrics": {
            f"accuracy_at_{k}": round(hit_counts[k] / eligible_counts[k], 5)
            if eligible_counts[k] else None
            for k in hit_counts
        },
        "metric_sample_counts": {f"accuracy_at_{k}": eligible_counts[k] for k in eligible_counts},
        "mrr": round(sum(reciprocal_ranks) / max(1, completed), 5),
        "latency_ms": {
            "build_median": round(statistics.median(build_times), 3) if build_times else None,
            "build_p95": round(sorted_build[p95_index], 3) if sorted_build else None,
            "semantic_median": round(statistics.median(semantic_times), 3) if semantic_times else None,
        },
        "semantic_mode": semantic_mode,
        "query_excludes_next_line": True,
        "predictions": str(predictions_path),
        "failures": failures,
        "note": "candidate-pool snippet ranking; latency includes per-item synthetic index preparation, not a warm project query",
    }
    report_path = target / "report.json"
    result["report"] = str(report_path)
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
