"""Loopback-only JSON API for the Galaxy web client."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import shutil
import secrets
import socket
import subprocess
import sys
from urllib.parse import parse_qs, unquote, urlsplit

from galaxy_core.projects import ProjectRegistry
from galaxy_core.runs.manager import RunManager

MAX_BODY_BYTES = 64 * 1024


def choose_project_directory() -> str | None:
    """Open the operating system's folder picker on the local machine."""
    if sys.platform == "win32":
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            raise RuntimeError("Не удалось запустить системный выбор папки Windows.")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$dialog.Description = 'Выберите папку проекта Galaxy'; "
            "$dialog.ShowNewFolderButton = $false; "
            "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
            "{ [Console]::WriteLine($dialog.SelectedPath) }; $dialog.Dispose()"
        )
        command = [powershell, "-NoProfile", "-STA", "-Command", script]
    elif sys.platform == "darwin":
        command = ["osascript", "-e", 'POSIX path of (choose folder with prompt "Выберите папку проекта Galaxy")']
    else:
        zenity = shutil.which("zenity")
        kdialog = shutil.which("kdialog")
        if zenity:
            command = [zenity, "--file-selection", "--directory", "--title=Выберите папку проекта Galaxy"]
        elif kdialog:
            command = [kdialog, "--getexistingdirectory", str(Path.home()), "--title", "Выберите папку проекта Galaxy"]
        else:
            raise RuntimeError("Для выбора папки в Linux установите zenity или kdialog.")

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Диалог выбора папки слишком долго оставался открытым.") from exc
    except OSError as exc:
        raise RuntimeError("Не удалось открыть системный диалог выбора папки.") from exc

    output = result.stdout.strip()
    if result.returncode != 0:
        if sys.platform == "darwin" and "User canceled" in result.stderr:
            return None
        if sys.platform.startswith("linux") and result.returncode == 1:
            diagnostic = result.stderr.lower()
            display_errors = (
                "cannot open display",
                "could not connect to display",
                "failed to connect to display",
                "unable to open display",
                "failed",
                "error",
            )
            if not any(marker in diagnostic for marker in display_errors):
                return None
        if not result.stderr.strip():
            return None
        raise RuntimeError("Системный диалог выбора папки завершился с ошибкой.")
    if not output:
        return None
    selected = Path(output).expanduser().resolve()
    if not selected.is_dir():
        raise ValueError("Выбранная папка больше не существует.")
    return str(selected)


def _token_file(registry: ProjectRegistry, port: int) -> Path:
    return registry.data_dir / f"local-api-{port}.token"


def _create_token(registry: ProjectRegistry, port: int) -> str:
    """Create an owner-local token for the Next.js server to read."""
    path = _token_file(registry, port)
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token


