MSU Timetable → Simple ICS Server

Quickstart
- Python: use version from `.python-version`.
- Install (uv): `uv sync`
- Install (pip): `python -m venv .venv && source .venv/bin/activate && pip install -e .`
- Copy `.env.config.sample` to `.env.config` and fill required variables.
- Run (direct): `uv run main.py` or `python main.py` (no CLI flags; all config is in `.env.config`).
- Run (installed): after `pip install -e .`, use `timetable` CLI.

Production Deploy
- The app hosts a tiny HTTP server that serves per‑group ICS files at `/timetable/<group_id>`.
- System prerequisites:
  - Install Google Chrome.
  - Use headless mode on servers: `HEADLESS=true` in `.env.config`.
  - Ensure outbound HTTPS to the MSU site.
- Install with uv (recommended):
  - `uv sync` (creates .venv and installs deps)
  - Optional: `uv run --with pytest -- pytest -q` to run tests.
- Configure `.env.config`:
  - Required: `Факультет`, `Курс`, `Группа` (or English aliases). For multiple groups use `ГРУППЫ`/`GROUPS`.
  - Set `LOG_FILE` for persistent logs and `LOG_LEVEL=INFO` (or `DEBUG` during rollout).
- Run as a service (systemd sketch):
  - WorkingDirectory: repository path (so it finds `.env.config`).
  - ExecStart: `uv run main.py` (inside the project .venv).
  - Restart=always; Environment=`PYTHONUNBUFFERED=1`.
- Expose timetable server:
  - Put behind TLS reverse proxy (Nginx/Caddy). Proxy GET/HEAD to `http://127.0.0.1:8080/timetable/`.
  - Subscribe URLs (any calendar app that accepts ICS):
    - `https://your.domain/timetable/101`
    - `https://your.domain/timetable/102`
    - `https://your.domain/timetable/103`
    - `https://your.domain/timetable/104`
- Operations:
  - Logs: stdout/stderr or `LOG_FILE` (rotation enabled).
  - Health: `…/calendar.ics` returns VCALENDAR; Telegram sends messages on changes if enabled.
  - Persist data dirs: `sync/var/timetable/`, `sync/var/telegram/`, `sync/var/last_schedule.json`.
  - Log file rotation/retention (env vars):
    - `LOG_ROTATION` (default: `10 MB`) — rotate by size or time (e.g., `00:00`).
    - `LOG_RETENTION` (default: `1 day`) — keep rotated logs for this time.
    - `LOG_COMPRESSION` (default: `zip`) — compress rotated files.

Timetable server
- Enabled by default. Configure:
  - `SERVE_TIMETABLE` (true/false, default true)
  - `TIMETABLE_HOST` / `TIMETABLE_PORT` (bind address/port; defaults: `127.0.0.1`, `8080`)
  - `TIMETABLE_BASE_PATH` (default `/timetable`)
  - `TIMETABLE_STORAGE_DIR` (default `sync/var/timetable`)
  - ICS URLs: `{domain}/timetable/<group_id>` (e.g., 101/102/103/104)
  - Landing page: `{domain}/timetable/` lists groups with counts and last updated.

Project Structure

- `main.py`: orchestration entrypoint (no CLI args). Reads `.env.config`, sets up logging, starts background servers (Telegram webhook, timetable server), then runs the parse → group → publish cycle on a schedule.

- `utils/`: shared utilities
  - `config.py`: `.env.config` loader (`AppConfig`, `load_env_config`), logging setup (`setup_logging`), constants, and env parsing helpers.
  - `__init__.py`: small helpers used across modules:
    - `group_id_from_name(name: str) -> str` — derive stable group id (leading digits or alnum slug).
    - `stable_event_key(item) -> str` — deterministic key for change detection and ICS UIDs.
    - `logged_sleep(total_seconds, message=...)` — progress‑bar sleep for visible waits.
    - `_log_samples(items, max_items=5)` — debug helper to log sample parsed items.

- `parse/`: scraping helpers
  - `browser.py`: Selenium WebDriver factory (headless and extra Chrome flags supported).
  - `fill_columns.py`: selects faculty/course/group on the page and waits for the table.
  - `parse_sheet.py`: parses the timetable grid into items; `group_language_lessons(...)` aggregates parallel language lessons into a single item; `build_events_for_group(driver, cfg)` applies filters and returns items for one group.

