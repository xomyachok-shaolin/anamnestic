# Персистентная память для AI-CLI сессий

Система, которая собирает историю разговоров с CLI-агентами (Claude Code, Codex, и в принципе любой агент, пишущий turn-based jsonl) в единый корпус, даёт по нему гибридный поиск и отдаёт его нативно обратно в те же клиенты через MCP.

На выходе:

- Единая SQLite-база + векторный индекс Chroma со всей историей независимо от клиента.
- Hybrid-поиск: BM25 (точные токены) + семантика (ONNX multilingual) + RRF-fusion.
- MCP-инструменты (`mem_search`, `mem_probe`, `mem_entity`, `mem_get_thread`, `mem_get_turn`, `mem_get_session`, `mem_stats`, `mem_audit_tail`), доступные в любом MCP-совместимом клиенте. `mem_probe(term)` — точный FTS-счётчик; `mem_search` — ранжирующий поиск; `mem_entity(value)` — scoped lookup по сущностям; `mem_get_thread(session_id)` — цепочка продолжений сессии. Каждый вызов логируется в `anamnestic_audit`; `mem_audit_tail` возвращает хвост для интроспекции.
- Инкрементальный sync новых сессий и ежедневные бэкапы через systemd user-таймеры.
- Команда `anamnestic restore` для отката и переезда на другую машину.

Платформа: Linux с `systemd` user-режима. Всё работает offline, embedding — локальный ONNX.

---

## 0. Подход

### Архитектурная позиция

Это **агент-нейтральный слой поверх jsonl-транскриптов**. Любой CLI-агент, который сохраняет диалог в формате «один файл = одна сессия, каждая реплика — отдельная строка», подключается через собственный парсер. Сегодня поддержаны два источника — **Claude Code** (main + subagent jsonl) и **Codex CLI**. Добавление третьего (Aider, Cursor agent, collective, свой собственный CLI) — это написать парсер и указать его директорию в конфиге.

Поверх собранного корпуса живут **три слоя поиска** и **три контура эксплуатации**, одинаковые для всех источников.

### Источники

- **Claude Code main-сессии** — `~/.claude/projects/<cwd-slug>/<session-uuid>.jsonl`. По одному файлу на верхнеуровневую сессию.
- **Claude Code sub-agent транскрипты** — `~/.claude/projects/<cwd-slug>/<session-uuid>/subagents/*.jsonl`. Отдельные файлы на каждый запуск Explore / Plan / general-агента. Часто их в разы больше, чем main-сессий, и именно в них лежит содержательная аналитика.
- **Codex CLI сессии** — `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`. Другой формат: события `session_meta | turn_context | response_item | event_msg`, content как Python-repr строка.

Если собирать только один из источников — теряешь параллельный трек работы. Задача — привести гетерогенные форматы к одной схеме, сохраняя метку источника (`platform_source`) для фильтрации и аудита.

### Принцип сбора

1. **Файл — единица идемпотентности.** Каждый jsonl описан в `anamnestic_ingest_state` как `(source, path, mtime_ns)`. Повторный sync не перечитывает неизменённые файлы.
2. **Turn — единица хранения.** Таблица `historical_turns` хранит каждую реплику (user + assistant) с UNIQUE-ключом `(content_session_id, turn_number)`. UPSERT через `ON CONFLICT DO NOTHING` гарантирует отсутствие дублей.
3. **Формат — ответственность парсера.** Сейчас три парсера (Claude Code main, Claude Code subagent, Codex CLI); добавление нового источника = новый парсер в `anamnestic/ingest/` + запись в `incremental.py::_discover()`.
4. **Восстановление пропусков.** Если какая-то сессия попала в `sdk_sessions` без соответствующих `historical_turns` (такое бывает при миграции между версиями инструментов) — скрипт `recover_main` перечитывает jsonl для таких сессий и дозаливает.

### Слои поверх корпуса

