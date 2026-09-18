#!/usr/bin/env python3
"""Interactive chat REPL over Codex / Antigravity CLI providers with Galaxy 1.7.1 Core integration.

Features:
- Seamless multi-provider support: Codex (`codex exec`) and Google Antigravity (`agy -p`).
- 8 Galaxy planet personas (Sun, Venera, Mars, Ceres, Earth, Neptun, Moon, Mercury)
  with system prompts loaded from `system/agents/<role>.md` and `AGENT_SPECS`.
- Active Task Context: `/task TASK-002` loads task requirements and verification history.
- Local Knowledge Memory: Automatic context retrieval from Galaxy's SQLite `MemoryStore`.
- Session logging to Markdown via `/save`.

In-chat commands:
    /role [NAME]        switch persona (Sun, Venera, Mars, Ceres, Earth, Neptun, Moon, Mercury, or none)
    /provider [NAME]    switch between codex / antigravity
    /task [ID|none]     load task specification and evidence into chat context
    /memory [QUERY]     search Galaxy MemoryStore directly
    /save [path]        save conversation transcript to markdown
    /clear              clear chat history
    /help               show available commands
    /exit, /quit        exit chat
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent

# Ensure Galaxy system packages are discoverable
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from system.agent_core.agents import AGENT_SPECS, PROVIDERS, resolve_provider_command
    from system.agent_core.memory import MemoryStore
    from system.agent_core.storage import VERSION
except ImportError:
    VERSION = "1.7.1"
    PROVIDERS = {
        "codex": ["codex", "exec", "--skip-git-repo-check"],
        "antigravity": ["agy", "-p"],
    }
    AGENT_SPECS = {
        "Sun": {"purpose": "orchestrate and route the task; do not implement code", "tools": ["vault.read", "vault.search"]},
        "Venera": {"purpose": "turn the request into an explicit, testable specification", "tools": ["vault.read", "vault.append", "filesystem.read"]},
        "Mars": {"purpose": "analyze architecture, security, data boundaries and implementation risks", "tools": ["vault.read", "filesystem.read", "git.diff"]},
        "Ceres": {"purpose": "research local project context and gather evidence; do not invent missing facts", "tools": ["vault.read", "vault.search", "filesystem.read"]},
        "Earth": {"purpose": "implement the specification with minimal verified code changes", "tools": ["vault.read", "filesystem.read", "filesystem.write", "terminal.run", "git.diff"]},
        "Neptun": {"purpose": "review implementation for correctness, regressions and maintainability", "tools": ["filesystem.read", "filesystem.write", "terminal.run", "git.diff"]},
        "Moon": {"purpose": "independently execute QA and report evidence; never mark failing work as passed", "tools": ["filesystem.read", "terminal.run", "git.diff"]},
        "Mercury": {"purpose": "summarize already verified work; never change files or commit", "tools": ["vault.read", "filesystem.read", "git.diff", "git.status"]},
    }
    MemoryStore = None

    def resolve_provider_command(provider):
        configured = PROVIDERS[provider]
        binary = shutil.which(configured[0])
        if not binary:
            candidate = Path.home() / ".local" / "bin" / configured[0]
            binary = str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
        return [binary, *configured[1:]] if binary else None

HISTORY_LIMIT = 20


def load_role_prompt(root: Path, role: str | None) -> str:
    if not role or role not in AGENT_SPECS:
        return ""
    spec = AGENT_SPECS[role]
    doc_path = root / "system" / "agents" / f"{role.lower()}.md"
    doc_content = ""
    if doc_path.exists():
        try:
            doc_content = doc_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    purpose = spec.get("purpose", "")
    tools = ", ".join(spec.get("tools", []))
    lines = [
        f"You are {role}, a Galaxy 1.7.1 planet assistant.",
        f"Purpose: {purpose}.",
        f"Authorized tools / capabilities: {tools}."
    ]
    if doc_content:
        lines.append(f"\nRole guidelines:\n{doc_content}")
    lines.append("Maintain the assigned role, respect data boundaries, and answer clearly and constructively.")
    return "\n".join(lines)


def load_task_context(root: Path, task_id: str | None) -> dict | None:
    if not task_id:
        return None
    tid = task_id.upper().strip()
    if not re.fullmatch(r"TASK-[A-Z0-9][A-Z0-9_-]*", tid):
        return None
    path = root / "tasks" / f"{tid.lower()}.md"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    ctx = {"id": tid, "path": str(path.relative_to(root)), "text": text}
    # Check latest run info if available
    latest_run_file = root / "tasks" / tid.lower() / "latest-run.json"
    if latest_run_file.exists():
        try:
            latest_run = json.loads(latest_run_file.read_text(encoding="utf-8"))
            run_id = latest_run.get("run_id")
            if run_id:
                ctx["latest_run_id"] = run_id
                state_file = root / "tasks" / tid.lower() / "runs" / run_id / "state.json"
                if state_file.exists():
                    ctx["state"] = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return ctx


def retrieve_memories(root: Path, query: str, role: str | None = None, limit: int = 3) -> list[dict]:
    if not MemoryStore:
        return []
    try:
        store = MemoryStore(root)
        results = store.search(query, agent=role, limit=limit)
        if not results and role:
            results = store.search(query, limit=limit)
        return results or []
    except Exception:
        return []


def build_prompt(
    root: Path,
    role: str | None,
    task_ctx: dict | None,
    history: list[tuple[str, str]],
    user_msg: str,
    include_memory: bool = True
) -> str:
    parts = []

    # 1. System Persona
    if role:
        parts.append(load_role_prompt(root, role))
    else:
        parts.append("You are the Galaxy 1.7.1 Core AI assistant. You help manage tasks, code, tests, and planetary workflows.")

    # 2. Attached Task context
    if task_ctx:
        parts.append(f"\n<attached_task id=\"{task_ctx['id']}\" file=\"{task_ctx['path']}\">")
        parts.append(task_ctx["text"].strip())
        if "state" in task_ctx:
            st = task_ctx["state"]
            parts.append(f"\nTask Runtime State: status={st.get('status')} stage={st.get('stage')} assignee={st.get('assignee')} current_agent={st.get('current_agent')}")
            if st.get("history"):
                parts.append("Recent steps:")
                for h in st["history"][-3:]:
                    parts.append(f"  - [{h.get('agent')}]: {h.get('status')} -> {h.get('summary', '')}")
        parts.append("</attached_task>")

    # 3. Relevant Memories
    if include_memory and user_msg:
        memories = retrieve_memories(root, user_msg, role=role, limit=3)
        if memories:
            parts.append("\n<galaxy_memory_references>")
            for m in memories:
                parts.append(f" - [{m.get('project') or 'global'}] {m.get('content')} (agent={m.get('agent')}, qa_pass={m.get('qa_pass')})")
            parts.append("</galaxy_memory_references>")

    # 4. Transcript history
    if history:
        parts.append("\nConversation so far:")
        for speaker, text in history[-HISTORY_LIMIT:]:
            parts.append(f"{speaker}: {text}")

    # 5. User Turn
    parts.append(f"\nUser: {user_msg}")
    parts.append("Assistant:")
    return "\n".join(parts)


def call_provider(cmd: list[str], prompt: str, cwd: Path = ROOT, timeout: int = 300) -> str:
    if not (Path(cmd[0]).is_file() or shutil.which(cmd[0])):
        return f"[ошибка] CLI бинарник не найден: {cmd[0]}"
    try:
        env = dict(os.environ, VAULT_PATH=str(cwd.resolve()))
        result = subprocess.run([*cmd, prompt], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return f"[ошибка] превышено время ожидания ответа ({timeout} сек)"
    except Exception as exc:
        return f"[ошибка при вызове {cmd[0]}]: {exc}"

    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode != 0:
        return f"[провайдер завершился с кодом {result.returncode}]\n{err or out}"
    return out or "[пустой ответ]"


def save_log(path: Path, history: list[tuple[str, str]], provider: str, role: str | None, task_id: str | None) -> None:
    now_str = dt.datetime.now().isoformat(timespec="seconds")
    lines = [
        f"# Galaxy Chat Log — {now_str}",
        f"- **Provider**: {provider}",
        f"- **Role**: {role or 'Core Assistant'}",
        f"- **Task**: {task_id or 'none'}",
        "",
        "---",
        ""
    ]
    for speaker, text in history:
        lines.append(f"### {speaker}:\n{text}\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"(история сохранена в {path})")


def print_help():
    print("""Доступные команды:
  /role [NAME]      — сменить планетарную роль (Sun, Venera, Mars, Ceres, Earth, Neptun, Moon, Mercury, или none)
  /provider [NAME]  — сменить провайдера (codex или antigravity)
  /task [ID|none]   — привязать контекст задачи (например, /task TASK-002) или отвязать (/task none)
  /memory [QUERY]   — выполнить поиск в базе памяти Galaxy
  /save [PATH]      — сохранить лог беседы в Markdown файл
  /clear            — очистить текущую историю переписки
  /help             — показать эту справку
  /exit, /quit      — выйти из чата
