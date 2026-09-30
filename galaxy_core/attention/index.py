"""Persistent prepared documents for the Attention Engine.

The world snapshot supplies content hashes. A sync only prepares changed nodes;
queries reuse the exact text, term frequencies and portable vectors.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import struct
from typing import Any


class StaleWorldSnapshot(RuntimeError):
    """Source bytes no longer match the snapshot used for retrieval."""


class AttentionIndex:
    VERSION = 5
    EXACT_SEMANTIC_THRESHOLD = 512
    DOCUMENT_BUCKETS = 24
    QUERY_BUCKETS = 32

    def __init__(self, root: Path, storage_root: Path, project: str, embedder: Any, tokenize: Any):
        self.root = root.resolve()
        self.embedder = embedder
        self.tokenize = tokenize
        key = hashlib.sha256(f"{self.root}\0{project}".encode()).hexdigest()[:20]
        self.path = storage_root.resolve() / "data" / "runtime" / "attention" / f"{key}.sqlite3"

    def prepare(self, nodes: list[Any]) -> dict[str, int]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            # Earlier versions packed document text and vector together. Rebuild the
            # disposable cache once to make the two independently readable.
            columns = {row[1] for row in db.execute("PRAGMA table_info(documents)")}
            if columns and "vector" not in columns:
                db.execute("DROP TABLE documents")
                db.execute("DROP TABLE IF EXISTS postings")
            db.execute("CREATE TABLE IF NOT EXISTS documents (path TEXT PRIMARY KEY, signature TEXT NOT NULL, length INTEGER NOT NULL, doc TEXT NOT NULL, vector BLOB NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS postings (term TEXT NOT NULL, path TEXT NOT NULL, freq INTEGER NOT NULL, PRIMARY KEY (term, path)) WITHOUT ROWID")
            db.execute("CREATE INDEX IF NOT EXISTS postings_path ON postings(path)")
            db.execute("CREATE TABLE IF NOT EXISTS vector_buckets (bucket INTEGER NOT NULL, path TEXT NOT NULL, PRIMARY KEY (bucket, path)) WITHOUT ROWID")
            db.execute("CREATE INDEX IF NOT EXISTS vector_buckets_path ON vector_buckets(path)")
            current = {p: sig for p, sig in db.execute("SELECT path, signature FROM documents")}
            expected: set[str] = set()
            updated = 0
            for node in nodes:
                expected.add(node.path)
                # Include metadata and algorithm version so changes to extraction invalidate entries.
                signature = hashlib.sha256(json.dumps([
                    self.VERSION, self.embedder.model, self.embedder.dims, node.content_hash,
                    node.path, node.symbols, node.summary, node.component, node.kind,
                ], ensure_ascii=False).encode()).hexdigest()
                if current.get(node.path) == signature:
                    continue
                try:
                    data = (self.root / node.path).read_bytes()
                except OSError as exc:
                    raise StaleWorldSnapshot(f"source unavailable: {node.path}") from exc
                if hashlib.sha256(data).hexdigest()[:16] != node.content_hash:
                    raise StaleWorldSnapshot(f"source changed since world snapshot: {node.path}")
                body = data.decode("utf-8", errors="replace")[:28_000]
                doc = f"{node.path}\n{' '.join(node.symbols)}\n{node.summary}\n" + body
                terms = self.tokenize(doc)
                semantic = f"{node.path} {' '.join(node.symbols)} {node.summary} {node.component} {node.kind}"
                frequencies = Counter(terms)
                vector = self.embedder.encode(semantic)
                db.execute("DELETE FROM postings WHERE path = ?", (node.path,))
                db.execute("DELETE FROM vector_buckets WHERE path = ?", (node.path,))
                db.executemany("INSERT INTO postings VALUES (?, ?, ?)",
                               ((term, node.path, freq) for term, freq in frequencies.items()))
                db.executemany("INSERT INTO vector_buckets VALUES (?, ?)",
                               ((bucket, node.path) for bucket in self._bucket_keys(vector, self.DOCUMENT_BUCKETS)))
                db.execute("INSERT OR REPLACE INTO documents VALUES (?, ?, ?, ?, ?)",
                           (node.path, signature, len(terms), doc, struct.pack(f"<{len(vector)}d", *vector)))
                updated += 1
            removed = set(current) - expected
            db.executemany("DELETE FROM documents WHERE path = ?", ((p,) for p in removed))
            db.executemany("DELETE FROM postings WHERE path = ?", ((p,) for p in removed))
            db.executemany("DELETE FROM vector_buckets WHERE path = ?", ((p,) for p in removed))
        return {"updated": updated, "reused": len(expected) - updated, "removed": len(removed)}

    def semantic(self, task: str) -> dict[str, float]:
        """Exact portable-vector scores without fetching source text."""
        query = self.embedder.encode(task)
        out: dict[str, float] = {}
        with sqlite3.connect(self.path) as db:
            for path, blob in db.execute("SELECT path, vector FROM documents"):
                vector = struct.unpack(f"<{self.embedder.dims}d", blob)
                out[path] = max(-1.0, min(1.0, sum(a * b for a, b in zip(query, vector))))
        return out

    def count(self) -> int:
        with sqlite3.connect(self.path) as db:
            return int(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0])

    @staticmethod
    def _bucket_keys(vector: list[float] | tuple[float, ...], limit: int) -> list[int]:
        """Encode the strongest signed coordinates as deterministic ANN buckets."""
        strongest = sorted(range(len(vector)), key=lambda i: (-abs(vector[i]), i))[:limit]
        return [index * 2 + int(vector[index] >= 0) for index in strongest if vector[index]]

    @staticmethod
    def _score(query: list[float], blob: bytes, dims: int) -> float:
        vector = struct.unpack(f"<{dims}d", blob)
        return max(-1.0, min(1.0, sum(a * b for a, b in zip(query, vector))))

    def semantic_search(self, task: str, *, candidate_limit: int = 1024,
                        exact_threshold: int | None = None) -> tuple[dict[str, float], dict[str, int | str]]:
        """Retrieve approximate neighbors, then compute exact scores for that bounded set.

        The inverted buckets contain only signed high-magnitude coordinates. They are
        cheap to update and portable across platforms. Small indexes retain exhaustive
        scoring because it is faster and preserves exact behavior.
        """
        query = self.embedder.encode(task)
        threshold = self.EXACT_SEMANTIC_THRESHOLD if exact_threshold is None else max(0, exact_threshold)
        with sqlite3.connect(self.path) as db:
            total = int(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
            if total <= threshold or candidate_limit >= total:
                rows = db.execute("SELECT path, vector FROM documents").fetchall()
                scores = {path: self._score(query, blob, self.embedder.dims) for path, blob in rows}
                return scores, {"mode": "exact", "total_vectors": total,
                                "scored_vectors": len(rows), "bucket_hits": 0}

            buckets = self._bucket_keys(query, self.QUERY_BUCKETS)
            if not buckets:
                return {}, {"mode": "ann", "total_vectors": total,
                            "scored_vectors": 0, "bucket_hits": 0}
            marks = ",".join("?" for _ in buckets)
            ranked = db.execute(
                f"SELECT path, COUNT(*) AS hits FROM vector_buckets "
                f"WHERE bucket IN ({marks}) GROUP BY path ORDER BY hits DESC, path LIMIT ?",
                [*buckets, max(1, candidate_limit)],
            ).fetchall()
            paths = [path for path, _hits in ranked]
            blobs: dict[str, bytes] = {}
            for offset in range(0, len(paths), 900):
                batch = paths[offset:offset + 900]
                if batch:
                    batch_marks = ",".join("?" for _ in batch)
                    blobs.update(db.execute(
                        f"SELECT path, vector FROM documents WHERE path IN ({batch_marks})", batch
                    ))
        scores = {path: self._score(query, blobs[path], self.embedder.dims)
                  for path in paths if path in blobs}
        return scores, {"mode": "ann", "total_vectors": total,
                        "scored_vectors": len(scores), "bucket_hits": len(ranked)}

    def documents(self, paths: list[str]) -> dict[str, str]:
        """Fetch bounded source text only for ranked candidates."""
        out: dict[str, str] = {}
        with sqlite3.connect(self.path) as db:
            for offset in range(0, len(paths), 900):
                batch = paths[offset:offset + 900]
                if batch:
                    marks = ",".join("?" for _ in batch)
                    out.update(db.execute(f"SELECT path, doc FROM documents WHERE path IN ({marks})", batch))
        return out

    def bm25(self, task: str) -> dict[str, float]:
        """Score only posting lists for query terms; preserve the previous BM25 formula."""
        query = list(dict.fromkeys(self.tokenize(task)))
        if not query:
            return {}
        with sqlite3.connect(self.path) as db:
            n_docs, total_length = db.execute("SELECT COUNT(*), COALESCE(SUM(length), 0) FROM documents").fetchone()
            if not n_docs:
                return {}
            avgdl = total_length / n_docs
            hits: list[tuple[str, str, int, int]] = []
            for offset in range(0, len(query), 900):
                batch = query[offset:offset + 900]
                marks = ",".join("?" for _ in batch)
                hits.extend(db.execute(
                    f"SELECT p.term, p.path, p.freq, d.length FROM postings p "
                    f"JOIN documents d ON d.path = p.path WHERE p.term IN ({marks})", batch
                ))
        df = Counter(term for term, _, _, _ in hits)
        by_path: dict[str, dict[str, int]] = {}
        lengths: dict[str, int] = {}
        for term, path, freq, length in hits:
            by_path.setdefault(path, {})[term] = freq
            lengths[path] = length
        out: dict[str, float] = {}
        for path, frequencies in by_path.items():
            score = 0.0
            for term in query:
                freq = frequencies.get(term, 0)
                if freq:
                    idf = math.log(1.0 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
                    denom = freq + 1.5 * (0.25 + 0.75 * max(1, lengths[path]) / max(1.0, avgdl))
                    score += idf * (freq * 2.5 / denom)
            if score > 0:
                out[path] = score
        return out