1. **BM25 через SQLite FTS5** — для точных токенов: IP-адреса, CVE, имена файлов, коды ошибок, идентификаторы. Триггеры держат индекс в синке с базой.
2. **Семантика через Chroma + ONNX multilingual** — для смысловых запросов на естественном языке. Модель по умолчанию `paraphrase-multilingual-MiniLM-L12-v2` (384-dim, компромисс скорость / качество). Инкрементальный embedding: `anamnestic_embed_state` отмечает, что уже в Chroma.
3. **Hybrid через Reciprocal Rank Fusion** — объединяет ранги (не скоры — у них разные шкалы) по формуле `score(d) = Σ 1 / (60 + rank_r(d))`. BM25 поднимает точные имена, семантика поднимает близкое по смыслу, RRF склеивает.

Результаты — не текст, а **адресуемые объекты**: `turn_id`, `session_id`, `turn_number`, `timestamp`, `platform_source`. Окрестность поднимается через `mem_get_turn(turn_id, context=N)`, обзор сессии — через `mem_get_session(session_id)`.

### Точки входа

Один stdio-MCP сервер обслуживает всех клиентов, которые понимают MCP:

- Claude Code регистрирует его через `claude mcp add`.
- Codex — через запись в `~/.codex/config.toml`.
- Любой другой MCP-совместимый клиент — аналогично.
- Модель (~220 МБ) загружается один раз при старте процесса; последующие запросы — миллисекунды.

Параллельно есть CLI (`anamnestic`) для эксплуатации: sync, verify, backup, restore, audit, eval.

### Контуры эксплуатации

- **Incremental sync** по mtime — systemd-таймер подхватывает новое без полной пересборки.
- **Verify** — `PRAGMA integrity_check`, FTS rebuild, drift SQLite↔Chroma, orphans. Ловит деградацию до того, как мусор появится в результатах.
- **Audit log** (`anamnestic_audit`) — каждая операция с длительностью и JSON-payload'ом. Через полгода можно реконструировать, когда что сломалось.
- **WAL-safe backup** — tarball с ротацией. `restore` откатывает атомарно с сохранением предыдущего состояния в `*.pre-restore-*`.
- **Golden eval** — **твой** набор известных запросов с известными ответами. Без него нельзя сказать, стал ли поиск лучше или хуже после любого изменения.

### Отношение к `claude-mem`

`claude-mem` (плагин от thedotmack) создаёт базовую SQLite-схему и web-viewer; мы строим наш слой поверх его БД. Если пользуешься Claude Code — ставь его, получишь заодно живые хуки автозахвата. Если Claude Code нет, а нужен только Codex или другой клиент — можно пропустить установку плагина и создать базовую схему вручную (см. §3.1).

Актуальный `claude-mem` v13 добавил opt-in server-beta runtime на Postgres/Redis, но default worker-режим с SQLite остаётся совместимым. `anamnestic` работает именно с SQLite `claude-mem.db`. Для multi-profile установок `claude-mem` можно задавать `CLAUDE_MEM_DATA_DIR`; `anamnestic` автоматически использует его как data root, если не задан более сильный override `ANAMNESTIC_DATA_DIR`.

### Короткий маршрут

1. Поставить зависимости (Bun, uv, опционально claude-mem, Python venv).
   Выбрать install profile: `anamnestic` или `anamnestic[semantic]`.
2. Клонировать репо, прогнать миграции.
3. Перенести jsonl-ы на машину, если они уже есть где-то ещё.
4. Один раз `anamnestic sync` — собрать всё прошлое.
5. При пропусках (см. §16.1) — `recover_main`.
6. Зарегистрировать MCP во всех клиентах, которые будешь использовать.
7. Включить systemd-таймеры.

---

## 1. Предварительные требования

