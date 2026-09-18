#!/usr/bin/env python3
"""Galaxy 1.7.1 Model Context Protocol (MCP) Stdio Server.

Provides tools for ChatGPT, Codex, Antigravity, and other MCP clients
to directly control and inspect the Galaxy 1.7.1 core.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import solar
import chat
from system.agent_core import GalaxyOrchestrator, AGENT_SPECS, MemoryStore, GalaxyRuntime
from system.agent_core.storage import VERSION

TOOLS = [
    {
        "name": "galaxy_list_tasks",
        "description": "Получить список всех задач Galaxy с текущими стадиями, статусами и исполнителями.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False
        }
    },
    {
        "name": "galaxy_verify_task",
        "description": "Запустить воспроизводимую детерминированную QA-проверку задачи (Definition of Done) через команду verify_command.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Идентификатор задачи, например TASK-002"
                }
            },
            "required": ["task"],
            "additionalProperties": False
        }
    },
    {
        "name": "galaxy_chat_turn",
        "description": "Отправить сообщение или вопрос ядру Galaxy или конкретной планетной роли (Sun, Venera, Mars, Ceres, Earth, Neptun, Moon, Mercury) с контекстом задачи и памяти.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Сообщение или вопрос пользователю/агенту"
                },
                "role": {
                    "type": "string",
                    "description": "Опциональная роль: Sun, Venera, Mars, Ceres, Earth, Neptun, Moon, Mercury"
                },
                "task": {
                    "type": "string",
                    "description": "Опциональный ID задачи для передачи её контекста (например, TASK-002)"
                },
                "provider": {
                    "type": "string",
                    "enum": ["codex", "antigravity"],
                    "description": "CLI провайдер модели (по умолчанию codex)"
                }
            },
            "required": ["message"],
            "additionalProperties": False
        }
    },
    {
        "name": "galaxy_memory_search",
        "description": "Поиск в локальной базе знаний и памяти Galaxy (SQLite MemoryStore) по ключевым словам.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Поисковый запрос"
                },
                "agent": {
                    "type": "string",
                    "description": "Опциональный фильтр по агенту (Sun, Earth и др.)"
                },
                "project": {
                    "type": "string",
                    "description": "Опциональный фильтр по проекту"
                }
            },
            "required": ["query"],
            "additionalProperties": False
        }
    },
    {
        "name": "galaxy_runtime_status",
        "description": "Проверить статус очередей WorkerMesh, ожидающие сообщения и недавние события рантайма.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Опциональный фильтр по задаче"
                }
            },
            "additionalProperties": False
        }
    },
    {
        "name": "galaxy_audit",
        "description": "Запустить статический аудит безопасности и целостности репозитория Galaxy.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False
        }
    }
]


def handle_tool_call(name: str, args: dict) -> dict:
    try:
        if name == "galaxy_list_tasks":
            tasks = solar.discover_tasks()
            return {"tasks": tasks}

        elif name == "galaxy_verify_task":
            tid = args["task"].upper().strip()
            task = solar.get_task(tid)
            verify_cmd = task.get("verify_command")
            if not verify_cmd:
                return {"status": "NO_VERIFY_COMMAND", "task": tid, "message": "Task has no reproducible verify_command"}
            code, out, err = solar.run_verify(tid), "", ""
            return {
                "task": tid,
                "exit_code": code,
                "status": "PASS" if code == 0 else "FAIL"
            }

        elif name == "galaxy_chat_turn":
            msg = args["message"]
            role = args.get("role")
            task_id = args.get("task")
            provider = args.get("provider", "codex")
            task_ctx = chat.load_task_context(ROOT, task_id)
            cmd = chat.PROVIDERS.get(provider, chat.PROVIDERS["codex"])
            prompt = chat.build_prompt(ROOT, role, task_ctx, [], msg)
            reply = chat.call_provider(cmd, prompt, cwd=ROOT)
            return {
                "provider": provider,
                "role": role or "Core",
                "task": task_id,
                "reply": reply
            }

        elif name == "galaxy_memory_search":
            q = args["query"]
            agent = args.get("agent")
            project = args.get("project")
            mem = MemoryStore(ROOT)
            results = mem.search(q, agent=agent, project=project, limit=10)
            return {"query": q, "results": results}

        elif name == "galaxy_runtime_status":
            rt = GalaxyRuntime(ROOT, AGENT_SPECS)
            task_id = args.get("task")
            return {
                "pending": rt.pending(),
                "history": rt.history(task=task_id, limit=20)
            }

        elif name == "galaxy_audit":
            # Run local audit
            import io
            from contextlib import redirect_stdout
            f = io.StringIO()
            with redirect_stdout(f):
                code = solar.audit(as_json=True)
            output = f.getvalue().strip()
            return {"exit_code": code, "audit_output": output}

        else:
            return {"error": f"Unknown tool: {name}"}

    except Exception as exc:
        return {"error": str(exc)}


def _safe_json(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = req.get("method")
        msg_id = req.get("id")

        if method == "initialize":
            res = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "galaxy-1.7.1",
                    "version": VERSION
                }
            }
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": res}) + "\n")
            sys.stdout.flush()

        elif method == "tools/list":
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": TOOLS}
            }) + "\n")
            sys.stdout.flush()

        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            result = handle_tool_call(name, args)
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": _safe_json(result)
                        }
                    ]
                }
            }) + "\n")
            sys.stdout.flush()

        elif method == "ping":
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": {}}) + "\n")
            sys.stdout.flush()

        elif msg_id is not None:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}
            }) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
