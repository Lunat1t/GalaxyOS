"""Galaxy retrieval adapter for ContextBench's independent dataset and evaluator."""
from __future__ import annotations

import io
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Iterable

from galaxy_core.attention import AttentionEngine
from galaxy_core.world.scanner import ProjectScanner

DJANGO_GRAPH_EXPERIMENT_IDS = (
    "django__django-12406", "django__django-12663", "django__django-16263",
)


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
                       token_budget: int = 10_000, max_files: int = 20,
                       semantic_mode: str = "auto", one_hop_graph: bool = False,
                       instance_ids: Iterable[str] | None = None) -> dict[str, Any]:
    """Write ContextBench prediction JSONL; never inspect gold labels."""
    if token_budget < 1 or max_files < 1 or limit < 0:
        raise ValueError("token_budget and max_files must be positive; limit must be nonnegative")
    if semantic_mode not in {"auto", "exact", "ann"}:
        raise ValueError("semantic_mode must be auto, exact or ann")
    rows = _rows(Path(dataset))
    missing_instance_ids: list[str] = []
    if instance_ids is not None:
        by_id = {str(row.get("instance_id") or ""): row for row in rows}
        wanted_ids = list(dict.fromkeys(str(item) for item in instance_ids))
        missing_instance_ids = [item for item in wanted_ids if item not in by_id]
        rows = [by_id[item] for item in wanted_ids if item in by_id]
    if limit:
        rows = rows[:limit]
    repo_path = Path(repo).resolve() if repo is not None else None
    repos_path = Path(repos_dir).resolve() if repos_dir is not None else None
    if repo_path is None and repos_path is None:
        raise ValueError("provide repo or repos_dir")
    predictions: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    semantic_runs: list[dict[str, Any]] = []
    graph_runs: list[dict[str, Any]] = []
    estimated_context_tokens = 0
    tokens_by_instance: dict[str, int] = {}
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
                    max_candidates=max(72, max_files * 4), max_files=max_files,
                    semantic_mode=semantic_mode, one_hop_graph=one_hop_graph)
                files = [item.path for item in result.selected]
                semantic_runs.append(dict(result.trace.get("semantic_search") or {}))
                graph_runs.append(dict(result.trace.get("one_hop_graph") or {}))
                estimated_context_tokens += sum(item.estimated_tokens for item in result.selected)
                tokens_by_instance[instance_id] = sum(item.estimated_tokens for item in result.selected)
                # Excerpts do not carry reliable source line offsets. Only claim file retrieval.
                predictions.append({"instance_id": instance_id, "traj_data": {"pred_files": files}})
        except Exception as exc:
            failures.append({"instance_id": instance_id, "error": f"{type(exc).__name__}: {exc}"})
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as stream:
        for prediction in predictions:
            stream.write(json.dumps(prediction, ensure_ascii=False) + "\n")
    total_vectors = sum(int(item.get("total_vectors") or 0) for item in semantic_runs)
    scored_vectors = sum(int(item.get("scored_vectors") or 0) for item in semantic_runs)
    semantic_summary = {
        "requested_mode": semantic_mode,
        "exact_tasks": sum(item.get("mode") == "exact" for item in semantic_runs),
        "ann_tasks": sum(item.get("mode") == "ann" for item in semantic_runs),
        "total_vectors": total_vectors,
        "scored_vectors": scored_vectors,
        "scored_ratio": round(scored_vectors / max(1, total_vectors), 4),
        "elapsed_ms": round(sum(float(item.get("elapsed_ms") or 0) for item in semantic_runs), 3),
    }
    return {"requested": len(rows), "completed": len(predictions), "predictions": str(target),
            "failures": failures, "semantic": semantic_summary,
            "one_hop_graph": {"enabled": one_hop_graph,
                              "seed_count": sum(int(item.get("seed_count") or 0) for item in graph_runs),
                              "candidate_count": sum(int(item.get("candidate_count") or 0) for item in graph_runs),
                              "inheritance_candidates": sum(int(item.get("inheritance_candidates") or 0)
                                                             for item in graph_runs)},
            "estimated_context_tokens": estimated_context_tokens,
            "estimated_context_tokens_by_instance": tokens_by_instance,
            "missing_instance_ids": missing_instance_ids,
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


def compare_one_hop_graph(dataset: str | Path, output_dir: str | Path, *,
                          repo: str | Path | None = None, repos_dir: str | Path | None = None,
                          evaluate: bool = False, cache: str | Path | None = None) -> dict[str, Any]:
    """Paired graph ablation on the three fixed Django tasks, Top-15 and 20k-token budget."""
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    common = dict(repo=repo, repos_dir=repos_dir, token_budget=20_000, max_files=15,
                  semantic_mode="auto", instance_ids=DJANGO_GRAPH_EXPERIMENT_IDS)
    baseline_path, expanded_path = target / "baseline.jsonl", target / "one-hop.jsonl"
    baseline = export_predictions(dataset, baseline_path, one_hop_graph=False, **common)
    expanded = export_predictions(dataset, expanded_path, one_hop_graph=True, **common)

    def read_files(path: Path) -> dict[str, set[str]]:
        predictions: dict[str, set[str]] = {}
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                raw = json.loads(line)
                instance_id = str(raw.get("instance_id") or "")
                if not instance_id or instance_id in predictions:
                    raise ValueError(f"invalid or duplicate prediction instance_id in {path}")
                predictions[instance_id] = set(raw.get("traj_data", {}).get("pred_files") or [])
        return predictions

    base_files, graph_files = read_files(baseline_path), read_files(expanded_path)
    paired_ids = set(base_files) & set(graph_files)
    jaccard: list[float] = []
    common_tokens = sorted(paired_ids)
    tasks_changed = recovered_slots = 0
    for instance_id in paired_ids:
        before, after = base_files[instance_id], graph_files[instance_id]
        union = before | after
        jaccard.append(len(before & after) / len(union) if union else 1.0)
        tasks_changed += before != after
        recovered_slots += len(after - before)

    result: dict[str, Any] = {
        "experiment": "django-top15-one-hop-graph",
        "dataset": str(Path(dataset)),
        "dataset_sha256": _sha256_file(Path(dataset)),
        "instances": list(DJANGO_GRAPH_EXPERIMENT_IDS),
        "configuration": {"token_budget": 20_000, "max_files": 15,
                          "semantic_mode": "auto", "one_hop_seed_limit": 15},
        "paired": len(paired_ids),
        "missing_instance_ids": sorted(set(DJANGO_GRAPH_EXPERIMENT_IDS) - paired_ids),
        "prediction_files": {"baseline": str(baseline_path), "one_hop": str(expanded_path)},
        "selection_delta": {"mean_file_jaccard": round(sum(jaccard) / len(jaccard), 4) if jaccard else None,
                            "tasks_with_changed_selection": tasks_changed,
                            "newly_selected_files_before_gold_scoring": recovered_slots},
        "context_tokens": {"baseline": baseline["estimated_context_tokens"],
                            "one_hop": expanded["estimated_context_tokens"],
                            "by_instance": {
                                key: {"baseline": baseline["estimated_context_tokens_by_instance"].get(key),
                                      "one_hop": expanded["estimated_context_tokens_by_instance"].get(key)}
                                for key in common_tokens
                            }},
        "one_hop_graph": expanded["one_hop_graph"],
        "failures": {"baseline": baseline["failures"], "one_hop": expanded["failures"]},
        "scores_note": "selection delta is not recall; use upstream ContextBench file metrics for gold-scored results",
    }
    complete = (len(paired_ids) == len(DJANGO_GRAPH_EXPERIMENT_IDS)
                and baseline["completed"] == expanded["completed"] == len(DJANGO_GRAPH_EXPERIMENT_IDS)
                and not baseline["failures"] and not expanded["failures"])
    if evaluate and complete:
        base_scores, graph_scores = target / "baseline-scores.jsonl", target / "one-hop-scores.jsonl"
        evaluate_predictions(dataset, baseline_path, base_scores, cache=cache)
        evaluate_predictions(dataset, expanded_path, graph_scores, cache=cache)
        result["scores"] = {"baseline": str(base_scores), "one_hop": str(graph_scores)}
        baseline_metrics = _contextbench_file_metrics(base_scores)
        graph_metrics = _contextbench_file_metrics(graph_scores)
        score_ids = sorted(set(baseline_metrics) & set(graph_metrics) & paired_ids)
        if len(score_ids) == len(DJANGO_GRAPH_EXPERIMENT_IDS):
            def aggregate(metrics: dict[str, dict[str, float]]) -> dict[str, Any]:
                per_task = {}
                for key in score_ids:
                    recall = metrics[key]["recall"]
                    precision = metrics[key]["precision"]
                    f1 = 2 * recall * precision / (recall + precision) if recall + precision else 0.0
                    per_task[key] = {"recall": round(recall, 5), "precision": round(precision, 5),
                                     "f1": round(f1, 5)}
                macro = {metric: round(sum(row[metric] for row in per_task.values()) / len(per_task), 5)
                         for metric in ("recall", "precision", "f1")}
                return {"macro": macro, "per_instance": per_task}
            before, after = aggregate(baseline_metrics), aggregate(graph_metrics)
            result["metrics"] = {
                "baseline": before, "one_hop": after,
                "delta": {metric: round(after["macro"][metric] - before["macro"][metric], 5)
                          for metric in ("recall", "precision", "f1")},
                "aggregation": "macro average across the same three fixed tasks",
            }
        else:
            result["scores_note"] = ("upstream score files were created, but their rows did not contain all three "
                                     "paired file coverage/precision metrics; inspect the score files directly")
    report_path = target / "comparison.json"
    result["report"] = str(report_path)
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _contextbench_file_metrics(path: Path) -> dict[str, dict[str, float]]:
    """Read upstream per-task file coverage/precision, accepting JSONL or JSON."""
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return {}
    try:
        decoded = json.loads(content)
        rows = decoded if isinstance(decoded, list) else [decoded]
    except json.JSONDecodeError:
        rows = [json.loads(line) for line in content.splitlines() if line.strip()]
    metrics: dict[str, dict[str, float]] = {}
    for row in rows:
        instance_id = str(row.get("instance_id") or "")
        final = row.get("final") or {}
        file_metrics = final.get("file") or row.get("file") or {}
        recall = file_metrics.get("coverage", file_metrics.get("recall"))
        precision = file_metrics.get("precision")
        if instance_id and isinstance(recall, (int, float)) and isinstance(precision, (int, float)):
            metrics[instance_id] = {"recall": float(recall), "precision": float(precision)}
    return metrics


def compare_exact_ann(dataset: str | Path, output_dir: str | Path, *,
                      repo: str | Path | None = None, repos_dir: str | Path | None = None,
                      limit: int = 0, token_budget: int = 10_000, max_files: int = 20,
                      evaluate: bool = False, cache: str | Path | None = None) -> dict[str, Any]:
    """Run exact and ANN on the same ContextBench slice and summarize paired differences."""
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    common = dict(repo=repo, repos_dir=repos_dir, limit=limit,
                  token_budget=token_budget, max_files=max_files)
    exact_path = target / "exact.jsonl"
    ann_path = target / "ann.jsonl"
    exact = export_predictions(dataset, exact_path, semantic_mode="exact", **common)
    ann = export_predictions(dataset, ann_path, semantic_mode="ann", **common)

    def read_predictions(path: Path) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                item = json.loads(line)
                instance_id = str(item.get("instance_id") or "")
                if not instance_id or instance_id in result:
                    raise ValueError(f"invalid or duplicate prediction instance_id in {path}")
                result[instance_id] = set(item.get("traj_data", {}).get("pred_files") or [])
        return result

    exact_files = read_predictions(exact_path)
    ann_files = read_predictions(ann_path)
    exact_ids, ann_ids = set(exact_files), set(ann_files)
    paired_ids = exact_ids & ann_ids
    jaccard = []
    exact_only = ann_only = changed_tasks = 0
    for instance_id in sorted(paired_ids):
        left, right = exact_files[instance_id], ann_files[instance_id]
        union = left | right
        jaccard.append(len(left & right) / len(union) if union else 1.0)
        exact_only += len(left - right)
        ann_only += len(right - left)
        changed_tasks += left != right

    result: dict[str, Any] = {
        "dataset": str(Path(dataset)),
        "dataset_sha256": _sha256_file(Path(dataset)),
        "configuration": {"limit": limit, "token_budget": token_budget, "max_files": max_files},
        "requested": exact["requested"],
        "completed": {"exact": exact["completed"], "ann": ann["completed"]},
        "paired": len(paired_ids),
        "unpaired_instance_ids": {
            "exact_only": sorted(exact_ids - ann_ids),
            "ann_only": sorted(ann_ids - exact_ids),
        },
        "selection": {
            "mean_file_jaccard": round(sum(jaccard) / len(jaccard), 4) if jaccard else None,
            "tasks_with_different_files": changed_tasks,
            "exact_only_files": exact_only,
            "ann_only_files": ann_only,
        },
        "semantic": {
            "exact_elapsed_ms": exact["semantic"]["elapsed_ms"],
            "ann_elapsed_ms": ann["semantic"]["elapsed_ms"],
            "ann_speedup": round(exact["semantic"]["elapsed_ms"] /
                                  max(0.001, ann["semantic"]["elapsed_ms"]), 3),
            "exact_scored_vectors": exact["semantic"]["scored_vectors"],
            "ann_scored_vectors": ann["semantic"]["scored_vectors"],
            "exact_total_vectors": exact["semantic"]["total_vectors"],
            "ann_total_vectors": ann["semantic"]["total_vectors"],
            "exact_scored_ratio": exact["semantic"]["scored_ratio"],
            "ann_scored_ratio": ann["semantic"]["scored_ratio"],
        },
        "predictions": {"exact": str(exact_path), "ann": str(ann_path)},
        "failures": {"exact": exact["failures"], "ann": ann["failures"]},
        "note": "paired file-selection and retrieval-work comparison only; recall and precision require the upstream evaluator",
    }
    complete_pair = (exact["completed"] == ann["completed"] == exact["requested"]
                     and len(paired_ids) == exact["requested"])
    if evaluate and complete_pair and exact["requested"]:
        exact_scores, ann_scores = target / "exact-scores.jsonl", target / "ann-scores.jsonl"
        evaluate_predictions(dataset, exact_path, exact_scores, cache=cache)
        evaluate_predictions(dataset, ann_path, ann_scores, cache=cache)
        result["scores"] = {"exact": str(exact_scores), "ann": str(ann_scores)}
    report_path = target / "comparison.json"
    result["report"] = str(report_path)
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
