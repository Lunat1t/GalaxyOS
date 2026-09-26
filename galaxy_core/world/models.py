from __future__ import annotations
from dataclasses import asdict, dataclass, field
from typing import Any

@dataclass(frozen=True)
class WorldNode:
    id: str
    path: str
    kind: str
    language: str = "text"
    component: str = "root"
    symbols: tuple[str, ...] = ()
    size_bytes: int = 0
    content_hash: str = ""
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True)
class WorldEdge:
    source: str
    target: str
    relation: str
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass
class WorldSnapshot:
    project: str
    root: str
    generated_at: str
    nodes: list[WorldNode] = field(default_factory=list)
    edges: list[WorldEdge] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "root": self.root,
            "generated_at": self.generated_at,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "stats": dict(self.stats),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WorldSnapshot":
        return cls(
            project=str(raw.get("project") or "default"),
            root=str(raw.get("root") or ""),
            generated_at=str(raw.get("generated_at") or ""),
            nodes=[WorldNode(**{**n, "symbols": tuple(n.get("symbols") or ())}) for n in raw.get("nodes", [])],
            edges=[WorldEdge(**e) for e in raw.get("edges", [])],
            stats=dict(raw.get("stats") or {}),
        )