""")


def chat_main(args=None) -> int:
    parser = argparse.ArgumentParser(description=f"Galaxy v{VERSION} Interactive Chat REPL")
    parser.add_argument("--provider", choices=sorted(PROVIDERS), default="codex", help="LLM CLI provider (codex or antigravity)")
    parser.add_argument("--role", choices=sorted(AGENT_SPECS), default=None, help="Galaxy planet role persona")
    parser.add_argument("--task", default=None, help="Attach task context (e.g. TASK-002)")
    parser.add_argument("--save", metavar="PATH", help="Save transcript to this file upon exit")
    parsed = parser.parse_args(args)

    provider = parsed.provider
    role = parsed.role
    task_id = parsed.task.upper() if parsed.task else None
    task_ctx = load_task_context(ROOT, task_id)
    configured_cmd = PROVIDERS[provider]
    cmd = resolve_provider_command(provider)

    binary = configured_cmd[0]
    if not cmd:
        print(f"Внимание: CLI бинарник '{binary}' для провайдера '{provider}' не найден в PATH.")
        alt_provider = "antigravity" if provider == "codex" else "codex"
        alt_cmd = resolve_provider_command(alt_provider)
        if alt_cmd:
            print(f"Автоматически переключаюсь на доступный провайдер: {alt_provider} ({alt_cmd[0]})")
            provider, cmd = alt_provider, alt_cmd
        else:
            print("Предупреждение: ни один из CLI провайдеров (codex, agy) не найден. Чат запустится, но вызовы завершатся ошибкой.")

    history: list[tuple[str, str]] = []

    print(f"=== Galaxy v{VERSION} Core Chat ===")
    display_binary = cmd[0] if cmd else binary
    print(f"Провайдер: {provider} ({display_binary})")
    print(f"Роль:      {role or 'Core Assistant'}")
    if task_ctx:
        print(f"Задача:    {task_ctx['id']} ({task_ctx['path']})")
    print("Введите сообщение или /help для списка команд (/exit для выхода).\n")

    while True:
        prompt_label = f"[{role or 'Core'}" + (f":{task_id}" if task_id else "") + f"] Вы> "
        try:
            user_msg = input(prompt_label).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nВыход из чата.")
            break

        if not user_msg:
            continue

        if user_msg in ("/exit", "/quit"):
            break

        if user_msg == "/help":
            print_help()
            continue

        if user_msg == "/clear":
            history.clear()
            print("(история очищена)")
            continue

        if user_msg.startswith("/role"):
            parts = user_msg.split(maxsplit=1)
            if len(parts) == 1:
                print(f"Текущая роль: {role or 'без роли'}. Доступно: {', '.join(sorted(AGENT_SPECS))}")
            else:
                target = parts[1].strip()
                match = [name for name in AGENT_SPECS if name.lower() == target.lower()]
                if match:
                    role = match[0]
                    print(f"(активная роль переключена на: {role})")
                elif target.lower() in ("none", "null", "reset", "clear"):
                    role = None
                    print("(роль сброшена: стандартный ассистент ядра)")
                else:
                    print(f"Неизвестная роль: {target}. Допустимые: {', '.join(sorted(AGENT_SPECS))}")
            continue

        if user_msg.startswith("/provider"):
            parts = user_msg.split(maxsplit=1)
            if len(parts) == 1:
                print(f"Текущий провайдер: {provider}. Доступно: {', '.join(sorted(PROVIDERS))}")
            else:
                target = parts[1].strip().lower()
                if target in PROVIDERS:
                    configured = PROVIDERS[target]
                    new_cmd = resolve_provider_command(target)
                    if not new_cmd:
                        print(f"Предупреждение: '{configured[0]}' не найден в PATH или ~/.local/bin.")
                        new_cmd = configured
                    provider, cmd = target, new_cmd
                    print(f"(провайдер переключен на: {provider})")
                else:
                    print(f"Неизвестный провайдер: {target}. Допустимые: {', '.join(sorted(PROVIDERS))}")
            continue

        if user_msg.startswith("/task"):
            parts = user_msg.split(maxsplit=1)
            if len(parts) == 1:
                if task_ctx:
                    print(f"Привязана задача: {task_ctx['id']} ({task_ctx['path']})")
                else:
                    print("Контекст задачи не привязан. Используйте `/task TASK-XXX`.")
            else:
                target = parts[1].strip().upper()
                if target.lower() in ("none", "null", "clear", "detach"):
                    task_id, task_ctx = None, None
                    print("(контекст задачи отвязан)")
                else:
                    loaded = load_task_context(ROOT, target)
                    if loaded:
                        task_id, task_ctx = target, loaded
                        print(f"(задача {target} привязана: {loaded['path']})")
                    else:
                        print(f"Задача {target} не найдена в {ROOT / 'tasks'}")
            continue

        if user_msg.startswith("/memory"):
            parts = user_msg.split(maxsplit=1)
            if len(parts) > 1:
                q = parts[1].strip()
                res = retrieve_memories(ROOT, q, role=role, limit=5)
                print(f"--- Результаты памяти для '{q}' ({len(res)}) ---")
                for m in res:
                    print(f"• [{m.get('project') or 'global'}] {m.get('content')} (agent={m.get('agent')}, qa_pass={m.get('qa_pass')})")
                print("---------------------------------------")
            else:
                print("Использование: /memory <поисковый запрос>")
            continue

        if user_msg.startswith("/save"):
            parts = user_msg.split(maxsplit=1)
            target = Path(parts[1].strip()) if len(parts) > 1 else Path(parsed.save or f"chat-log-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.md")
            save_log(target, history, provider, role, task_id)
            continue

        # Build prompt with role persona, task context, and relevant memories
        full_prompt = build_prompt(ROOT, role, task_ctx, history, user_msg)
        speaker_label = role or provider
        print(f"[{speaker_label}] думает...")
        reply = call_provider(cmd, full_prompt, cwd=ROOT)
        print(f"\n{speaker_label}>\n{reply}\n")

        history.append(("User", user_msg))
        history.append((speaker_label, reply))

    if parsed.save:
        save_log(Path(parsed.save), history, provider, role, task_id)

    return 0


if __name__ == "__main__":
    raise SystemExit(chat_main(sys.argv[1:]))