- `python3 ≥ 3.11`, `git`, `curl`, `sqlite3`.
- Node.js ≥ 20 и Bun нужны, если ставишь/запускаешь свежий `claude-mem`.
- Хотя бы один CLI-агент, чьи сессии хочешь индексировать (Claude Code CLI `claude`, Codex CLI `codex`, или свой).
- Свободное место: порядка 1% от суммарного размера jsonl плюс ~220 МБ модель и ~200 МБ на каждый бэкап.

Проверка:

```bash
python3 --version
sqlite3 --version
claude --version 2>/dev/null
codex --version 2>/dev/null
```

---

## 2. Установить Bun и uv

Bun нужен, только если ставишь плагин `claude-mem` (он на Bun). uv — быстрый менеджер Python-зависимостей без torch.

```bash
curl -fsSL https://bun.sh/install | bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
uv --version
```

---

## 3. Базовая SQLite-схема

Два пути — через плагин `claude-mem` (рекомендуется, если используешь Claude Code) или вручную.

### 3.a — через плагин (Claude Code есть)

```bash
npx -y claude-mem install
```

Создаст `~/.claude-mem/claude-mem.db` с базовой схемой, положит плагин в `~/.claude/plugins/marketplaces/thedotmack/`, пропишет хуки автозахвата в `~/.claude/settings.json`. Если используешь отдельный профиль, сначала выставь `CLAUDE_MEM_DATA_DIR=/path/to/profile`.

Опционально — запустить worker (web-viewer на `:37777`):

```bash
export PATH="$HOME/.bun/bin:$PATH"
nohup npx claude-mem start > /tmp/claude-mem-worker.log 2>&1 &
disown
```

### 3.b — вручную (Claude Code не используется)

Создать директорию и пустую БД:

```bash
mkdir -p ~/.claude-mem
sqlite3 ~/.claude-mem/claude-mem.db <<'SQL'
CREATE TABLE IF NOT EXISTS sdk_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_session_id TEXT UNIQUE NOT NULL,
    memory_session_id TEXT UNIQUE,
    project TEXT NOT NULL,
    platform_source TEXT NOT NULL DEFAULT 'claude',
    user_prompt TEXT,
    started_at TEXT NOT NULL,
    started_at_epoch INTEGER NOT NULL,
    completed_at TEXT,
    completed_at_epoch INTEGER,
    status TEXT CHECK(status IN ('active','completed','failed')) NOT NULL DEFAULT 'active',
    worker_port INTEGER,
    prompt_counter INTEGER DEFAULT 0,
    custom_title TEXT
);
CREATE TABLE IF NOT EXISTS user_prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_session_id TEXT NOT NULL,
    prompt_number INTEGER NOT NULL,
    prompt_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS session_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_session_id TEXT NOT NULL,
    project TEXT NOT NULL,
    request TEXT,
    investigated TEXT,
    learned TEXT,
    completed TEXT,
    next_steps TEXT,
    files_read TEXT,
    files_edited TEXT,
    notes TEXT,
    prompt_number INTEGER,
    discovery_tokens INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL
);
SQL
```

Остальные таблицы (`historical_turns`, `ext_*`, FTS) создадут наши миграции в §7.

---

## 4. Python venv и install profile

```bash
uv venv ~/.claude-mem/semantic-env --python 3.11
```

Дальше выбирается только профиль установки. Команды `anamnestic search`,
`anamnestic sync`, `mem_search` и остальные не меняются.

Минимальный профиль — SQLite/FTS/BM25 + temporal + graph:

```bash
uv pip install --python ~/.claude-mem/semantic-env/bin/python anamnestic
```

Семантический профиль — тот же интерфейс плюс Chroma/fastembed:

```bash
uv pip install --python ~/.claude-mem/semantic-env/bin/python 'anamnestic[semantic]'
```

Если ставишь из git checkout, после §5 используй editable-install:

```bash
uv pip install --python ~/.claude-mem/semantic-env/bin/python -e .
# или:
uv pip install --python ~/.claude-mem/semantic-env/bin/python -e '.[semantic]'
```

