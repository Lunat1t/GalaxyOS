"""Small terminal workspace for selecting a Galaxy project."""
from __future__ import annotations

import subprocess
import sys

from galaxy_core.projects import ProjectRegistry


def _branch(path: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", path, "branch", "--show-current"],
            capture_output=True, text=True, timeout=1, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else "not a Git repository"
    except (OSError, subprocess.TimeoutExpired):
        return "Git unavailable"


def _prompt(window, curses, label: str) -> str:
    height, width = window.getmaxyx()
    y = max(0, height - 2)
    window.move(y, 0)
    window.clrtoeol()
    window.addnstr(y, 0, label, max(1, width - 1))
    window.refresh()
    curses.echo()
    try:
        raw = window.getstr(y, min(len(label), max(0, width - 1)), max(1, width - len(label) - 1))
    finally:
        curses.noecho()
    return raw.decode(sys.stdin.encoding or "utf-8", errors="replace").strip()


def _draw(window, curses, registry: ProjectRegistry, selected: int, status: str) -> int:
    window.erase()
    height, width = window.getmaxyx()
    if height < 8 or width < 40:
        window.addnstr(0, 0, "Galaxy Code: окно слишком узкое. Увеличьте терминал.", max(1, width - 1))
        window.refresh()
        return 0

    projects = registry.list_recent()
    selected = max(0, min(selected, len(projects) - 1)) if projects else 0
    split = max(22, min(width // 3, 34))
    window.addnstr(0, 0, "GALAXY CODE  /  WORKSPACE", width - 1, curses.A_BOLD)
    window.addnstr(1, 0, "Проект: " + (registry.current().name if registry.current() else "не выбран"), split - 2)
    window.addnstr(2, 0, "Недавние проекты", split - 2, curses.A_BOLD)
    for index, project in enumerate(projects[:max(0, height - 7)]):
        marker = ">" if index == selected else " "
        active = " *" if project.is_active else ""
        label = f"{marker} {project.name}{active}"
        window.addnstr(3 + index, 0, label, split - 2,
                       curses.A_REVERSE if index == selected else curses.A_NORMAL)
    for y in range(1, height - 3):
        try:
            window.addch(y, split, curses.ACS_VLINE)
        except curses.error:
            pass
    window.addnstr(1, split + 2, "РАБОЧАЯ ОБЛАСТЬ", width - split - 3, curses.A_BOLD)
    project = projects[selected] if projects else registry.current()
    if project:
        window.addnstr(3, split + 2, f"{project.name}  ·  {project.id[:8]}", width - split - 3, curses.A_BOLD)
        window.addnstr(4, split + 2, project.path, width - split - 3)
        window.addnstr(5, split + 2, "Git: " + _branch(project.path), width - split - 3)
    else:
        window.addnstr(3, split + 2, "Откройте каталог проекта клавишей O.", width - split - 3)
    window.addnstr(7, split + 2, "Агент и задачи появятся на следующих шагах.", width - split - 3)
    window.addnstr(height - 2, 0, "↑/↓ выбрать   Enter открыть   O каталог   A добавить   Q выход",
                   width - 1, curses.A_DIM)
    if status:
        window.addnstr(height - 1, 0, status, width - 1)
    window.refresh()
    return len(projects)


def _curses_main(window, curses, registry: ProjectRegistry) -> int:
    curses.curs_set(0)
    window.keypad(True)
    selected = 0
    status = ""
    while True:
        count = _draw(window, curses, registry, selected, status)
        key = window.get_wch()
        status = ""
        if key in ("q", "Q", "\x1b"):
            return 0
        if key in (curses.KEY_UP, "k"):
            selected = max(0, selected - 1)
        elif key in (curses.KEY_DOWN, "j"):
            selected = min(max(0, count - 1), selected + 1)
        elif key in ("o", "O", "a", "A"):
            value = _prompt(window, curses, "Путь к каталогу: ")
            if value:
                try:
                    project = registry.open(value) if key in ("o", "O") else registry.add(value)
                    if key in ("a", "A"):
                        status = f"Добавлен: {project.name}"
                    else:
                        status = f"Открыт: {project.name}"
                        selected = 0
                except (ValueError, OSError) as exc:
                    status = str(exc)
        elif key in (curses.KEY_ENTER, "\n", "\r"):
            projects = registry.list_recent()
            if projects:
                try:
                    project = registry.open(projects[selected].id)
                    status = f"Открыт: {project.name}"
                except (ValueError, OSError) as exc:
                    status = str(exc)


def _line_mode(registry: ProjectRegistry) -> int:
    while True:
        projects = registry.list_recent()
        print("\nGALAXY CODE / WORKSPACE")
        for index, project in enumerate(projects, 1):
            active = " *" if project.is_active else ""
            print(f"{index}. {project.name}{active} — {project.path}")
        choice = input("[o] открыть путь или ID, [a] добавить, [q] выход: ").strip().lower()
        if choice == "q":
            return 0
        if choice in {"o", "a"}:
            value = input("Путь или ID проекта: ").strip()
            if not value:
                continue
            try:
                project = registry.open(value) if choice == "o" else registry.add(value)
                print(("Открыт: " if choice == "o" else "Добавлен: ") + project.path)
            except (ValueError, OSError) as exc:
                print(f"Ошибка: {exc}", file=sys.stderr)
        elif choice.isdigit() and 1 <= int(choice) <= len(projects):
            project = registry.open(projects[int(choice) - 1].id)
            print(f"Открыт: {project.path}")


def run_tui(registry: ProjectRegistry | None = None) -> int:
    registry = registry or ProjectRegistry()
    try:
        import curses
    except ImportError:
        return _line_mode(registry)
    try:
        return curses.wrapper(_curses_main, curses, registry)
    except curses.error:
        return _line_mode(registry)
