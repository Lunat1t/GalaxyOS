"""Dependency-aware repository scanner used by the Galaxy project world model."""
from __future__ import annotations

import ast
import datetime as dt
import hashlib
from pathlib import Path
import re
import posixpath
from typing import Iterable

from .models import WorldEdge, WorldNode, WorldSnapshot

IGNORE_DIRS = {".git", ".galaxy", ".venv", "venv", "node_modules", "dist", "build", "__pycache__", ".idea", ".vscode"}
MANAGED_PREFIXES = {("data", "runtime"), ("data", "brain"), ("data", "runs"), ("data", "agents"), ("data", "teams"), ("data", "planning"), ("data", "interviews"), ("data", "briefs"), ("data", "plans"), ("data", "vault")}
TEXT_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".rs", ".go", ".c", ".h", ".cpp", ".hpp",
    ".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".html", ".css", ".scss", ".sql", ".sh"
}
LANG = {
    ".py":"python", ".js":"javascript", ".jsx":"javascript", ".ts":"typescript", ".tsx":"typescript", ".mjs":"javascript", ".cjs":"javascript",
    ".java":"java", ".kt":"kotlin", ".rs":"rust", ".go":"go", ".c":"c", ".h":"c", ".cpp":"cpp", ".hpp":"cpp",
    ".md":"markdown", ".json":"json", ".toml":"toml", ".yaml":"yaml", ".yml":"yaml", ".html":"html", ".css":"css", ".scss":"scss", ".sql":"sql", ".sh":"shell"
}
JS_IMPORT = re.compile(r"(?:from\s+|require\(\s*|import\s*\(\s*)['\"]([^'\"]+)['\"]")
MD_LINK = re.compile(r"\[\[([^\]|#]+)")