`sentence-transformers` не ставить — он тянет torch + CUDA. ONNX-backend через `fastembed` решает ту же задачу без гигабайт.
В semantic extra закреплены `chromadb==0.5.23`, `fastembed==0.5.1` и
`onnxruntime==1.24.4`: Chroma 1.x local Rust API на этой базе ловил native
SIGSEGV при `count/get/add`, а ONNX runtime должен быть воспроизводимым.

---

## 5. Получить репо

```bash
git clone <url> ~/projects/anamnestic
cd ~/projects/anamnestic
```

Проверка:

```
anamnestic/    # config.py, db.py, cli.py, audit.py, verify.py,
            # restore.py, backup.py, ingest/, indexers/, search/, eval/, daemon/
migrations/ # 001_fts_and_unique.sql, 002_incremental_state.sql, 003_audit_log.sql
systemd/    # *.service, *.timer
```

Ниже все команды подразумевают `cd ~/projects/anamnestic && export PYTHONPATH=$PWD`.

---

## 6. Перенести jsonl-историю (если уже есть)

Если ставишь на новой машине, но старая история где-то лежит:

```bash
# на старой:
tar czf history.tar.gz ~/.claude/projects/ ~/.codex/sessions/

# на новой (раскатает в $HOME):
tar xzf history.tar.gz -C /
```

Если истории нет — пропусти.

---

## 7. Миграции

```bash
~/.claude-mem/semantic-env/bin/python -m anamnestic.db
```

Ожидаемый вывод:

```
Applying 001_fts_and_unique.sql...
Applying 002_incremental_state.sql...
Applying 003_audit_log.sql...
Applied: 001_fts_and_unique.sql, 002_incremental_state.sql, 003_audit_log.sql
```

Проверка таблиц:

```bash
sqlite3 ~/.claude-mem/claude-mem.db ".tables" | tr ' ' '\n' | grep -E "ext_|historical_"
# должны быть: anamnestic_audit, anamnestic_embed_state, anamnestic_ingest_state, anamnestic_migrations,
# historical_turns, historical_turns_fts (+ служебные _fts_*)
```

---

## 8. Первичный бэкфилл всей истории

Одноразовая операция: читает jsonl из всех сконфигурированных директорий, наполняет `sdk_sessions`, `user_prompts`, `historical_turns`, `session_summaries`, затем эмбеддит в Chroma.

```bash
~/.claude-mem/semantic-env/bin/python -m anamnestic.cli sync
```

По умолчанию `sync` индексирует embedding чанком до 512 turns за запуск. Это держит systemd timer коротким и защищает long-running ONNX/Chroma процесс от native crash; повторный запуск продолжит с места. Для явного полного прохода в одном процессе можно использовать `--embed-limit 0`.

В конце печатает:

```json
{"ingest": {"total": N, "skipped": 0, "new_files": N, "new_turns": K, "errors": 0},
 "embed":  {"embedded": E, "elapsed": ...}}
```

### 8.1. Проверка целостности

```bash
~/.claude-mem/semantic-env/bin/python -m anamnestic.cli verify
```

Должно быть `"healthy": true`, `"issues": []`, `drift_state_vs_chroma = 0`.

Если `missing_embeddings > 0` — запусти `sync` ещё раз, дошлёт.

---

## 9. Проверить поиск

```bash
~/.claude-mem/semantic-env/bin/python -m anamnestic.cli search "любой запрос из твоего контекста" --top-k 10
```

Результат — turns с `session_id`, `timestamp`, `role`, `source` (claude / claude-subagent / codex / …), snippet. Если возвращает пусто — в корпусе нет того, что ищешь (проверь `anamnestic status` — `turns > 0`).

### 9.1. Регрессионный набор (golden)

В репо `anamnestic/eval/golden.yaml` лежит шаблон. На чужой истории он **не работает** — его надо заменить на 15–30 запросов под **свои** темы.