- `sync/`: integrations
  - `serve/`: lightweight HTTP server for ICS + landing
    - `timetable_server.py`: serves `GET /timetable/<group_id>` (and `.ics`), and landing `GET /timetable/` with last updated times. Writes `meta.json` per group and `groups.json` for landing.
  - `telegram_bot/`: Telegram integration package (files grouped by concern)
    - `core.py`: Telegram API client, notifier with file persistence, snapshot helpers, and broadcast orchestration.
    - `formatting.py`: HTML message formatter (day/week), language bullets, and diff helpers (normalize/compute/format) plus simple period helpers.
    - `runtime.py`: bot runtimes — long‑polling loop and webhook HTTP server.
    - `store.py`: optional PostgreSQL (SQLAlchemy) store for per‑chat group and subscribers; falls back to file storage.

- `tests/`: minimal tests (e.g., group id derivation, label normalization).

- Top‑level tooling
  - `pyproject.toml`: deps, dev tools, `timetable` script → `main:run`.
  - `.env.config.sample`: config template to copy and fill as `.env.config`.
  - `uv.lock`: resolved dependency lock for uv.

Environment (.env.config)
- Required:
  - `Факультет` or `FACULTY`: Бакалавриат/Магистратура/Аспирантура
  - `Курс` or `COURSE`: course number or text as shown on site
  - `Группа` or `GROUP`: default group name as shown on site
  - `ГРУППЫ` or `GROUPS`: comma‑separated list of groups to parse (e.g., `101б__Философия, 102б__Философия, 103б__Философия, 104б__Философия`). If not set, falls back to single `GROUP`.
