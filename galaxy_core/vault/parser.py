"""Markdown and Frontmatter Parser for Galaxy Open Vault."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from galaxy_core.vault.models import Link, Note


# Regex for Obsidian-style wikilinks: [[target#heading|alias]]
WIKILINK_REGEX = re.compile(r"\[\[([^\]\|#\n]+)(?:#([^\]\|\n]+))?(?:\|([^\]\n]+))?\]\]")

# Regex for inline hashtags: #tag or #nested/tag
TAG_REGEX = re.compile(r"(?:^|\s)#([a-zA-Z][a-zA-Z0-9_\-\/]*)\b")

# Regex for H1 markdown header
H1_REGEX = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter between opening and closing '---' lines."""
    clean_text = text.lstrip("\ufeff")  # remove BOM if present
    if not clean_text.startswith("---"):
        return {}, clean_text

    lines = clean_text.splitlines()
    if len(lines) < 2 or lines[0].strip() != "---":
        return {}, clean_text

    frontmatter_lines: list[str] = []
    body_start_idx = -1

    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            body_start_idx = i + 1
            break
        frontmatter_lines.append(lines[i])

    if body_start_idx == -1:
        return {}, clean_text

    body = "\n".join(lines[body_start_idx:])
    raw_yaml = "\n".join(frontmatter_lines)

    metadata = _parse_simple_yaml(raw_yaml)
    return metadata, body


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Lightweight YAML parser supporting key: value, lists, numbers, booleans."""
    try:
        import yaml
        parsed = yaml.safe_load(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    # Fallback pure-python parser
    result: dict[str, Any] = {}
    current_list_key: str | None = None

    for line in text.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue

        if trimmed.startswith("- ") and current_list_key:
            val = trimmed[2:].strip().strip("\"'")
            result[current_list_key].append(val)
            continue

        if ":" in line:
            key, raw_val = line.split(":", 1)
            key = key.strip()
            val = raw_val.strip()

            if not val:
                current_list_key = key
                result[key] = []
                continue

            current_list_key = None
            # Inline list: [a, b, c]
            if val.startswith("[") and val.endswith("]"):
                items = [x.strip().strip("\"'") for x in val[1:-1].split(",") if x.strip()]
                result[key] = items
            elif val.lower() in ("true", "yes", "on"):
                result[key] = True
            elif val.lower() in ("false", "no", "off"):
                result[key] = False
            else:
                # Try number
                try:
                    if "." in val:
                        result[key] = float(val)
                    else:
                        result[key] = int(val)
                except ValueError:
                    result[key] = val.strip("\"'")

    return result


def parse_note(file_path: Path, vault_root: Path) -> Note:
    """Parse a Markdown file from the vault into a structured Note object."""
    rel_path = file_path.relative_to(vault_root).as_posix()
    note_id = rel_path[:-3] if rel_path.endswith(".md") else rel_path

    raw_text = file_path.read_text(encoding="utf-8", errors="replace")
    frontmatter, body = parse_frontmatter(raw_text)

    # 1. Title detection
    title = str(frontmatter.get("title") or "")
    if not title:
        h1_match = H1_REGEX.search(body)
        if h1_match:
            title = h1_match.group(1).strip()
        else:
            title = Path(rel_path).stem.replace("-", " ").replace("_", " ").title()

    # 2. Tags extraction
    tags_set: set[str] = set()
    # Frontmatter tags
    fm_tags = frontmatter.get("tags") or frontmatter.get("tag") or []
    if isinstance(fm_tags, list):
        for t in fm_tags:
            tags_set.add(str(t).lstrip("#"))
    elif isinstance(fm_tags, str):
        tags_set.add(fm_tags.lstrip("#"))

    # Inline tags from body
    for match in TAG_REGEX.finditer(body):
        tags_set.add(match.group(1))

    # 3. Outgoing links extraction
    outgoing_links: list[Link] = []
    lines = body.splitlines()

    for line in lines:
        for match in WIKILINK_REGEX.finditer(line):
            target_raw = match.group(1).strip()
            heading = match.group(2).strip() if match.group(2) else None
            alias = match.group(3).strip() if match.group(3) else None

            # Normalize target id (strip .md if user typed [[note.md]])
            clean_target = target_raw[:-3] if target_raw.endswith(".md") else target_raw

            outgoing_links.append(
                Link(
                    source_id=note_id,
                    target_id=clean_target,
                    link_type="wikilink",
                    alias=alias,
                    heading=heading,
                    context_snippet=line.strip()[:160],
                )
            )

    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    stat = file_path.stat()

    return Note(
        id=note_id,
        path=rel_path,
        title=title,
        content=body,
        raw_text=raw_text,
        frontmatter=frontmatter,
        tags=sorted(list(tags_set)),
        outgoing_links=outgoing_links,
        mtime=stat.st_mtime,
        size_bytes=stat.st_size,
        content_hash=content_hash,
    )
