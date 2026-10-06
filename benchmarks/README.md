# ContextBench adapter

Use the independent [ContextBench](https://github.com/EuniAI/ContextBench) dataset and evaluator to measure Galaxy's selected files against human-annotated gold context.

```bash
python galaxy.py contextbench-run data/contextbench_verified.parquet \
  --repos-dir /path/to/repos --out benchmarks/contextbench-predictions.jsonl \
  --evaluate benchmarks/contextbench-scores.jsonl
```

Local repositories must be arranged under `/path/to/repos/OWNER/REPO` and include each task's base commit. Install `pyarrow` for Parquet input and install upstream ContextBench for `--evaluate`. Use `--repo` for a single-repository subset. Galaxy exports file predictions only, and the upstream evaluator calculates the scores.

To measure ANN quality loss, export and evaluate the same subset twice:

```bash
python galaxy.py contextbench-run data/contextbench_verified.parquet \
  --repos-dir /path/to/repos --semantic-mode exact \
  --out benchmarks/contextbench-exact.jsonl
python galaxy.py contextbench-run data/contextbench_verified.parquet \
  --repos-dir /path/to/repos --semantic-mode ann \
  --out benchmarks/contextbench-ann.jsonl
```

The command summary reports semantic time and the ratio of scored to indexed vectors.
Recall and precision still come only from the upstream evaluator.

For a paired run that guarantees the same task slice and budgets, use:

```bash
python galaxy.py contextbench-compare data/contextbench_verified.parquet \
  --repos-dir /path/to/repos --limit 50 --evaluate \
  --out-dir benchmarks/contextbench-ann-verified-50
```

This writes aligned exact/ANN predictions, a retrieval-work and file-overlap
summary, and (with `--evaluate`) separate upstream score files. File overlap is
not gold-label recall. Keep the dataset revision, subset, repository base
commits, and evaluator version alongside any reported result.

## Fixed Django graph-expansion ablation

The AST World Model records only uniquely resolved in-repository base classes.
The optional experiment leaves the existing retrieval path as baseline and
adds a directed one-hop pass over imports and inheritance from the baseline's
top 15. It holds the output cap at 15 files and the context budget at 20,000
tokens for `django__django-12406`, `django__django-12663`, and
`django__django-16263`:

```bash
python galaxy.py contextbench-graph-compare data/contextbench_verified.parquet \
  --repo /path/to/django --evaluate \
  --out-dir benchmarks/django-one-hop-graph
```

## RepoBench-R snippet retrieval

Export one official RepoBench-R split to JSONL. For example, using Hugging
Face `datasets` and the official `tianyang/repobench-r` dataset:

```python
import json
from datasets import load_dataset

rows = load_dataset("tianyang/repobench-r", "python_cff", split="test_easy")
with open("data/repobench-r-python-cff-test-easy.jsonl", "w", encoding="utf-8") as out:
    for row in rows:
        item = {key: row[key] for key in (
            "repo_name", "file_path", "context", "import_statement", "code",
            "gold_snippet_index",
        ) if key in row}
        out.write(json.dumps(item, ensure_ascii=False) + "\n")
```

The export deliberately omits `next_line`. The
adapter ranks the provided candidates with Galaxy's hybrid Attention Engine,
never uses the held-out `next_line`, and reports Accuracy@1/5/10, MRR, and
retrieval timings:

```bash
python galaxy.py repobench-r-run data/repobench-r-python-cff-test-easy.jsonl \
  --semantic-mode exact --out-dir benchmarks/repobench-r-python-cff-test-easy
```

The adapter expects local JSONL so the benchmark files are not bundled in
Galaxy releases. Keep Python `cff`, `cfr`, and difficulty splits reported
separately; candidate-snippet ranking is not an issue-resolution score.
