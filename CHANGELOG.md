# Журнал изменений

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).
Проект придерживается [семантического версионирования](https://semver.org/lang/ru/).

## [0.3.8] — 2026-06-22

### Исправлено

- Парсеры транскриптов (`parse_claude_jsonl`, `parse_codex_jsonl`) больше не
  падают на повреждённом UTF-8. Раньше единственный битый байт в `.jsonl`
  вызывал `UnicodeDecodeError`, который не перехватывался ни `except
  json.JSONDecodeError`, ни внешним `except OSError`, и весь файл целиком
  выпадал из ingest на каждом `sync` (вечный статус `warn`, `errors=1`).
  Теперь файл открывается с `errors="replace"`: валидные строки ингестятся,
  пропускается только реально повреждённая. На практике вернуло 836 турнов из
  одного 9 МБ транскрипта, который ранее терялся полностью.

### Безопасность

- Недоверенная сохранённая память, возвращаемая `mem_search`, `mem_get_turn`,
  `mem_get_session` и `mem_entity`, теперь ограждается (fencing), чтобы
  отравленный турн не мог передать инструкции будущему агенту (prompt-injection).
- `restore`: отклоняются tar-элементы с обходом каталога и symlink-escape
  (`filter=data` на Python 3.12+, явная проверка членов на 3.11).
- `mem_get_turn`: введён лимит на объём текста одного турна и суммарного ответа
  (защита от переполнения контекста).

## [0.3.7] — 2026-06-04

### Исправлено

- Повреждённый HNSW-сегмент Chroma больше не роняет процесс sync/MCP. Вся работа
  с Chroma (инкрементальный embed и полная переиндексация) вынесена в изолированные
  дочерние процессы, поэтому нативный сбой (SIGSEGV в hnswlib на повреждённом
  индексе) возвращается как результат `chroma_index_corrupt` с просьбой
  переиндексировать, а не убивает весь процесс на каждом запуске. Первопричина —
  неатомарная запись HNSW в chromadb: процесс, убитый во время записи, оставлял
  файл метаданных индекса рассинхронизированным с бинарными `.bin`-файлами.

### Добавлено

- `anamnestic reindex` — атомарная полная пересборка векторного индекса: эмбеддинг
  во временный staging-каталог в дочернем процессе, валидация чтения без нативного
  сбоя, затем атомарная подмена через `os.rename`. Прерывание оставляет staging
  неполным, а рабочий индекс — нетронутым (устраняет рецидив повреждения).

### Тесты

- `tests/test_incremental_chroma.py`: обработка исходов воркера (segfault →
  `chroma_index_corrupt`, ненулевой код → error, успех → json), очистка staging
  и отсутствие подмены при сбое сборки, проба нечитаемого каталога.

## [0.3.6] — 2026-05-18

### Исправлено

- MCP auto-sync теперь перенаправляет stdout CLI-хелперов только для своего
  background thread, не подменяя stdout основного JSON-RPC транспорта.

### Тесты

- Добавлен regression-тест на гонку: пока auto-sync держит stdout redirect,
  запись из другого thread остается в stdout и не уходит в stderr.

## [0.3.5] — 2026-05-18

### Исправлено

- MCP auto-sync теперь принудительно перенаправляет stdout нижележащих CLI-хелперов
  в stderr, чтобы служебные сообщения миграций не попадали в stdio JSON-RPC
  поток и не ломали `initialize`.

### Тесты

- Добавлен regression-тест, который проверяет, что `_auto_sync()` не пишет в
  stdout даже если `run_migrations()` или ingest-хелпер используют `print()`.

## [0.3.4] — 2026-05-18

### Исправлено

- MCP startup больше не блокируется lightweight auto-sync: ingest запускается
  в фоне после старта процесса, поэтому клиент быстрее получает `initialize`.
- Wrapper-скрипты `scripts/anamnestic.sh` и `scripts/mcp_server.sh` больше не
  требуют локальную `.venv`: добавлен fallback на `ANAMNESTIC_PYTHON`,
  `~/.claude-mem/semantic-env/bin/python`, `python3`, `python`.

### Изменено

- `CLAUDE_MEM_DATA_DIR` теперь используется как data root по умолчанию, если
  не задан явный `ANAMNESTIC_DATA_DIR`. Это синхронизирует `anamnestic` с
  multi-profile установками свежего `claude-mem`.
- Документация обновлена под `claude-mem` v13: SQLite worker-режим остаётся
  совместимым, server-beta на Postgres/Redis является opt-in.

### Тесты

- Добавлены тесты приоритета `ANAMNESTIC_DATA_DIR` над `CLAUDE_MEM_DATA_DIR`.

## [0.2.0] — 2026-04-18

Первый версионированный релиз. Включает как базовую функциональность,
накопленную за предыдущие итерации, так и шесть новых улучшений поискового
пайплайна, вдохновлённых анализом SOTA-систем (Hindsight, HippoRAG, MemGPT).

### Базовая функциональность

- Инкрементальный ingest сессий из Claude Code, Codex CLI, VS Code Copilot.
- Гибридный поиск: BM25 (SQLite FTS5) + семантический (Chroma, MiniLM-L12-v2) с RRF-слиянием (K=60).
- MCP-сервер: `mem_search`, `mem_probe`, `mem_get_turn`, `mem_get_session`, `mem_get_thread`, `mem_stats`, `mem_entity`, `mem_audit_tail`.
- Инкрементальное встраивание через fastembed (ONNX, без torch).
- Извлечение сущностей (пути, URL) с regex-сайдкаром.
- Потоковая группировка сессий (threading) по проекту и 7-дневному порогу.
- Аудит-лог всех операций с пассивным сигналом релевантности.
- Резервное копирование / восстановление базы.

### Новое в этом релизе

- **Importance scoring** — эвристическая оценка важности каждого turn (0.0–1.0) по наличию кода, ошибок, решений, длине текста. Используется как множитель RRF-скора. Миграция 009.
- **Cross-encoder reranking** — финальная перескорировка top-20 кандидатов ONNX cross-encoder'ом (Xenova/ms-marco-MiniLM-L-6-v2). Ленивая загрузка, graceful fallback.
- **Temporal retrieval** — третий канал RRF, парсит временные выражения из запроса (EN/RU: «вчера», «на прошлой неделе», «last week», «in March») и достаёт turns из нужного периода.
- **Session summaries** — экстрактивные (без LLM) саммари сессий как слой наблюдений. Индексируются в FTS5, ищутся вместе с сырыми turns. Миграция 010.
- **Decay / consolidation** — экспоненциальное затухание по времени (настраиваемый период полураспада, по умолчанию 90 дней). Опциональная архивация старых low-importance turns. Миграция 011.
- **Entity graph** — граф совместной встречаемости сущностей в сессиях. BFS-обход графа как четвёртый канал RRF. Миграция 012.
- **Версионирование** — pyproject.toml с semver, журнал изменений.
- CLI-команда `anamnestic archive` для ручной архивации старых turns.

### Изменено

- `anamnestic sync` теперь дополнительно выполняет importance backfill, генерацию саммари и построение рёбер графа.
- Ответ `mem_search` MCP-инструмента включает поля `rerank_score`, `temporal_rank`, `graph_rank`, `hit_type`.
- Поисковый пайплайн: 4 канала RRF → importance weighting → decay factor → cross-encoder rerank.
- Entity graph: pruning рёбер с weight < 2 (шумоподавление), IDF-нормализация `weight / log2(degree + 1)` для подавления сущностей-хабов.
- MCP-сервер: auto-sync (ingest + embed) при запуске процесса — данные актуальны без ожидания cron-таймера.
- Ответ `mem_search` включает `diagnostics` с разбивкой хитов по каналам (bm25, semantic, temporal, graph, summaries).

### Тесты

- 16 интеграционных тестов RRF fusion pipeline: формула скора, multi-channel merge, importance weighting, temporal decay, graph channel, summary channel, merge-by-turn-id, diagnostics.
- 2 новых unit-теста entity graph: pruning low-weight edges, IDF normalization.
- Итого: 93 теста, все зелёные.