Формат:

```yaml
queries:
  - query: "текст запроса"
    any_keywords: ["слово1", "слово2"]   # хотя бы одно должно встретиться в хите
    min_hits: 1                          # минимум N хитов в top-K
    top_k: 10
```

Принцип: выбираешь темы, про которые точно знаешь, что они обсуждались. Формулируешь запросы так, как реально будешь искать (обобщённо, не точно-по-словам). В `any_keywords` — точные токены, которые должны оказаться в результатах.

Прогон:

```bash
~/.claude-mem/semantic-env/bin/python -m anamnestic.cli eval --mode hybrid
```

Смысл не в 100%, а в **baseline**. После любого изменения (смена модели, правка tokenizer, эксперимент с весами RRF) прогоняешь снова и сравниваешь числа.

---

## 10. Зарегистрировать MCP-сервер в клиентах

### 10.a — Claude Code

```bash
claude mcp add anamnestic ~/.claude-mem/semantic-env/bin/python \
    -e PYTHONPATH=$HOME/projects/anamnestic \
    -- -m anamnestic.daemon.mcp_server

claude mcp list   # должно быть "anamnestic ... ✓ Connected"
```

### 10.b — Codex CLI

```bash
cp ~/.codex/config.toml ~/.codex/config.toml.bak

cat >> ~/.codex/config.toml <<EOF

[mcp_servers.anamnestic]
command = "$HOME/.claude-mem/semantic-env/bin/python"
args = ["-m", "anamnestic.daemon.mcp_server"]
env = { PYTHONPATH = "$HOME/projects/anamnestic" }
EOF
```

(Если shell не раскроет `$HOME` в heredoc — подставь путь вручную.)

Проверить:

```bash
codex mcp list              # anamnestic, enabled=true
codex mcp get anamnestic       # детали
```

### 10.c — любой другой MCP-совместимый клиент

Конфигурация аналогична: stdio-транспорт, команда `python -m anamnestic.daemon.mcp_server`, env `PYTHONPATH`. Инструменты, которые экспонируются: `mem_search`, `mem_get_turn`, `mem_get_session`, `mem_stats`.

Все клиенты используют **один** SQLite и **одну** Chroma — дублировать данные не нужно.

---

## 11. Systemd user-таймеры

```bash
mkdir -p ~/.config/systemd/user
cp ~/projects/anamnestic/systemd/*.service \
   ~/projects/anamnestic/systemd/*.timer \
   ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now anamnestic-sync.timer
systemctl --user enable --now anamnestic-backup.timer

systemctl --user list-timers | grep anamnestic
```

Юниты:

- `anamnestic-sync.timer` — инкрементальный sync + WAL checkpoint.
- `anamnestic-backup.timer` — ежедневный WAL-safe snapshot DB + Chroma в `~/anamnestic-backups/` (ротация последних 10).

### 11.1. Работа без активной сессии

```bash
sudo loginctl enable-linger $USER
```

### 11.2. Проверка запуска

```bash
systemctl --user start anamnestic-sync.service
journalctl --user -u anamnestic-sync.service -n 30
```

---

## 12. Ежедневные команды

Если пакет установлен через `pip/uv pip install`, команда `anamnestic` уже доступна в venv.
Для работы прямо из checkout без editable-install можно оставить алиас:

```bash
alias anamnestic='PYTHONPATH=$HOME/projects/anamnestic $HOME/.claude-mem/semantic-env/bin/python -m anamnestic.cli'
```

```bash
anamnestic status            # сессии / turns / embedded / drift / last_ingest
anamnestic verify            # integrity SQLite + FTS + drift + orphans
anamnestic search "query" --top-k 10
anamnestic sync              # вручную (обычно делает timer; embedding чанками)
anamnestic backup            # вручную (обычно делает timer)
anamnestic audit --limit 20  # последние операции с timestamps
anamnestic eval --mode hybrid
anamnestic restore ~/anamnestic-backups/<tarball>.tar.gz
```

