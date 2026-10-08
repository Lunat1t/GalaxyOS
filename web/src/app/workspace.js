"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { defaultModelProviders, ModelPicker } from "@/components/ui/model-picker";

const AUTO_CODEX_MODEL = "galaxy-codex-auto";
const codexModelProviders = [{
  ...defaultModelProviders[0],
  name: "Codex CLI",
  models: [
    {
      id: AUTO_CODEX_MODEL,
      name: "Автоматически",
      description: "Модель выбирается в настройках Codex CLI",
      capabilities: ["reasoning"],
      thinking: ["low", "medium", "high"],
      defaultThinking: "low",
    },
    ...defaultModelProviders[0].models,
  ],
}];

async function requestJson(url, options) {
  const response = await fetch(url, {
    ...options,
    cache: "no-store",
    headers: options?.body
      ? { "Content-Type": "application/json", ...options.headers }
      : options?.headers,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error?.message || `Request failed (${response.status})`);
  return payload;
}

function timeLabel(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("ru", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

export default function Workspace() {
  const [projects, setProjects] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [tasks, setTasks] = useState([]);
  const [activeRunId, setActiveRunId] = useState(null);
  const [run, setRun] = useState(null);
  const [runEvents, setRunEvents] = useState([]);
  const [request, setRequest] = useState("");
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState("low");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const activeProject = useMemo(() => projects.find((item) => item.id === activeId) || null, [projects, activeId]);
  const latestTask = tasks[0] || null;
  const visibleRunId = activeRunId || latestTask?.latest_run_id || null;
  const answer = runEvents.findLast?.((item) => item.event_type === "provider.message")
    || [...runEvents].reverse().find((item) => item.event_type === "provider.message");
  const usage = [...runEvents].reverse().find((item) => item.event_type === "provider.result");

  const loadProjects = useCallback(async (preferId) => {
    const payload = await requestJson("/api/v1/projects?limit=20");
    const rows = payload.projects || [];
    setProjects(rows);
    const nextId = preferId || payload.active_project_id || rows[0]?.id || null;
    setActiveId(nextId);
    const selected = rows.find((item) => item.id === nextId);
    setModel(selected?.settings?.default_model || "");
    setEffort(selected?.settings?.reasoning_effort || "low");
    return nextId;
  }, []);

  const loadTasks = useCallback(async (projectId) => {
    if (!projectId) { setTasks([]); return; }
    const payload = await requestJson(`/api/v1/projects/${encodeURIComponent(projectId)}/tasks?limit=30`);
    setTasks(payload.tasks || []);
  }, []);

  useEffect(() => {
    loadProjects().catch((cause) => setError(cause.message)).finally(() => setLoading(false));
  }, [loadProjects]);

  useEffect(() => {
    if (!loading) loadTasks(activeId).catch((cause) => setError(cause.message));
  }, [activeId, loading, loadTasks]);

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
        if (stopped) return;
        setRun(runPayload.run);
        setRunEvents(eventPayload.events || []);
        if (runPayload.run.status !== "running") {
          if (timer) window.clearInterval(timer);
          loadTasks(activeId).catch((cause) => setError(cause.message));
        }
      } catch (cause) {
        if (!stopped) setError(cause.message);
      }
    }
    refreshRun();
    timer = window.setInterval(refreshRun, 1500);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [visibleRunId, activeId, loadTasks]);

  async function chooseProjectFolder() {
    setBusy(true);
    setError("");
    try {
      const selection = await requestJson("/api/v1/projects/choose", { method: "POST", body: "{}" });
      if (!selection.path) return;
      const payload = await requestJson("/api/v1/projects", {
        method: "POST", body: JSON.stringify({ path: selection.path }),
      });
      const projectId = payload.project.id;
      await requestJson(`/api/v1/projects/${encodeURIComponent(projectId)}/open`, { method: "POST", body: "{}" });
      setTasks([]); setActiveRunId(null); setRun(null); setRunEvents([]);
      await loadProjects(projectId);
    } catch (cause) {
      setError(cause.message);
    } finally { setBusy(false); }
  }

  async function saveModelSettings(projectId) {
    await requestJson(`/api/v1/projects/${encodeURIComponent(projectId)}/settings`, {
      method: "PATCH",
      body: JSON.stringify({
        default_provider: "codex",
        default_model: model === AUTO_CODEX_MODEL ? "" : model.trim(),
        reasoning_effort: effort,
      }),
    });
  }

  async function addTask(event) {
    event.preventDefault();
    if (!activeProject || !request.trim() || busy) return;
    setBusy(true); setError("");
    try {
      await saveModelSettings(activeProject.id);
      const payload = await requestJson(`/api/v1/projects/${encodeURIComponent(activeProject.id)}/tasks`, {
        method: "POST", body: JSON.stringify({ request: request.trim() }),
      });
      setTasks((current) => [payload.task, ...current]);
      setRequest(""); setRun(null); setRunEvents([]);
      const runPayload = await requestJson(
        `/api/v1/projects/${encodeURIComponent(activeProject.id)}/tasks/${encodeURIComponent(payload.task.id)}/run`,
        { method: "POST", body: "{}" },
      );
      setActiveRunId(runPayload.run.id);
      await loadTasks(activeProject.id);
    } catch (cause) { setError(cause.message); }
    finally { setBusy(false); }
  }

  async function cancelRun() {
    if (!visibleRunId) return;
    try {
      await requestJson(`/api/v1/runs/${encodeURIComponent(visibleRunId)}/cancel`, { method: "POST", body: "{}" });
    } catch (cause) { setError(cause.message); }
  }

  const statusText = run?.status === "running" ? "Codex работает" : run?.status === "completed" ? "Готово" : run?.status === "failed" ? "Запуск завершился с ошибкой" : run?.status === "cancelled" ? "Остановлено" : "Ответ появится здесь";

  return (
    <main className="ask-screen">
      <div className="ask-topline"><span className="ask-brand"><i>G</i> Galaxy</span><span className="ask-device"><b /> Локально на устройстве</span></div>

      <section className={`ask-content ${latestTask ? "has-conversation" : ""}`}>
        {loading ? (
          <p className="ask-muted">Подключаюсь к Galaxy…</p>
        ) : latestTask ? (
          <div className="ask-conversation" aria-live="polite">
            <article className="ask-user-message"><time>{timeLabel(latestTask.created_at)}</time><p>{latestTask.request}</p></article>
            <article className="ask-answer">
              <div className="ask-answer-heading"><span className="ask-glyph">G</span><strong>Codex</strong><span>{statusText}</span></div>
              {answer ? <p>{answer.payload.text}</p> : <p className="ask-muted">{run?.status === "running" ? "Изучаю проект и выполняю запрос…" : statusText}</p>}
              {run?.status === "running" && <button className="ask-stop" type="button" onClick={cancelRun}>Остановить</button>}
              {usage && <details className="ask-usage"><summary>Использование</summary><span>{usage.payload.input_tokens ?? "—"} входных · {usage.payload.output_tokens ?? "—"} выходных токенов</span></details>}
            </article>
          </div>
        ) : (
          <div className="ask-welcome"><div className="ask-orbit">✳</div><h1>О чём хотите спросить?</h1><p>Выберите папку проекта и поручите задачу Codex.</p></div>
        )}
      </section>

      <div className="ask-composer-area">
        {error && <div className="ask-error" role="alert">{error}</div>}
        <form className="ask-composer" onSubmit={addTask}>
          <textarea
            value={request}
            onChange={(event) => setRequest(event.target.value)}
            onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }}
            placeholder="Спросите о проекте или опишите задачу…"
            aria-label="Запрос к Galaxy"
            rows={3}
          />
          <div className="ask-composer-toolbar">
            <div className="ask-toolbar-left">
              <button className={`ask-project-chip ${activeProject ? "is-selected" : ""}`} type="button" onClick={chooseProjectFolder} disabled={busy} title={activeProject?.path || "Выбрать папку проекта"}>
                <span>⌂</span><span className="ask-project-name">{activeProject?.name || "Выбрать папку"}</span><span className="ask-chip-caret">⌄</span>
              </button>
              <ModelPicker
                providers={codexModelProviders}
                value={model || AUTO_CODEX_MODEL}
                thinking={effort}
                disabled={busy}
                placeholder="Выбрать модель"
                side="top"
                align="start"
                onValueChange={(modelId, _providerId, nextEffort) => {
                  setModel(modelId === AUTO_CODEX_MODEL ? "" : modelId);
                  if (nextEffort) setEffort(nextEffort);
                }}
              />
            </div>
            <div className="ask-toolbar-right"><span className="ask-shortcut">Enter ↵</span><button className="ask-send" type="submit" disabled={busy || loading || !activeProject || !request.trim()} aria-label="Отправить запрос">{busy ? "…" : "↑"}</button></div>
          </div>
        </form>
        <p className="ask-footnote">Codex CLI · Запросы запускаются в изолированной копии проекта</p>
      </div>
    </main>
  );
}
