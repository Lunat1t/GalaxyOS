"""One-way import of user data from Galaxy 1.x/early 2.0 layouts."""
from __future__ import annotations

from pathlib import Path
import shutil


def migrate_user_data(old_root: str | Path, new_root: str | Path) -> dict:
    """Copy knowledge into the clean v2 data boundary; never modifies the source."""
    old_root, new_root = Path(old_root).resolve(), Path(new_root).resolve()
    if old_root == new_root:
        raise ValueError("old_root must be a separate extracted Galaxy build")
    if not old_root.is_dir():
        raise ValueError(f"source does not exist: {old_root}")
    copied: list[str] = []
    skipped: list[str] = []
    old_db = old_root / "memory" / "index.db"
    new_db = new_root / "data" / "brain" / "index.db"
    if old_db.is_file():
        if new_db.exists():
            skipped.append("memory/index.db (destination already exists)")
        else:
            new_db.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old_db, new_db)
            copied.append("data/brain/index.db")
    vault = new_root / "data" / "vault"
    for folder in ("daily", "resources", "projects", "inbox"):
        source = old_root / folder
        if not source.is_dir():
            continue
        for item in source.rglob("*.md"):
            if item.is_symlink():
                continue
            target = vault / folder / item.relative_to(source)
            if target.exists():
                skipped.append(target.relative_to(new_root).as_posix())
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            copied.append(target.relative_to(new_root).as_posix())
    return {"source": str(old_root), "copied": copied, "skipped": skipped}