Обычный пользовательский режим — `ANAMNESTIC_SEMANTIC=auto`: команды те же, а
`status`, `verify`, `search` и MCP-ответы показывают capability metadata
(`capabilities.semantic`, `diagnostics.channels_used`). Если Chroma/fastembed
недоступны или индекс ещё догоняется, поиск остаётся на SQLite/FTS/BM25,
temporal и graph без смены интерфейса.

Строгая проверка semantic-индекса для эксплуатации:

```bash
ANAMNESTIC_SEMANTIC=1 anamnestic verify
```

---

## 13. Где что лежит

```
~/.claude-mem/                     # данные (путь историческиий; переопределяется
                                   # через ANAMNESTIC_DATA_DIR)
├─ claude-mem.db                   # SQLite: все таблицы
├─ semantic-chroma/                # Chroma коллекция 'history_turns'
├─ fastembed-models/               # ONNX модель (cached)
├─ semantic-env/                   # Python venv
├─ health.json                     # snapshot последнего sync
├─ settings.json                   # конфиг claude-mem (если плагин стоит)
├─ supervisor.json, worker.pid     # worker state (claude-mem)
└─ logs/                           # worker logs (claude-mem)

~/anamnestic-backups/              # tarball'ы (last N)

~/projects/anamnestic/                # код (git)
├─ anamnestic/
│  ├─ config.py                    # пути / модель / коллекция (env-overridable)
│  ├─ db.py                        # connect() + миграции
│  ├─ cli.py                       # sync/status/search/backup/verify/restore/audit/eval
│  ├─ audit.py                     # audited() + write_health()
│  ├─ backup.py, restore.py, verify.py
│  ├─ ingest/
│  │  ├─ incremental.py            # mtime scanner, UPSERT
│  │  └─ recover_main.py           # скрипт из §16.1
│  ├─ indexers/incremental_chroma.py
│  ├─ search/hybrid.py             # BM25 + semantic → RRF
│  ├─ eval/{golden.yaml, run.py}
│  └─ daemon/mcp_server.py         # stdio MCP
├─ migrations/
└─ systemd/
```

---

## 14. Добавить новый источник jsonl

Сценарий: кроме Claude Code и Codex появился третий клиент (Aider, Cursor agent, свой собственный). Интеграция:

1. Написать парсер в `anamnestic/ingest/parsers_<name>.py`, возвращающий dict:

   ```python
   {"csid": "...", "cwd": "...", "title": ..., "first_ts": "...", "last_ts": "...",
    "turns": [(role, text, ts), ...], "files": [...], "platform": "<name>"}
   ```

2. Зарегистрировать источник в `anamnestic/ingest/incremental.py::_discover()`:

   ```python
   for p in glob(os.path.join(MY_ROOT, "pattern.jsonl")):
       yield "<name>", p, os.stat(p).st_mtime_ns
   ```

3. Добавить ветку в `process()` → вызывающую твой парсер.
4. `anamnestic sync` начнёт подхватывать новые файлы. `platform_source='<name>'` появится в `anamnestic status` / `mem_stats()`.

Голден-набор можно расширить запросами, специфичными для этого клиента.

---

## 15. Переезд на другую машину

На старой (или из последнего бэкапа) нужно:

- `~/anamnestic-backups/<latest>.tar.gz` — данные,
- `~/projects/anamnestic/` — репо,
- (опционально) `~/.codex/sessions/`, `~/.claude/projects/` — если хочешь иметь raw jsonl-ы.

На новой — §§1–5, затем:

