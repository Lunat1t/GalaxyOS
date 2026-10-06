"""Data models for Galaxy Open Vault Knowledge File System."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Link:
    source_id: str
    target_id: str
    link_type: str = "wikilink"  # wikilink, embed, markdown_link
    alias: str | None = None
    heading: str | None = None
    context_snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "link_type": self.link_type,
            "alias": self.alias,
            "heading": self.heading,
            "context_snippet": self.context_snippet,
        }


@dataclass
class Note:
    id: str  # clean slug without .md (e.g. "specs/architecture")
    path: str  # relative path with .md (e.g. "specs/architecture.md")
    title: str
    content: str
    raw_text: str  # full file text including frontmatter
    frontmatter: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    outgoing_links: list[Link] = field(default_factory=list)
    mtime: float = 0.0
    size_bytes: int = 0
    content_hash: str = ""

    def to_dict(self, include_content: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "path": self.path,
            "title": self.title,
            "frontmatter": self.frontmatter,
            "tags": self.tags,
            "mtime": self.mtime,
            "size_bytes": self.size_bytes,
            "outgoing_links": [link.to_dict() for link in self.outgoing_links],
        }
        if include_content:
            data["content"] = self.content
            data["raw_text"] = self.raw_text
        return data


@dataclass
class GraphData:
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": self.nodes,
            "edges": self.edges,
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges),
        }


@dataclass
class SearchResult:
    note_id: str
    path: str
    title: str
    snippet: str
    score: float
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "note_id": self.note_id,
            "path": self.path,
            "title": self.title,
            "snippet": self.snippet,
            "score": self.score,
            "tags": self.tags,
        }
