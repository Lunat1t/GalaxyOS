# Тема 2: контекст, память и поиск по проекту

## 1. Контекст должен быть отобран, а не просто увеличен

Anthropic, [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents), 2025.

Материал описывает, почему способность модели принять длинный контекст не означает, что она одинаково хорошо использует каждую его часть. Авторы рекомендуют формировать небольшой набор наиболее полезных токенов для заданной цели, проектировать ясные инструменты и проверять влияние контекста. Это поддерживает приоритет Galaxy: измерять качество retrieval и итог задачи, а не стремиться передать модели весь репозиторий.

## 2. Гибридный поиск и контекст фрагментов

Anthropic, [Contextual Retrieval in AI Systems](https://www.anthropic.com/engineering/contextual-retrieval), 2024.

Публикация объясняет сочетание лексического BM25 и векторного поиска и показывает, что слишком маленький фрагмент может потерять связь с документом, из которого он взят. В описанном авторами эксперименте contextual embeddings и BM25 уменьшили ошибки retrieval@20; результаты относятся к их данным и конфигурации, не гарантируют такой же выигрыш Galaxy. Для roadmap отсюда следуют гипотезы о контекстных метаданных, сохранении происхождения фрагментов и paired evaluation на кодовых репозиториях.

## 3. Кэширование повторяющегося контекста

- Anthropic, [Contextual Retrieval — Using Prompt Caching](https://www.anthropic.com/engineering/contextual-retrieval#using-prompt-caching-to-reduce-the-costs-of-contextual-retrieval).
- Официальное руководство OpenAI по prompt caching: [Prompt caching](https://platform.openai.com/docs/guides/prompt-caching).

Кэширование может снизить повторные вычисления при повторно используемых префиксах и запросах. Эффект зависит от провайдера, модели, структуры запросов, правил свежести и тарифа. Galaxy должен считать фактические input/output/cached tokens и стоимость отдельно по провайдерам, проверяя, что кэш не обслуживает устаревший контекст.

## Проверка качества поиска и агентных систем

Anthropic, [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents), 2026.

Источник описывает тест-кейсы с критериями успеха, повторные попытки, grader-ы и полные trace для многошаговых агентов. Это помогает проверять техническое качество поиска и агентов. Продуктовую пользу для человека — время, удобство, повторные объяснения и ручные исправления — отдельно проверяем по [карточке 05](05-product-validation.md). Результат одной удачной демонстрации не является надёжной оценкой.

OpenAI, [Introducing SWE-bench Verified](https://openai.com/index/introducing-swe-bench-verified/), 2024, и [Why SWE-bench Verified no longer measures frontier coding capabilities](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/), 2025.

Первый материал объясняет тестирование на реальных issue и проверках до/после; более поздний материал описывает ограничения benchmark-а как сигнала о передовых coding способностях. Вывод для Galaxy: использовать несколько источников оценки, собственные фиксированные задачи и реальные пользовательские прогоны; не обещать преимущество по одному leaderboard.

## Инструменты должны быть понятны и ограничены

Anthropic, [Building Effective AI Agents](https://www.anthropic.com/engineering/building-effective-agents), 2024.

Материал различает простые LLM-workflow и более сложные агентные системы и рекомендует соотносить сложность оркестрации с задачей. Это технический ориентир для выбора сложности workflow; практическая ценность ролей и помощников описана в [карточке 04](04-custom-assistants.md) и должна проверяться на задачах.

Model Context Protocol, [MCP Server concepts](https://modelcontextprotocol.io/docs/concepts/architecture) и [официальная спецификация](https://modelcontextprotocol.io/specification/2025-11-25).

MCP задаёт общий интерфейс для подключения моделей к ресурсам и инструментам; он не решает за приложение вопросы безопасности, доверия, удобного согласия и изоляции. Конкретные security- и authorization-требования следует проверять по действующей версии спецификации перед реализацией.