```bash
cd ~/projects/anamnestic
PYTHONPATH=$PWD ~/.claude-mem/semantic-env/bin/python -m anamnestic.cli restore \
    ~/anamnestic-backups/<latest>.tar.gz

PYTHONPATH=$PWD ~/.claude-mem/semantic-env/bin/python -m anamnestic.db     # миграции (no-op)
PYTHONPATH=$PWD ~/.claude-mem/semantic-env/bin/python -m anamnestic.cli verify
```

Далее §§10–11: регистрация MCP в клиентах + systemd-таймеры.

**Как устроен restore.** Tarball содержит `claude-mem.db` и `semantic-chroma/` на верхнем уровне. Команда распаковывает во временную директорию, атомарно подменяет текущие файлы, а старые сохраняет рядом как `claude-mem.db.pre-restore-<stamp>` и `semantic-chroma.pre-restore-<stamp>/`. Если что-то пошло не так — эти файлы остаются, можно откатиться.

---

## 16. Известные грабли

### 16.1. Пропущенные main-сессии

Если базовая SQLite уже существовала (например, из старого `claude-mem`) и в `sdk_sessions` есть строки без соответствующих записей в `historical_turns` — идемпотентность `sync` по content_session_id пропустит их.

Диагностика:

```bash
sqlite3 ~/.claude-mem/claude-mem.db "
SELECT platform_source, COUNT(*) FROM historical_turns GROUP BY platform_source;"
```

Если для какого-то источника 0 или сильно меньше ожидаемого:

```bash
cd ~/projects/anamnestic
PYTHONPATH=$PWD ~/.claude-mem/semantic-env/bin/python -m anamnestic.ingest.recover_main
```

Перечитает jsonl и дозальёт. Далее — `anamnestic sync`, чтобы Chroma догнала.

### 16.2. Смена формата у клиента

Если один из клиентов меняет формат jsonl — парсер в `anamnestic/ingest/` требует обновления. Симптом: после апгрейда клиента `new_files` в sync'е не растёт или стабильно `errors > 0`.

### 16.3. `paraphrase-multilingual-MiniLM-L12-v2` — mean pooling warning

`fastembed ≥ 0.6` переключился с CLS на mean pooling. Качество близко. Для точного воспроизведения старого поведения — `fastembed==0.5.1`.

### 16.4. Своя Chroma у `claude-mem` на `:8000`

`claude-mem` объявляет собственный Chroma, но поднимает его лениво. Наш индекс живёт отдельно в `~/.claude-mem/semantic-chroma/` через `chromadb.PersistentClient`. Их Chroma не используется и не мешает.

### 16.5. Несколько клиентов одновременно

Все клиенты работают с одной БД и одной Chroma. SQLite в WAL-режиме выдерживает конкурентные чтения. Параллельные writes в `sync` защищены systemd-юнитом `Type=oneshot`. MCP-запросы — read-only.

### 16.6. Первый поиск через MCP медленнее

При старте процесса модель (~220 МБ) загружается в RAM. Последующие запросы — быстрые. Кэш модели — в `~/.claude-mem/fastembed-models/`.

### 16.7. SQLite повреждён (`verify` показывает `sqlite_integrity != ok`)

Если бэкап свежий — `anamnestic restore`. Если нет:

```bash
sqlite3 ~/.claude-mem/claude-mem.db ".recover" > /tmp/recovered.sql
sqlite3 /tmp/recovered.db < /tmp/recovered.sql
# перенести /tmp/recovered.db в ~/.claude-mem/claude-mem.db вручную после проверки
```

Потерянные сессии (между последним бэкапом и сбоем) вернутся автоматически при следующем `sync` — jsonl-файлы живут независимо от БД.

### 16.8. FTS5 деградировал

FTS перестраивается без потери данных:

```bash
sqlite3 ~/.claude-mem/claude-mem.db \
    "INSERT INTO historical_turns_fts(historical_turns_fts) VALUES('rebuild');"
anamnestic verify
```

### 16.9. Chroma «сломалась»

Chroma — кэш эмбеддингов, сносится и пересчитывается без потери данных:

```bash
rm -rf ~/.claude-mem/semantic-chroma
sqlite3 ~/.claude-mem/claude-mem.db "DELETE FROM anamnestic_embed_state;"
anamnestic sync
```

---

## 17. Кастомизация через env vars

| Variable | Default | Что делает |
| --- | --- | --- |
| `ANAMNESTIC_DATA_DIR` | `CLAUDE_MEM_DATA_DIR` или `~/.claude-mem` | корень данных (БД + Chroma + venv + модель); имеет приоритет над `CLAUDE_MEM_DATA_DIR` |
| `CLAUDE_MEM_DATA_DIR` | `~/.claude-mem` | профиль данных `claude-mem`, который `anamnestic` подхватывает по умолчанию |
| `ANAMNESTIC_PYTHON` | auto-detect | интерпретатор для `scripts/anamnestic.sh` и `scripts/mcp_server.sh` |
| `ANAMNESTIC_CC_ROOT` | `~/.claude/projects` | источник Claude Code jsonl |
| `ANAMNESTIC_CODEX_ROOT` | `~/.codex/sessions` | источник Codex jsonl |
| `ANAMNESTIC_BACKUP_ROOT` | `~/anamnestic-backups` | куда бэкапить |
| `ANAMNESTIC_BACKUP_KEEP_LAST` | `10` | ротация |
| `ANAMNESTIC_SEMANTIC` | `auto` | `auto` использует Chroma/fastembed когда доступны; `0` отключает; `1` включает строгие semantic-проверки |
| `ANAMNESTIC_MCP_AUTO_SYNC` | `1` | фоновый lightweight ingest при запуске MCP; `0` отключает |
| `ANAMNESTIC_EMBED_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | ONNX fastembed |
| `ANAMNESTIC_CHROMA_COLLECTION` | `history_turns` | имя коллекции |

---

## 18. Проверочный чек-лист после установки

```bash
anamnestic status                                  # корпус заполнен
anamnestic verify                                  # healthy=true
claude mcp list 2>/dev/null | grep anamnestic     # Claude Code видит (если поставлен)
codex  mcp list 2>/dev/null | grep anamnestic     # Codex видит (если поставлен)
systemctl --user list-timers | grep anamnestic    # таймеры активны
anamnestic search "любой_твой_запрос" --top-k 3    # поиск возвращает результаты
ls -lh ~/anamnestic-backups/                   # после первого дня — tarball
```

---

## 19. Удаление

```bash
systemctl --user disable --now anamnestic-sync.timer anamnestic-backup.timer
rm ~/.config/systemd/user/anamnestic-*.{service,timer}
systemctl --user daemon-reload

claude mcp remove anamnestic 2>/dev/null
codex mcp remove anamnestic 2>/dev/null
```

`claude-mem` плагин (если ставил):

```bash
claude plugin remove claude-mem@thedotmack
# или снять флаги в ~/.claude/settings.json
```

Данные остаются в `~/.claude-mem/` и `~/anamnestic-backups/` — удаляй вручную.

---

## 20. Что НЕ входит (осознанно отложено)

- **Privacy layer** — маскировка токенов/секретов при индексации. Риск: секреты попадают в tarball-бэкапы и в Chroma. Включать когда корпус содержит чувствительные данные или бэкап уходит за пределы машины.
- **Event extraction** (decisions / todos / facts через локальный LLM) — превращает архив в базу знаний, не в базу реплик. Отдельный кусок работы с Ollama + структурированными промптами.
- **Апгрейд на `multilingual-e5-large`** (1024-dim) — жирнее, качество выше. Только если MiniLM систематически промахивается на твоём домене (golden eval покажет).
- **Off-site backup** — сейчас только локальный диск. Для серьёзной надёжности — `rclone` / `git-crypt` / `zfs send` на внешнее хранилище.
Каждый пункт — отдельная итерация с измеримым критерием.