- Optional:
  - `BASE_URL`: timetable URL (default: https://cacs.philos.msu.ru/time-table/group?type=0)
  - `TIMEZONE` or `TZ`: IANA timezone for events (default: Europe/Moscow)
  - `HEADLESS`: true/false
  - `DRY_RUN`: true/false
  - Timetable server:
    - `SERVE_TIMETABLE`, `TIMETABLE_HOST`, `TIMETABLE_PORT`, `TIMETABLE_BASE_PATH`, `TIMETABLE_STORAGE_DIR`
  - `SELENIUM_TIMEOUT`: waits in seconds (default: 20)
  - `CHROME_ARGS`: extra Chrome flags, e.g. `--disable-gpu --lang=ru-RU`
  - `LOG_LEVEL`: logging level (DEBUG/INFO/WARNING/ERROR)
  - `LOG_FILE`: optional log path (rotates 10MB, keeps 7 days, zip)
  - `LOG_COLOR`: true/false; force colorization
  - `GROUP_LANGUAGES`: true/false; group multiple language classes in one event (default: true)
  - `WATCH_INTERVAL_MINUTES` or `WATCH_INTERVAL_SECONDS`: how often to re-check and sync (default: 5 minutes)
  - Telegram (optional):
    - `TELEGRAM_ENABLED`: true/false; enable notifications on changes
    - `TELEGRAM_BOT_TOKEN`: bot token from @BotFather
    - `TELEGRAM_ADMIN_USER_ID`: Telegram user id to receive error reports
    - `TELEGRAM_PERSIST_DIR`: directory to store subscribers (default: sync/var/telegram)
    - `SCHEDULE_SNAPSHOT_PATH`: snapshot JSON path for bot queries (default: sync/var/last_schedule.json)
  - Timetable server: see above (`SERVE_TIMETABLE`, `TIMETABLE_HOST`, `TIMETABLE_PORT`, `TIMETABLE_BASE_PATH`, `TIMETABLE_STORAGE_DIR`).

PostgreSQL (optional) for Telegram user data (SQLAlchemy)
- Purpose: persist per-chat selected group in the database instead of a local file.
- Install PostgreSQL:
  - macOS (Homebrew): `brew install postgresql` then `brew services start postgresql`
  - Ubuntu/Debian: `sudo apt update && sudo apt install -y postgresql postgresql-contrib` and `sudo systemctl enable --now postgresql`
- Create DB and user (example):
  - `sudo -u postgres psql -c "CREATE USER timetable WITH PASSWORD 'strong-pass';"`
  - `sudo -u postgres psql -c "CREATE DATABASE timetable OWNER timetable;"`
- Configure the app:
  - Set `DATABASE_URL=postgresql+pg8000://timetable:strong-pass@localhost:5432/timetable` in `.env.config`.
  - On startup, the bot will create tables via SQLAlchemy:
    - `tg_users(chat_id BIGINT PRIMARY KEY, group_id TEXT, updated_at timestamptz)` — per‑chat selected group
    - `tg_subscribers(chat_id BIGINT PRIMARY KEY, created_at timestamptz)` — list of subscribed chats
  - If `DATABASE_URL` is not set or database is unavailable, the bot falls back to file persistence in `sync/var/telegram/subscribers.json`.

CLI overrides
- None. All settings are read from `.env.config` / environment variables.

Window size
- Default window size is `1400,1000`. To change it, pass via `CHROME_ARGS`, e.g. `--window-size=1280,800`.

- Use `HEADLESS=true` in CI/remote environments.
- Logging uses loguru with colored console output and optional file sink (rotation, retention, compression).
- Language grouping merges parallel foreign language classes in the same timeslot into one summary like: `Английский г264it, г267it; Французский г319, г318; Немецкий г236it*, г234`.
- Event description includes parsed details from the popover: pair number, teacher, room(s), group/subgroup line(s), and the "Добавлено" date, followed by raw HTML snapshot.

Item shape (parsed schedule)

Each parsed item is a mapping with keys (some optional):

- `date` (YYYY‑MM‑DD)
- `start`, `end` (HH:MM)
- `title` (may include `[Лк]`, `[Сем]` etc.)
- `room` (optional)
- `teacher` (optional)
- `group_info` (optional; subgroup line)
- `pair` / `pair_label` (optional)
- `added_at` (optional; DD.MM.YYYY if available)
- `raw` (debug info)

First run & Troubleshooting
- Ensure your reverse proxy exposes `/timetable/` to the public if you want subscriptions.

Telegram bot
- Enable by setting `TELEGRAM_ENABLED=true` and `TELEGRAM_BOT_TOKEN=...` in `.env.config`.
- On each successful cycle of `main.py`, the app saves a snapshot to `SCHEDULE_SNAPSHOT_PATH` and, if it detects changes, broadcasts a summary to all subscribed chats.
- Webhook mode: the app runs a lightweight HTTP server and configures Telegram via `setWebhook`.
  - Required env: `TELEGRAM_WEBHOOK_URL` (public HTTPS URL; e.g., `https://your.domain/telegram/<secret>`)
  - Optional: `TELEGRAM_WEBHOOK_HOST` (bind, default `0.0.0.0`), `TELEGRAM_WEBHOOK_PORT` (default `8081`), `TELEGRAM_WEBHOOK_SECRET_TOKEN` (adds header check)
- Subscribe by chatting with your bot and sending `/subscribe`. Unsubscribe with `/unsubscribe`.
- Commands supported:
  - `/today` — today’s schedule
  - `/week` — current week
  - `/nextweek` — next week
  - `/help` — help
- Errors are sent to `TELEGRAM_ADMIN_USER_ID` if configured.

Telegram notifications
- By default, change detection for notifications considers only future lessons (to avoid noise from historical edits). Set `TELEGRAM_DIFF_FUTURE_ONLY=false` in `.env.config` to notify on any changes regardless of date. When only past lessons changed, the logs will state: "Изменений в будущих занятиях не обнаружено …" and no notification is sent.

Local Development
- Python:
  - Match `.python-version` (e.g., `pyenv install 3.13 && pyenv local 3.13`).
  - Install deps with uv: `uv sync`.
  - Dev tools (pytest/black/isort/ruff):
    - Option A (install dev extras into venv): `uv sync --extra dev`
    - Option B (ephemeral for a single run): prefix commands with `uv run --with <tools> -- …`
- Config for dev:
  - `DRY_RUN=true` to avoid filesystem writes.
  - `HEADLESS=false` to see the browser while debugging.
  - `LOG_LEVEL=DEBUG`, `WATCH_INTERVAL_SECONDS=30` for faster cycles.
- Run: `uv run main.py` (stop with Ctrl+C).
- Tests: `uv run --with pytest -- pytest -q` (or `uv run -m pytest -q` after `uv sync --extra dev`).
- Code style:
  - `uv run --with ruff,black -- ruff check --fix . && uv run --with black -- black .`
  - or after `uv sync --extra dev`: `uv run -m black . && uv run -m isort .`

License

No license file is provided in this repository.
