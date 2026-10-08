"use client";

import { useEffect, useRef, useState } from "react";

const providers = [
  { id: "codex", name: "Codex CLI", ready: true },
  { id: "openrouter", name: "OpenRouter", ready: false },
  { id: "agy", name: "Antigravity CLI", ready: false },
  { id: "claude-code", name: "Claude Code", ready: false },
];

const efforts = ["low", "medium", "high"];
const effortLabels = { low: "Низкий", medium: "Средний", high: "Высокий" };

export default function ModelPicker({ value = "", effort = "low", disabled, onChange }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeProvider, setActiveProvider] = useState("codex");
  const rootRef = useRef(null);
  const searchRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const closeOnOutside = (event) => { if (!rootRef.current?.contains(event.target)) setOpen(false); };
    const closeOnEscape = (event) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("pointerdown", closeOnOutside);
    document.addEventListener("keydown", closeOnEscape);
    requestAnimationFrame(() => searchRef.current?.focus());
    return () => {
      document.removeEventListener("pointerdown", closeOnOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  const customModel = query.trim();
  const selectedName = value || "Автоматически";

  return (
    <div className="model-picker" ref={rootRef}>
      <button className="model-trigger" type="button" aria-haspopup="dialog" aria-expanded={open} disabled={disabled} onClick={() => setOpen((current) => !current)}>
        <span className="model-trigger-mark">✳</span><span className="model-trigger-copy"><strong>{selectedName}</strong><small>{providers.find((item) => item.id === activeProvider)?.name || "Codex CLI"}</small></span><span className="model-caret">⌄</span>
      </button>
      {open && <section className="model-popover" role="dialog" aria-label="Выбор провайдера и модели">
        <div className="model-search-wrap"><span aria-hidden="true">⌕</span><input ref={searchRef} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Найти модель или ввести ID" aria-label="Поиск модели" /></div>
        <div className="model-picker-main">
          <nav className="provider-rail" aria-label="Провайдеры">
            {providers.map((provider) => <button key={provider.id} type="button" className={`provider-rail-item ${activeProvider === provider.id ? "is-active" : ""}`} aria-pressed={activeProvider === provider.id} disabled={!provider.ready} title={provider.ready ? provider.name : `${provider.name} — скоро`} onClick={() => setActiveProvider(provider.id)}>
              <span>{provider.id === "codex" ? "✳" : provider.id === "openrouter" ? "◈" : provider.id === "agy" ? "◎" : "✦"}</span><i>{provider.ready ? provider.name : "Скоро"}</i>
            </button>)}
          </nav>
          <div className="model-list-area">
            <p className="model-provider-title">Codex CLI · локальный запуск</p>
            <button type="button" className={`model-option ${!value ? "is-selected" : ""}`} onClick={() => { onChange({ model: "", effort }); setQuery(""); setOpen(false); }}>
              <span className="model-option-icon">✳</span><span className="model-option-copy"><strong>Автоматический выбор</strong><small>Codex использует модель из своей настройки</small></span>{!value && <span className="model-check">✓</span>}
              <span className="model-capabilities"><i title="Рассуждение">◉</i><i title="Уровень рассуждений">↗</i></span>
            </button>
            {customModel && <button type="button" className={`model-option ${value === customModel ? "is-selected" : ""}`} onClick={() => { onChange({ model: customModel, effort }); setOpen(false); }}>
              <span className="model-option-icon">⌕</span><span className="model-option-copy"><strong>Использовать «{customModel}»</strong><small>Передать этот ID модели в Codex CLI</small></span>{value === customModel && <span className="model-check">✓</span>}
            </button>}
            {!customModel && value && <button type="button" className="model-option is-selected" onClick={() => { onChange({ model: value, effort }); setOpen(false); }}>
              <span className="model-option-icon">⌕</span><span className="model-option-copy"><strong>{value}</strong><small>Настроенный ID модели</small></span><span className="model-check">✓</span>
            </button>}
          </div>
        </div>
        <div className="model-effort"><div><span className="effort-symbol">◉</span><span>Уровень рассуждений</span></div><div className="effort-options" role="group" aria-label="Уровень рассуждений">
          {efforts.map((option) => <button key={option} type="button" aria-pressed={effort === option} className={effort === option ? "is-selected" : ""} onClick={() => onChange({ model: value, effort: option })}>{effortLabels[option]}</button>)}
        </div></div>
        <p className="model-picker-note">Для Codex настройки применяются к следующему запуску. Другие провайдеры пока не подключены.</p>
      </section>}
    </div>
  );
}
