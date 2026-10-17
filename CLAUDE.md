# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Telegram bot (aiogram) + FastAPI backend that plans a shared rehearsal room for several club groups. `README.md` has the
user-facing description (roles, weekly flow, Sheets setup, env vars); this file covers what you need to change the code.

## Commands

```bash
pip install -r requirements-dev.txt     # runtime + pytest, pytest-asyncio, aiosqlite
pytest                                  # whole suite (asyncio_mode=auto, tests/ only)
pytest tests/test_allocator.py -k name  # single file / single test
python -m compileall -q app             # quick syntax check after bulk edits

cp .env.example .env && docker compose up --build   # db(postgres) + redis + backend + ngrok + bot
```

Backend: `uvicorn app.main:app` (port 8001 on the host in compose, docs at `/docs`). Bot: `python -m app.bot.main`.
There is no linter configured.

## Architecture

Two processes share one codebase and talk over HTTP + Redis:

- **Backend** (`app/main.py`, `app/api/routes/`, `app/services/`): all business rules live in `app/services/`; routes are
  thin. Every route except `/health` and `/webapp/*` requires `X-Internal-Token`. The Telegram Mini App is vanilla-JS
  static files in `app/webapp/static/` (served at `/webapp/static`, entry `/webapp/schedule`) talking to
  `app/api/routes/webapp.py` (`/webapp/api/*`: `schedule`, `me`, `locales/{lang}`, bookings and `bands` for the signed-in member). Those routes authenticate with the
  signed `X-Telegram-Init-Data` header via `api/webapp_auth.py` (`core/telegram_auth.py` validates it; use
  `fresh_webapp_user`, 1 h max age, for anything that mutates) and take their strings from the `webapp.*` keys of
  `app/locales/*.json`. The bot sets the chat menu button to the Mini App on startup (`bot/menu_button.py`). A middleware hides everything but
  `/webapp/*` and `/health` from requests carrying `X-Forwarded-Host` (i.e. coming through the ngrok tunnel).
- **Bot** (`app/bot/`): never touches the DB. It calls the backend through `BackendClient` (`api_client.py`), which turns
  HTTP errors into `RuntimeError(detail)`. Feature routers (`handlers_groups/history/dsvr/planning`) are included
  **before** the main `handlers.router` because the latter has catch-all fallback handlers. FSM state is in Redis.
  User-facing strings go through `i18n.py` / `app/locales/{ru,en,kz}.json`. Booking rehearsals and managing bands happen
  only in the Mini App: the bot's Репетиции/Коллективы menus and the `start=join_<code>` deep link just offer `web_app`
  buttons (`keyboards.mini_app_*`; a web_app button cannot carry `start_param`, so the target goes in `?screen=` / `?join=`,
  which `app.js` reads). Callbacks of the removed flows are caught by `legacy_flow_button` in `handlers.py`.
- **Backend → bot notifications:** services call `outbox.queue_notification` / `queue_schedule_changed` on the session
  and then `outbox.commit_and_publish(session)`; events are pushed to the Redis list `bot:outbox` only after the commit,
  and `outbox_worker` in `app/bot/main.py` delivers them (and refreshes schedule menus once per burst). In tests
  `outbox.publish` is monkeypatched to collect events (see `tests/conftest.py`).

### Booking model and weekly planning

The default model is a sliding 7-day window (`WEEKLY_PLANNING_ENABLED=false`, the default); the bot hides the planning
buttons and `plan:*` entry points then (`settings.weekly_planning_enabled` is read by the bot process too). The optional
`club_settings` key `max_future_bookings_per_band` (0 = off) caps a band's not-yet-started bookings in `create_booking`.
Users with `notifications_enabled=False` are skipped when queuing notifications (history is still written).

The opt-in weekly mode: `services/planning.py` stores weekly requests (up to 5 priority windows) and runs the allocation; `services/allocator.py`
is the **pure** algorithm (no DB) — keep it that way and unit-test it directly. `services/weeks.py` holds week/deadline
math; days of a week that is still "collecting" are closed for manual booking (`is_date_open_for_booking`).
A cron job (APScheduler, started in `on_startup`) runs the allocation on Sunday at `ALLOCATION_TIME`;
`catch_up_allocations` runs on startup for missed weeks. `WEEKLY_PLANNING_ENABLED=false` disables the scheduler and the
closing of future days.

### Schedule history and Sheets backup

Every booking mutation (create/edit/cancel/DSVR takeover/auto-assign) must go through `services/schedule_history.py` so
it is recorded with before/after values; bookings are soft-cancelled, never deleted. `services/sheets_sync.py` is a
one-way background worker (started in `on_startup`, triggered by `request_sync()`) that rewrites the **Расписание**
sheet and appends to **История**; it is a no-op without `GOOGLE_SHEETS_ID`.

### Permissions

Role checks are centralized in `services/permissions.py` (user, band leader/responsible person, group admin scoped to
their own group, DSVR, admin). Users in `INITIAL_ADMIN_TELEGRAM_IDS` become admins on `/start` and cannot be demoted.

## Gotchas

- **No real migrations.** Schema changes are `Base.metadata.create_all` plus hand-written idempotent
  `ALTER TABLE ... IF NOT EXISTS` statements in `create_db_and_tables()` (`app/db/session.py`). New columns on existing
  tables need an ALTER there; `alembic/versions/` is empty. These statements are Postgres-only.
- **Postgres vs SQLite.** Tests run on in-memory SQLite, so Postgres-specific code (the startup ALTERs and the advisory
  lock `_lock_bookings` in `services/bookings.py` and `services/planning.py`) is not exercised by tests; the `session`
  fixture monkeypatches the locks away. Verify such changes against a real Postgres.
- Tests force `weekly_planning_enabled=False` because planning closes next-week days and would make date-relative tests
  depend on the weekday; planning tests opt in via the `planning_on` fixture.
- Time is club-local (`TIMEZONE`, default `Asia/Almaty`); use `core/timezone.now_local()` rather than naive `datetime.now()`.
- The backend refuses to start with an empty `INTERNAL_API_TOKEN`; with `ENVIRONMENT` other than `local` it must also be
  non-default and at least 32 chars (`core/config.py`). `.env.example` ships `ENVIRONMENT=production` and an empty token.
- On this Windows/Git-Bash setup the Bash tool mangles backslashes in heredocs (e.g. `\\n` becomes a real newline). For
  patch scripts or edits containing backslashes, use the Write/Edit tools instead of heredocs.
