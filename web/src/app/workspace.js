"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

async function requestJson(url, options) {
  const response = await fetch(url, {
    ...options,
    cache: "no-store",
    headers: options?.body
      ? { "Content-Type": "application/json", ...options.headers }
      : options?.headers,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error?.message || `Request failed (${response.status})`);
  }
  return payload;
}

function shortDate(value) {
  if (!value) return "только что";
  return new Intl.DateTimeFormat("ru", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
    .format(new Date(value));
}

export default function Workspace() {
  const [projects, setProjects] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [tasks, setTasks] = useState([]);
  const [selectedTaskId, setSelectedTaskId] = useState(null);
  const [activeRunId, setActiveRunId] = useState(null);
  const [run, setRun] = useState(null);
  const [runEvents, setRunEvents] = useState([]);
  const [request, setRequest] = useState("");
  const [provider, setProvider] = useState("codex");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const activeProject = useMemo(
    () => projects.find((project) => project.id === activeId) || null,
    [projects, activeId],
  );
  const selectedTask = useMemo(
    () => tasks.find((task) => task.id === selectedTaskId) || tasks[0] || null,
    [tasks, selectedTaskId],
  );
  const selectedProvider = activeProject?.settings?.default_provider || "codex";
  const visibleRunId = activeRunId || selectedTask?.latest_run_id || null;

  useEffect(() => {
    setProvider(activeProject?.settings?.default_provider || "codex");
  }, [activeProject?.id, activeProject?.settings?.default_provider]);

  const loadProjects = useCallback(async (preferId) => {
    const payload = await requestJson("/api/v1/projects?limit=20");
    const rows = payload.projects || [];
    setProjects(rows);
    const nextId = preferId || payload.active_project_id || rows[0]?.id || null;
    setActiveId(nextId);
    return nextId;
  }, []);

  const loadTasks = useCallback(async (projectId) => {
    if (!projectId) {
      setTasks([]);
      setSelectedTaskId(null);
      return;
    }
    const payload = await requestJson(`/api/v1/projects/${encodeURIComponent(projectId)}/tasks?limit=30`);
    setTasks(payload.tasks || []);
    setSelectedTaskId((current) => (payload.tasks || []).some((task) => task.id === current)
      ? current
      : payload.tasks?.[0]?.id || null);
  }, []);

  useEffect(() => {
    loadProjects()
      .catch((cause) => setError(cause.message))
      .finally(() => setLoading(false));
  }, [loadProjects, loadTasks]);

  useEffect(() => {
    if (!loading) loadTasks(activeId).catch((cause) => setError(cause.message));
  }, [activeId, loading, loadTasks]);

  useEffect(() => {
    setActiveRunId(null);
    setRun(null);
    setRunEvents([]);
  }, [selectedTaskId]);

  useEffect(() => {
    if (!visibleRunId) return undefined;
    let stopped = false;
    let timer;
    async function refreshRun() {
      try {
        const [runPayload, eventPayload] = await Promise.all([
          requestJson(`/api/v1/runs/${encodeURIComponent(visibleRunId)}`),
          requestJson(`/api/v1/runs/${encodeURIComponent(visibleRunId)}/events?after=0`),
        ]);
        if (!stopped) {
          setRun(runPayload.run);
          setRunEvents(eventPayload.events || []);
          if (runPayload.run.status !== "running" && timer) window.clearInterval(timer);
          if (runPayload.run.status !== "running") {
            loadTasks(activeId).catch((cause) => setError(cause.message));
          }
        }
      } catch (cause) {
        if (!stopped) setError(cause.message);
      }
    }
    refreshRun();
    timer = window.setInterval(refreshRun, 1500);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [visibleRunId, activeId, loadTasks]);

  async function chooseProject(projectId) {
    setBusy(true);
    setError("");
    try {
      await requestJson(`/api/v1/projects/${encodeURIComponent(projectId)}/open`, { method: "POST", body: "{}" });
      await loadProjects(projectId);
    } catch (cause) {
      setError(cause.message);
    } finally {
      setBusy(false);
    }
  }

  async function registerProject(projectPath) {
    const payload = await requestJson("/api/v1/projects", {
      method: "POST",
      body: JSON.stringify({ path: projectPath }),
    });
    const projectId = payload.project.id;
    await requestJson(`/api/v1/projects/${encodeURIComponent(projectId)}/open`, { method: "POST", body: "{}" });
    await loadProjects(projectId);
  }

  async function chooseProjectFolder() {
    setBusy(true);
    setError("");
    try {
      const selection = await requestJson("/api/v1/projects/choose", {
        method: "POST",
        body: "{}",
      });
      if (selection.path) await registerProject(selection.path);
    } catch (cause) {
      setError(cause.message);
    } finally {
      setBusy(false);
    }
  }

  async function addTask(event) {
    event.preventDefault();
    if (!activeProject || !request.trim()) return;
    setBusy(true);
    setError("");
    try {
      const payload = await requestJson(`/api/v1/projects/${encodeURIComponent(activeProject.id)}/tasks`, {
        method: "POST",
        body: JSON.stringify({ request: request.trim() }),
      });
      setTasks((current) => [payload.task, ...current]);
      setSelectedTaskId(payload.task.id);
      setRequest("");
    } catch (cause) {
      setError(cause.message);
    } finally {
      setBusy(false);
    }
  }

  async function executeTask() {
    if (!activeProject || !selectedTask) return;
    setBusy(true);
    setError("");
    try {
      const payload = await requestJson(
        `/api/v1/projects/${encodeURIComponent(activeProject.id)}/tasks/${encodeURIComponent(selectedTask.id)}/run`,
        { method: "POST", body: "{}" },
      );
      setActiveRunId(payload.run.id);
      await loadTasks(activeProject.id);
    } catch (cause) {
      setError(cause.message);
    } finally {
      setBusy(false);
    }
  }

  async function cancelRun() {
    if (!visibleRunId) return;
    setBusy(true);
    setError("");
    try {
      await requestJson(`/api/v1/runs/${encodeURIComponent(visibleRunId)}/cancel`, {
        method: "POST", body: "{}",
      });
    } catch (cause) {
      setError(cause.message);
    } finally {
      setBusy(false);
    }
  }

  async function saveSettings(event) {
    event.preventDefault();
    if (!activeProject) return;
    setBusy(true);
    setError("");
    try {
      await requestJson(`/api/v1/projects/${encodeURIComponent(activeProject.id)}/settings`, {
        method: "PATCH",
        body: JSON.stringify({ default_provider: provider }),
      });
      await loadProjects(activeProject.id);
      setSettingsOpen(false);
    } catch (cause) {
      setError(cause.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand-row">
          <div className="brand-mark">G</div>
          <div><strong>Galaxy</strong><span>CODE WORKSPACE</span></div>
        </div>

        <div className="sidebar-label">РАБОЧЕЕ ПРОСТРАНСТВО</div>
        <button className="nav-item nav-active" type="button"><span>▦</span> Рабочая область</button>
        <button className="nav-item" type="button" disabled><span>✳</span> Агенты <small>скоро</small></button>
        <button className="nav-item" type="button" disabled><span>◉</span> Память <small>скоро</small></button>

        <div className="section-heading">
          <span>ПРОЕКТЫ</span>
          <span className="count-pill">{projects.length}</span>
        </div>
        <div className="project-list">
          {projects.map((project) => (
            <button
              className={`project-item ${project.id === activeId ? "project-selected" : ""}`}
              key={project.id}
              onClick={() => chooseProject(project.id)}
              type="button"
              disabled={busy}
              title={project.path}
            >
              <span className="project-icon">⌘</span>
              <span className="project-name">{project.name}</span>
              {project.is_active && <span className="active-dot" aria-label="Открыт" />}
            </button>
          ))}
          {!projects.length && !loading && <p className="muted small-note">Пока нет проектов</p>}
        </div>

        <div className="add-project">
          <span className="add-project-title">Добавить проект</span>
          <button className="button button-secondary full-width" type="button" onClick={chooseProjectFolder} disabled={busy}>
            <span>＋</span> Выбрать папку
          </button>
        </div>

        <div className="sidebar-bottom">
          <span className="status-dot" /> Локальный режим
          <span className="version-label">v1.2.4 · PREVIEW</span>
        </div>
      </aside>

      <section className="main-panel">
        <header className="topbar">
          <div className="breadcrumbs"><span>Рабочая область</span><b>/</b><strong>{activeProject?.name || "Выберите проект"}</strong></div>
          <div className="topbar-actions">
            <select
              className="mobile-project-select"
              aria-label="Выбрать проект"
              value={activeId || ""}
              onChange={(event) => event.target.value && chooseProject(event.target.value)}
              disabled={busy}
            >
              {!projects.length && <option value="">Нет проектов</option>}
              {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
            </select>
            <button className="settings-button" type="button" onClick={() => setSettingsOpen(true)} disabled={!activeProject} aria-label="Настройки проекта">
              <span>⚙</span> Настройки
            </button>
            <div className="local-badge"><span className="status-dot" /> Данные на этом устройстве</div>
          </div>
        </header>

        <div className="workspace-content">
          {error && <div className="notice notice-error" role="alert">{error}</div>}
          {loading ? (
            <div className="empty-state"><div className="spinner" /><p>Подключаюсь к Galaxy…</p></div>
          ) : !activeProject ? (
            <div className="empty-state welcome-state">
              <div className="empty-icon">⌘</div>
              <p className="eyebrow">ВАШЕ ЛОКАЛЬНОЕ ПРОСТРАНСТВО</p>
              <h1>Начните с проекта</h1>
              <p>Добавьте существующую папку. Galaxy сохранит проект и его задачи на этом устройстве.</p>
              <div className="welcome-form">
                <button className="button button-primary" type="button" onClick={chooseProjectFolder} disabled={busy}>Выбрать папку проекта <span>→</span></button>
              </div>
              <div className="local-note"><span>✦</span> Пока без подключения модели. Ваши данные остаются локальными.</div>
            </div>
          ) : (
            <>
              <div className="project-header">
                <div>
                  <p className="eyebrow">ПРОЕКТ</p>
                  <h1>{activeProject.name}</h1>
                  <div className="project-path" title={activeProject.path}><span>⌂</span> {activeProject.path}</div>
                </div>
                <div className="project-id">ID <code>{activeProject.id.slice(0, 8)}</code></div>
              </div>

              <div className="task-area">
                <div className="task-column">
                  <div className="section-heading task-heading"><span>ЗАДАЧИ</span><span className="count-pill">{tasks.length}</span></div>
                  {tasks.length ? (
                    <div className="task-list">
                      {tasks.map((task) => (
                        <button
                          key={task.id}
                          type="button"
                          className={`task-card ${task.id === selectedTask?.id ? "task-selected" : ""}`}
                          onClick={() => setSelectedTaskId(task.id)}
                        >
                          <div className="task-card-top"><span className={`queued-tag task-status-${task.status}`}><i /> {{ queued: "В очереди", running: "Выполняется", completed: "Готово", failed: "Ошибка", cancelled: "Отменено" }[task.status] || task.status}</span><span className="task-date">{shortDate(task.created_at)}</span></div>
                          <p>{task.request}</p>
                        </button>
                      ))}
                    </div>
                  ) : (
                    <div className="task-empty"><span>✦</span><p>Здесь появятся задачи проекта</p></div>
                  )}
                </div>

                <div className="conversation-column">
                  <div className="conversation-header">
                    <div><span className="agent-avatar">G</span><div><strong>Рабочая область</strong><small>Новый запрос</small></div></div>
                    <span className="status-chip">{run?.status === "running" ? "ВЫПОЛНЯЕТСЯ" : run?.status === "completed" ? "ЗАВЕРШЕНО" : run?.status === "failed" ? "ОШИБКА" : run?.status === "cancelled" ? "ОТМЕНЕНО" : "ОЖИДАЕТ ЗАПУСКА"}</span>
                  </div>

                  <div className="conversation-body">
                    {selectedTask ? (
                      <div className="message-row">
                        <div className="user-avatar">Вы</div>
                        <div className="message-content"><span className="message-author">Ваш запрос <time>{shortDate(selectedTask.created_at)}</time></span><p>{selectedTask.request}</p></div>
                      </div>
                    ) : (
                      <div className="conversation-empty">
                        <div className="sparkle">✳</div>
                        <h2>Что будем делать?</h2>
                        <p>Опишите задачу для проекта. Сейчас запрос сохранится в очереди; запуск агента подключим следующим этапом.</p>
                      </div>
                    )}
                      {runEvents.filter((event) => ["provider.message", "run.failed"].includes(event.event_type)).map((event) => (
                        <div className="message-row" key={event.sequence}><div className="agent-avatar">{event.event_type === "provider.message" ? "G" : "!"}</div><div className="message-content"><span className="message-author">{event.event_type === "provider.message" ? "Codex" : "Galaxy"}<time>Событие {event.sequence}</time></span><p>{event.payload.text || event.payload.message || event.payload.code}</p></div></div>
                      ))}
                      {runEvents.filter((event) => event.event_type === "provider.result").map((event) => (
                        <div className="queued-notice" key={event.sequence}><span>↗</span><div><strong>Результат и расход</strong><p>{event.payload.input_tokens ?? "—"} входных и {event.payload.output_tokens ?? "—"} выходных токенов</p></div></div>
                      ))}
                      <div className="queued-notice"><span className="status-dot" /><div><strong>{run ? `Запуск ${run.status}` : selectedTask ? "Задача сохранена" : "Агент готов"}</strong><p>{run ? "Ход работы и результат сохраняются в журнале задачи." : selectedTask ? "Запустите задачу, чтобы Codex выполнил её в отдельной рабочей копии." : "Создайте задачу для выбранного проекта."}</p></div></div>
                  </div>

                  <form className="composer" onSubmit={addTask}>
                    <textarea
                      value={request}
                      onChange={(event) => setRequest(event.target.value)}
                      placeholder="Опишите задачу для проекта…"
                      aria-label="Новая задача"
                      rows={3}
                    />
                    <div className="composer-footer"><span>↵ Запрос сохранится в очереди</span><div>{run?.status === "running" ? <button className="button button-secondary" type="button" onClick={cancelRun} disabled={busy}>Остановить</button> : selectedTask ? <button className="button button-primary" type="button" onClick={executeTask} disabled={busy || selectedTask.status === "running" || selectedProvider !== "codex"}>{selectedProvider === "codex" ? "Запустить Codex" : "Сначала выберите Codex"}</button> : null}<button className="button button-primary send-button" type="submit" disabled={busy || !request.trim()} aria-label="Сохранить задачу">Сохранить <span>↑</span></button></div></div>
                  </form>
                </div>
              </div>
            </>
          )}
        </div>
      </section>

      <aside className="inspector">
        <div className="inspector-header"><span>СВЕДЕНИЯ</span><button type="button" aria-label="Закрыть панель" disabled>×</button></div>
        {activeProject ? (
          <>
            <div className="inspector-section"><p className="eyebrow">ИСПОЛНИТЕЛЬ</p><div className="provider-card"><span className="provider-icon">✳</span><div><strong>{({ codex: "Codex", openrouter: "OpenRouter", agy: "Antigravity", "claude-code": "Claude Code" })[selectedProvider] || "Codex"}</strong><small>{selectedProvider === "codex" ? "Запуск в отдельной рабочей копии" : "Провайдер сохранён в настройках"}</small></div><span className="offline-tag">{selectedProvider === "codex" ? "LOCAL" : "LATER"}</span></div><p className="helper-text">{selectedProvider === "codex" ? "Запуск доступен через Codex CLI. Другие провайдеры подключим позже." : "Для запуска задачи выберите Codex в настройках проекта."}</p></div>
            <div className="inspector-divider" />
            <div className="inspector-section"><p className="eyebrow">ВЫБРАННАЯ ЗАДАЧА</p>{selectedTask ? <><h3 className="inspector-task">{selectedTask.request}</h3><div className="meta-row"><span>Статус</span><span className={`queued-tag task-status-${selectedTask.status}`}><i /> {{ queued: "В очереди", running: "Выполняется", completed: "Готово", failed: "Ошибка", cancelled: "Отменено" }[selectedTask.status] || selectedTask.status}</span></div><div className="meta-row"><span>Создана</span><span>{shortDate(selectedTask.created_at)}</span></div><div className="meta-row"><span>Task ID</span><code>{selectedTask.id.slice(0, 8)}</code></div></> : <p className="helper-text">Выберите задачу или создайте новую.</p>}</div>
            <div className="inspector-divider" />
            <div className="inspector-section"><p className="eyebrow">ПРОВЕРКИ И DIFF</p><div className="pending-block"><span>⌁</span><p>Появятся после подключения выполнения задачи.</p></div></div>
          </>
        ) : <p className="helper-text inspector-empty">Сведения проекта появятся после его выбора.</p>}
      </aside>

      {settingsOpen && activeProject && (
        <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && setSettingsOpen(false)}>
          <section className="settings-dialog" role="dialog" aria-modal="true" aria-labelledby="settings-title">
            <div className="dialog-heading"><div><p className="eyebrow">ПРОЕКТ</p><h2 id="settings-title">Настройки</h2></div><button type="button" onClick={() => setSettingsOpen(false)} aria-label="Закрыть">×</button></div>
            <form onSubmit={saveSettings}>
              <label className="field-label" htmlFor="default-provider">Провайдер по умолчанию</label>
              <select id="default-provider" className="provider-select" value={provider} onChange={(event) => setProvider(event.target.value)}>
                <option value="codex">Codex</option>
                <option value="openrouter">OpenRouter</option>
                <option value="agy">Antigravity CLI</option>
                <option value="claude-code">Claude Code</option>
              </select>
              <p className="dialog-help">Запуск задач сейчас поддерживает Codex CLI. Остальные провайдеры подключим отдельными адаптерами.</p>
              <div className="dialog-actions"><button className="button button-secondary" type="button" onClick={() => setSettingsOpen(false)}>Отмена</button><button className="button button-primary" type="submit" disabled={busy}>Сохранить</button></div>
            </form>
          </section>
        </div>
      )}
    </main>
  );
}