class _ThreadingServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class GalaxyLocalApi:
    """Serve project and queued-task operations on the local machine only."""

    def __init__(self, registry: ProjectRegistry | None = None, host: str = "127.0.0.1",
                 port: int = 8765):
        if host not in {"127.0.0.1", "::1"}:
            raise ValueError("Galaxy API may only listen on a loopback address")
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        self.registry = registry or ProjectRegistry()
        self.runs = RunManager(self.registry)
        self.host = host
        self.port = port
        self.token = ""
        if host == "::1":
            class IPv6ThreadingServer(_ThreadingServer):
                address_family = socket.AF_INET6
            server_type = IPv6ThreadingServer
        else:
            server_type = _ThreadingServer
        self.httpd = server_type((host, port), self._handler_type())
        try:
            self.token = _create_token(self.registry, self.port)
        except Exception:
            self.httpd.server_close()
            raise

    def _handler_type(self):
        api = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "GalaxyLocalAPI/1"
            sys_version = ""

            def log_message(self, format, *args):
                # Request paths may contain local project data; keep them out of logs.
                return

            def _send(self, status: int, payload: dict | list):
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(raw)

            def _error(self, status: int, code: str, message: str):
                self._send(status, {"error": {"code": code, "message": message}})

            def _authorized(self) -> bool:
                host_header = self.headers.get("Host", "").lower()
                if host_header.startswith("["):
                    host = host_header[1:].split("]", 1)[0]
                else:
                    host = host_header.rsplit(":", 1)[0] if ":" in host_header else host_header
                if host not in {"127.0.0.1", "localhost", "::1"}:
                    self._error(403, "host_not_allowed", "Host header is not allowed")
                    return False
                supplied = self.headers.get("Authorization", "")
                prefix = "Bearer "
                if not supplied.startswith(prefix) or not hmac.compare_digest(
                        supplied[len(prefix):], api.token):
                    self._error(401, "unauthorized", "Local API authorization required")
                    return False
                return True

            def _body(self) -> dict:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 0 or size > MAX_BODY_BYTES:
                    raise ValueError("Request body is too large")
                if size == 0:
                    return {}
                if "application/json" not in self.headers.get("Content-Type", ""):
                    raise ValueError("Content-Type must be application/json")
                value = json.loads(self.rfile.read(size).decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("JSON body must be an object")
                return value

            def _route(self, method: str):
                path = urlsplit(self.path).path.rstrip("/") or "/"
                query = parse_qs(urlsplit(self.path).query)
                registry = api.registry
                if path == "/api/v1/health" and method == "GET":
                    return 200, {"status": "ok"}
                if path == "/api/v1/projects/choose" and method == "POST":
                    self._body()
                    return 200, {"path": choose_project_directory()}
                if path == "/api/v1/projects" and method == "GET":
                    limit = int(query.get("limit", ["20"])[0])
                    projects = [item.to_dict() for item in registry.list_recent(limit)]
                    current = registry.current()
                    return 200, {"projects": projects,
                                 "active_project_id": current.id if current else None}
                if path == "/api/v1/projects" and method == "POST":
                    body = self._body()
                    project_path = body.get("path")
                    if not isinstance(project_path, str) or not project_path.strip():
                        raise ValueError("path must be a non-empty string")
                    name = body.get("name")
                    if name is not None and not isinstance(name, str):
                        raise ValueError("name must be a string")
                    return 201, {"project": registry.add(project_path, name=name).to_dict()}

                parts = [unquote(part) for part in path.split("/") if part]
                if len(parts) >= 3 and parts[:2] == ["api", "v1"]:
                    if parts[2] == "runs" and len(parts) in {4, 5}:
                        run_id = parts[3]
                        if len(parts) == 4 and method == "GET":
                            return 200, {"run": api.runs.get(run_id)}
                        if len(parts) == 5 and parts[4] == "events" and method == "GET":
                            after = int(query.get("after", ["0"])[0])
                            if after < 0:
                                raise ValueError("after must be zero or greater")
                            return 200, {"events": api.runs.events(run_id, after)}
                        if len(parts) == 5 and parts[4] == "cancel" and method == "POST":
                            self._body()
                            return 200, {"run": api.runs.cancel(run_id)}
                    if (parts[2] == "projects" and len(parts) == 7 and parts[4] == "tasks"
                            and parts[6] == "run" and method == "POST"):
                        self._body()
                        return 202, {"run": api.runs.start(parts[3], parts[5])}
                    if parts[2] == "tasks" and len(parts) == 4 and method == "GET":
                        return 200, {"task": registry.get_task(parts[3]).to_dict()}
                    if parts[2] == "projects" and len(parts) in {4, 5}:
                        project_id = parts[3]
                        action = parts[4] if len(parts) == 5 else None
                        if action == "open" and len(parts) == 5 and method == "POST":
                            return 200, {"project": registry.open(project_id).to_dict()}
                        if action == "settings" and len(parts) == 5:
                            if method == "GET":
                                return 200, {"settings": registry.get(project_id).settings}
                            if method == "PATCH":
                                return 200, {"settings": registry.update_settings(
                                    project_id, self._body()).settings}
                        if action == "tasks" and len(parts) == 5:
                            registry.get(project_id)
                            if method == "GET":
                                limit = int(query.get("limit", ["20"])[0])
                                tasks = [item.to_dict() for item in registry.list_tasks(project_id, limit)]
                                return 200, {"tasks": tasks}
                            if method == "POST":
                                request = self._body().get("request")
                                if not isinstance(request, str):
                                    raise ValueError("request must be a string")
                                return 201, {"task": registry.create_task(project_id, request).to_dict()}
                        if action is None and method == "GET":
                            return 200, {"project": registry.get(project_id).to_dict()}
                return 404, {"error": {"code": "not_found", "message": "API route not found"}}

            def _handle(self):
                if not self._authorized():
                    return
                try:
                    status, payload = self._route(self.command)
                    self._send(status, payload)
                except KeyError:
                    self._error(404, "not_found", "Project or task was not found")
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._error(400, "invalid_request", str(exc))
                except RuntimeError as exc:
                    code = "folder_picker_unavailable" if urlsplit(self.path).path == "/api/v1/projects/choose" else "operation_unavailable"
                    self._error(503, code, str(exc))
                except OSError:
                    self._error(500, "internal_error", "Local API could not complete the request")
                except Exception:
                    self._error(500, "internal_error", "Local API could not complete the request")

            do_GET = _handle
            do_POST = _handle
            do_PATCH = _handle

        return Handler

    @property
    def token_path(self) -> Path:
        return _token_file(self.registry, self.port)

    def serve_forever(self):
        self.httpd.serve_forever(poll_interval=0.25)

    def shutdown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.token_path.unlink(missing_ok=True)


def serve_local_api(port: int = 8765) -> None:
    server = GalaxyLocalApi(port=port)
    print(f"Galaxy local API listening at http://127.0.0.1:{port}")
    print(f"Next.js server token file: {server.token_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