class ProjectScanner:
    def __init__(self, project_root: str | Path, project: str = "default", *, max_file_bytes: int = 750_000):
        self.root = Path(project_root).resolve()
        self.project = project
        self.max_file_bytes = max_file_bytes

    def scan(self) -> WorldSnapshot:
        nodes: list[WorldNode] = []
        edges: list[WorldEdge] = []
        text_by_path: dict[str, str] = {}
        path_index: set[str] = set()
        for path in self._files():
            rel = path.relative_to(self.root).as_posix()
            path_index.add(rel)
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if len(data) > self.max_file_bytes:
                text = ""
            else:
                text = data.decode("utf-8", errors="replace")
            text_by_path[rel] = text
            symbols = self._symbols(path, text)
            component = rel.split("/", 1)[0] if "/" in rel else "root"
            kind = self._kind(path)
            summary = self._summary(text)
            nodes.append(WorldNode(
                id=rel, path=rel, kind=kind, language=LANG.get(path.suffix.lower(), "text"),
                component=component, symbols=tuple(symbols[:40]), size_bytes=len(data),
                content_hash=hashlib.sha256(data).hexdigest()[:16], summary=summary,
            ))

        for rel, text in text_by_path.items():
            suffix = Path(rel).suffix.lower()
            targets: Iterable[tuple[str, str]] = ()
            if suffix == ".py":
                targets = ((t, "imports") for t in self._python_imports(rel, text, path_index))
            elif suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
                targets = ((t, "imports") for t in self._js_imports(rel, text, path_index))
            elif suffix == ".md":
                targets = ((t, "links") for t in self._markdown_links(rel, text, path_index))
            for target, relation in targets:
                if target != rel:
                    edges.append(WorldEdge(rel, target, relation, 0.98))

        # Soft same-component edges are intentionally omitted; they create noise. The component
        # field is retained for coarse navigation and context compilation.
        languages: dict[str, int] = {}
        components: dict[str, int] = {}
        for node in nodes:
            languages[node.language] = languages.get(node.language, 0) + 1
            components[node.component] = components.get(node.component, 0) + 1
        return WorldSnapshot(
            project=self.project,
            root=str(self.root),
            generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            nodes=sorted(nodes, key=lambda n: n.path),
            edges=sorted({(e.source, e.target, e.relation): e for e in edges}.values(), key=lambda e: (e.source, e.target, e.relation)),
            stats={"files": len(nodes), "edges": len(edges), "languages": languages, "components": components},
        )

    def _files(self):
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            try:
                parts = path.relative_to(self.root).parts
            except ValueError:
                continue
            if any(part in IGNORE_DIRS or part.startswith(".pytest") for part in parts[:-1]):
                continue
            if len(parts) >= 2 and tuple(parts[:2]) in MANAGED_PREFIXES:
                continue
            if path.name.startswith(".") and path.name not in {".env.example"}:
                continue
            if path.suffix.lower() in TEXT_EXTS or path.name in {"Dockerfile", "Makefile", "AGENTS.md", "CLAUDE.md"}:
                yield path

    @staticmethod
    def _kind(path: Path) -> str:
        name = path.name.lower()
        rel = path.as_posix().lower()
        if "test" in name or "/tests/" in rel or rel.startswith("tests/"):
            return "test"
        if path.suffix.lower() == ".md":
            return "documentation"
        if path.suffix.lower() in {".json", ".toml", ".yaml", ".yml", ".ini", ".cfg"} or name in {"dockerfile", "makefile"}:
            return "configuration"
        return "source"

    @staticmethod
    def _summary(text: str) -> str:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return " ".join(lines[:3])[:360]

    @staticmethod
    def _symbols(path: Path, text: str) -> list[str]:
        if path.suffix.lower() == ".py":
            try:
                tree = ast.parse(text)
                return [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
            except SyntaxError:
                return []
        if path.suffix.lower() in {".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rs"}:
            rx = re.compile(r"\b(?:class|interface|function|def|fn|func)\s+([A-Za-z_][A-Za-z0-9_]*)")
            return rx.findall(text)[:40]
        return []

    def _python_imports(self, rel: str, text: str, index: set[str]) -> set[str]:
        out: set[str] = set()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return out
        module = Path(rel).with_suffix("").parts
        for node in ast.walk(tree):
            names: list[tuple[str, int]] = []
            if isinstance(node, ast.Import):
                names = [(a.name, 0) for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                names = [(base, int(node.level or 0))]
            for name, level in names:
                if level:
                    parts = list(module[:-1])
                    parts = parts[:max(0, len(parts) - level + 1)]
                else:
                    parts = []
                parts += [p for p in name.split(".") if p]
                candidates = ["/".join(parts) + ".py", "/".join(parts + ["__init__"]) + ".py"]
                for candidate in candidates:
                    if candidate in index:
                        out.add(candidate); break
        return out

    @staticmethod
    def _resolve_relative(rel: str, spec: str, index: set[str], extensions: tuple[str, ...]) -> str | None:
        if not spec.startswith("."):
            return None
        base = posixpath.normpath((Path(rel).parent / spec).as_posix())
        while base.startswith("./"):
            base = base[2:]
        candidates = [base]
        candidates += [base + ext for ext in extensions]
        candidates += [(Path(base) / ("index" + ext)).as_posix() for ext in extensions]
        for candidate in candidates:
            normalized = Path(candidate).as_posix()
            if normalized in index:
                return normalized
        return None

    def _js_imports(self, rel: str, text: str, index: set[str]) -> set[str]:
        out = set()
        exts = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json")
        for spec in JS_IMPORT.findall(text):
            target = self._resolve_relative(rel, spec, index, exts)
            if target:
                out.add(target)
        return out

    def _markdown_links(self, rel: str, text: str, index: set[str]) -> set[str]:
        out = set()
        for spec in MD_LINK.findall(text):
            spec = spec.strip().replace("\\", "/")
            local = spec if spec.endswith(".md") else spec + ".md"
            candidates = [local, posixpath.normpath((Path(rel).parent / local).as_posix())]
            for candidate in candidates:
                if candidate in index:
                    out.add(candidate); break
        return out
