"""Small terminal workspace for selecting a Galaxy project."""
from __future__ import annotations

import json
import subprocess
import sys

from galaxy_core.projects import ProjectRegistry

VIEWS = ("overview", "project", "agents", "models", "integrations", "permissions", "settings")
VIEW_TITLES = {
    "overview": "РАБОЧАЯ ОБЛАСТЬ", "project": "ПРОЕКТ", "agents": "АГЕНТЫ",
    "models": "МОДЕЛИ", "integrations": "SKILLS / MCP",
    "permissions": "РАЗРЕШЕНИЯ", "settings": "НАСТРОЙКИ ПРОЕКТА",
}


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


def _draw(window, curses, registry: ProjectRegistry, selected: int, status: str, view: str) -> int:
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
    window.addnstr(1, 0, "Текущий: " + (registry.current().name if registry.current() else "не выбран"), split - 2)
    window.addnstr(2, 0, "Проекты", split - 2, curses.A_BOLD)
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
    window.addnstr(1, split + 2, VIEW_TITLES.get(view, VIEW_TITLES["overview"]),
                   width - split - 3, curses.A_BOLD)
    project = projects[selected] if projects else registry.current()
    if project:
        window.addnstr(3, split + 2, f"{project.name}  ·  {project.id[:8]}", width - split - 3, curses.A_BOLD)
        settings = project.settings
        if view == "project":
            lines = [f"Путь: {project.path}", f"Git: {_branch(project.path)}",
                     f"Создан: {project.created_at}",
                     f"Последнее открытие: {project.last_opened_at or 'ещё не открывали'}"]
        elif view == "agents":
            lines = [f"Агент по умолчанию: {settings.get('default_agent_id', 'не выбран')}",
                     "Постоянные профили подключим следующим шагом."]
        elif view == "models":
            lines = [f"Провайдер: {settings.get('default_provider', 'не выбран')}",
                     f"Профиль модели: {settings.get('model_profile', 'не выбран')}"]
        elif view == "integrations":
            lines = ["Skills: " + (", ".join(settings.get("skills", [])) or "не выбраны"),
                     "MCP: " + (", ".join(settings.get("mcp_servers", [])) or "не настроен"),
                     "Подключение этих каталогов ещё не реализовано."]
        elif view == "permissions":
            permissions = settings.get("permissions", {})
            lines = [f"{key}: {value}" for key, value in sorted(permissions.items())]
            if not lines:
                lines = ["Разрешения не заданы."]
            lines.append("Эти настройки пока не ограничивают инструменты.")
        elif view == "settings":
            lines = json.dumps(settings, ensure_ascii=False, indent=2).splitlines()
            if not lines:
                lines = ["Настройки пока не заданы."]
        else:
            lines = [f"Путь: {project.path}", f"Git: {_branch(project.path)}",
                     f"Исполнитель: {settings.get('default_agent_id', 'агент не выбран')}",
                     "Нажмите T, чтобы записать новый запрос.",
                     "Запрос сохранится в очереди проекта; запуск агента добавим позже."]
        for index, line in enumerate(lines[:max(0, height - 8)]):
            window.addnstr(4 + index, split + 2, line, width - split - 3)
        if view == "overview":
            tasks = registry.list_tasks(project.id, limit=max(1, min(6, height - 8)))
            task_y = min(height - 4, 8)
            window.addnstr(task_y, 0, "ЗАДАЧИ", split - 2, curses.A_BOLD)
            if not tasks:
                window.addnstr(task_y + 1, 0, "Пока нет задач", split - 2)
            for index, task in enumerate(tasks):
                label = f"[{task.status}] {task.request}"
                y = task_y + 1 + index
                if y < height - 3:
                    window.addnstr(y, 0, label, split - 2)
        if view == "settings":
            settings_command = f"Изменить: galaxy project settings {project.id} --set '{{}}'"
            window.addnstr(height - 4, split + 2, settings_command,
                           width - split - 3, curses.A_DIM)
    else:
        window.addnstr(3, split + 2, "Откройте каталог проекта клавишей O.", width - split - 3)
    window.addnstr(height - 2, 0, "↑/↓ проект   Enter открыть выбранный   O каталог   A добавить   T задача   Tab раздел   Q выход",
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
    view = "overview"
    while True:
        count = _draw(window, curses, registry, selected, status, view)
        key = window.get_wch()
        status = ""
        if key in ("q", "Q", "\x1b"):
            return 0
        if key in (curses.KEY_UP, "k"):
            selected = max(0, selected - 1)
        elif key in (curses.KEY_DOWN, "j"):
            selected = min(max(0, count - 1), selected + 1)
        elif key in ("s", "S"):
            view = "overview" if view == "settings" else "settings"
        elif key == "\t":
            view = VIEWS[(VIEWS.index(view) + 1) % len(VIEWS)]
        elif key in ("t", "T"):
            projects = registry.list_recent()
            if projects:
                request = _prompt(window, curses, "Новая задача: ")
                if request:
                    try:
                        task = registry.create_task(projects[selected].id, request)
                        status = "Сохранена, ожидает агента: " + task.id[:8]
                        view = "overview"
                    except (ValueError, OSError, KeyError) as exc:
                        status = str(exc)
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
        choice = input("[o] открыть, [a] добавить, [t] новая задача, [q] выход: ").strip().lower()
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
        elif choice == "t":
            project = registry.current()
            request = input("Новая задача: ").strip()
            if project and request:
                try:
                    task = registry.create_task(project.id, request)
                    print(f"Сохранена, ожидает агента: {task.id}")
                except (ValueError, OSError, KeyError) as exc:
                    print(f"Ошибка: {exc}", file=sys.stderr)
            elif not project:
                print("Сначала откройте проект.", file=sys.stderr)
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
