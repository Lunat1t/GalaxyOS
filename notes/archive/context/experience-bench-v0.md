> Исторический документ из legacy Amnesia. Не является действующим планом Galaxy. См. [решение об отказе от контекстного ядра](../../context-retirement.md).

# ExperienceBench v0

ExperienceBench запускает три изолированные группы Codex на одном и том же упорядоченном наборе задач
и снимке репозитория:

1. **baseline** — задача и доступ к репозиторию, без памяти/контекста Galaxy;
2. **raw** — задача, доступ к репозиторию и извлечённые записи эпизодов;
3. **compiled** — задача, доступ к репозиторию и ограниченный пакет контекста Galaxy.

Runner использует `codex exec --json` в эфемерных сессиях только для чтения. У каждой группы
собственное хранилище runtime; размеченные наблюдения становятся доступными только
после исходной задачи. `gold_prior` должен ссылаться на более ранние пары `(run_id, node_id)`.
Порядок групп детерминированно чередуется между задачами; ответы, потоки событий, журналы stderr
и `report.json` сохраняются в `--out-dir`.

## Набор данных

```json
{
  "project": "refresh-demo",
  "tasks": [
    {
      "id": "learn-contract",
      "task": "Review refresh token rotation and state what the repository specifies about retries.",
      "criteria": {"must_include": ["not specified"]},
      "observation": {
        "run_id": "review-1",
        "node_id": "retry-contract",
        "outcome": "success",
        "summary": "Captured the client retry contract for refresh rotation.",
        "lesson": "For the same idempotency key, return the original successor token; do not mint another.",
        "evidence": ["synthetic annotation; confirm with service owner"],
        "verified": false
      }
    },
    {
      "id": "apply-contract",
      "task": "A refresh request timed out and is retried with the same idempotency key. Recommend behavior and a regression test.",
      "gold_prior": [["review-1", "retry-contract"]],
      "criteria": {"must_include": ["same successor"], "must_not_include": ["repository proves this"]}
    }
  ]
}
```

`criteria` — это простые проверки подстрок ответа без учёта регистра, а не
проверка кода. Все термины `must_include` должны присутствовать; `must_include_any` — список
групп альтернативных терминов (достаточно одного совпадения в каждой группе); термины
`must_not_include` не должны встречаться. Предпочитайте несколько допустимых формулировок:
хрупкие точные подстроки могут неверно оценить семантически правильный ответ. Критерии не входят в prompt.
Наблюдения — аннотации бенчмарка, а не обучение, сгенерированное моделью; честно указывайте
статус доказательств. Не помечайте синтетические уроки как проверенные.

## Запуск

```bash
galaxy experience-bench tasks.json \
  --repo /path/to/frozen/repository \
  --out-dir benchmarks/experience-run-01 \
  --budget-tokens 5000 --max-files 16
```

Для каждой задачи в каждой группе Codex вызывается один раз. `report.json` содержит
успешность по критериям, количество извлечённых/эталонных эпизодов, оценку присоединённых токенов контекста,
счётчики input/cached-input/output токенов из JSONL Codex, время компиляции
и общее время, точность/полноту извлечения опыта, ложные добавления и число
извлечённых непроверенных эпизодов. Отсутствующий счётчик провайдера записывается как ноль,
но это не оценочное значение. Перед интерпретацией результатов изучите `limitations`.

Бенчмарк работает только для чтения и оценивает ответы, а не изменения кода или прохождение
детерминированных тестов. Все группы имеют доступ к репозиторию, поэтому Codex может изучать
файлы самостоятельно даже при наличии пакета. Небольшой успешный запуск диагностирует
механизм, но не доказывает общий эффект Galaxy. До выводов о результатах зафиксируйте текст задач
и коммиты, используйте несколько категорий ошибок и отложенные случаи, публикуйте все
группы, включая неудачные.
