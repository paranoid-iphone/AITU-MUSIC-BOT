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
alembic upgrade head                    # manual migration run (the backend does it on start); alembic check = drift

cp .env.example .env && docker compose up -d --build   # prod: db(postgres) + redis + backend + ngrok + bot + backup
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build   # dev overlay: .:/app mounts + uvicorn --reload
```

`docker-compose.yml` is the production config (code in the image, only `./secrets` mounted into the backend,
`restart: unless-stopped`, healthchecks on db/redis/backend with `service_healthy` ordering, Redis AOF on `redis_data`,
rotated json logs). Anything that needs the working tree mounted belongs in `docker-compose.dev.yml`.

The `backup` service runs `ops/pg_backup.sh` (pg_dump into `./backups` with rotation; README has the restore order).
`.gitattributes` keeps `*.sh` on LF, which the Alpine `sh` in the container needs.

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
  HTTP errors into `RuntimeError(detail)`. Feature routers (`handlers_planning/repeat/move/waitlist`) are included
  **before** the main `handlers.router` because the latter has catch-all fallback handlers. FSM state is in Redis.
  User-facing strings go through `i18n.py` / `app/locales/{ru,en,kz}.json`. Booking rehearsals and managing bands happen
  only in the Mini App: the bot's Репетиции/Коллективы menus and the `start=join_<code>` deep link just offer `web_app`
  buttons (`keyboards.mini_app_*`; a web_app button cannot carry `start_param`, so the target goes in `?screen=` / `?join=`,
  which `app.js` reads). Callbacks of the removed flows are caught by `legacy_flow_button` in `handlers.py`.
  Registration requests, user moderation and admin/DSVR/group-admin roles also live only in the Mini App ("Админ" tab,
  `app/api/routes/webapp_admin.py` → `/webapp/api/admin/*`, `?screen=admin&tab=requests|users|roles`); the bot's admin
  menu has `web_app` buttons for them. The user-facing notifications for approve / reject / role changes (admin, DSVR,
  group admin) are queued by the services (`services/users.py`, `services/groups.py`), so every caller must publish with
  `outbox.commit_and_publish`. Taking the room (DSVR priority, staff booking), editing or cancelling any booking, the days /
  room hours / limits / personal-rehearsal settings and the schedule history are Mini App only too (grid modes and the
  "Настройки" / "История" admin tabs); the bot keeps just the concerts, the admin band list, the action journal and
  weekly-planning admin screens. Stale buttons and reply-keyboard texts of the removed admin flows are listed in
  `LEGACY_MINI_APP_SCREENS["admin"]` / `REMOVED_ADMIN_TEXTS`: add new removals there, and keep prefixes narrow (`admin:` as a
  whole would also swallow `admin:back`).
- **Backend → bot notifications:** services call `outbox.queue_notification` / `queue_schedule_changed` on the session
  and then `outbox.commit_and_publish(session)`. `queue_*` add an `OutboxEvent` row (`outbox_events`) to the session, so the event commits or
  rolls back with the change; after the commit it is pushed to the Redis list `bot:outbox` and stamped `published_at`. The
  APScheduler job `run_outbox_retry` (every 30 s) re-pushes committed events that missed Redis and purges published rows after 7 days.
  The bot takes events with `BLMOVE` into `bot:outbox:processing` and `LREM`s them after delivery (`bot/outbox_delivery.py`; unfinished ones are
  moved back on start), so delivery is at least once; `TelegramRetryAfter` is waited out and the same event retried. `outbox_worker` in
  `app/bot/main.py` also refreshes schedule menus once per burst, in a background task via `bot/menu_refresh.py`, throttled and
  honouring `RetryAfter`). Whatever changes what the schedule image shows must queue `schedule_changed`; handlers never refresh menus themselves. In tests
  `outbox.publish` is monkeypatched to collect events (see `tests/conftest.py`).

### Booking model and weekly planning

The default model is a sliding 7-day window (`WEEKLY_PLANNING_ENABLED=false`, the default); the bot hides the planning
buttons and `plan:*` entry points then (`settings.weekly_planning_enabled` is read by the bot process too). The optional
`club_settings` key `max_future_bookings_per_band` (0 = off) caps a band's not-yet-started bookings in `create_booking`.
Users with `notifications_enabled=False` are skipped when queuing notifications (history is still written).
"Repeat next week": the slot a week after a booking only enters the sliding window once that booking has started, so an
APScheduler interval job (`run_repeat_offers`, every 5 min, always on) calls `services/repeat_offers.offer_repeats`: for bookings
that started in the last 2 h with a free slot +7 days it sets `bookings.repeat_offered_at` and queues a `repeat_offer` outbox event
(text + button). The bot sends it with a `repeat:<booking_id>` button; `handlers_repeat.py` calls `POST /bookings/{id}/repeat`
(`repeat_offers.repeat_booking` → `create_booking`, the date is derived server-side).

"Ask to move": a member who needs a slot held by someone else's booking proposes a free slot of the same length
(`services/move_requests.py`, table `move_requests`). The band's responsible person gets a
`move_request` outbox event (buttons `move:accept|decline:<id>`, handled by `handlers_move.py` → internal
`/move-requests/{id}/accept|decline`); the Mini App has the card button and a "requests" section on "Мои брони"
(`/webapp/api/move-requests*`). Whoever may manage the booking, or is the band's responsible person, can answer. Accepting
moves the booking through `update_booking(authorized=True, commit=False)` so history/Sheets/notifications behave as for
any edit, and the request status commits in the same transaction; nothing is booked for the requester. Requests expire
after 24 h or at the booking's start (APScheduler job `expire_move_requests`, plus a lazy check on every answer).

Waiting list: a member can queue for the exact range of an occupied, non-staff booking, for one of their bands or as a
personal rehearsal (`services/waitlist.py`, table `waitlist_entries`; max 5 per person).
`bookings.delete_booking`, `update_booking` (old slot) and `create_priority_booking` (leftovers of a preempted booking)
call `_offer_freed` → `waitlist.notify_freed` before committing: every waiter whose range is now entirely free gets a
`waitlist_offer` outbox event (button `waitlist:take:<id>`, `handlers_waitlist.py` → internal `/waitlist/{id}/take`; the
Mini App has "Встать в очередь" on a booking card and a section on "Мои брони" → `/webapp/api/waitlist*`). Nothing is
reserved: the first `take` wins, which deletes the entry and runs `create_booking` in one transaction (a failed take rolls
back and the entry stays). Entries are dropped when their start passes (`cleanup_waitlist` job, lazily on listing), when
the band is deleted or the member removed from it.

The opt-in weekly mode: `services/planning.py` stores weekly requests (up to 5 priority windows) and runs the allocation; `services/allocator.py`
is the **pure** algorithm (no DB) — keep it that way and unit-test it directly. `services/weeks.py` holds week/deadline
math; days of a week that is still "collecting" are closed for manual booking (`is_date_open_for_booking`).
A cron job (APScheduler, started in `on_startup`) runs the allocation on Sunday at `ALLOCATION_TIME`;
`catch_up_allocations` runs on startup for missed weeks. `WEEKLY_PLANNING_ENABLED=false` skips that job and the
closing of future days (the scheduler itself still runs for the repeat-offer job).

### Schedule history and Sheets backup

Every booking mutation (create/edit/cancel/DSVR takeover/auto-assign) must go through `services/schedule_history.py` so
it is recorded with before/after values; bookings are soft-cancelled, never deleted. `services/sheets_sync.py` is a
one-way background worker (started in `on_startup`, triggered by `request_sync()`) that rewrites the **Расписание**
sheet and appends to **История**; it is a no-op without `GOOGLE_SHEETS_ID`. `python -m app.tools.restore_from_sheets`
(`app/tools/`, dry-run unless `--apply`, `--csv` for an export) adds the future bookings the DB lacks from the sheet's ID
columns, never changes existing ones, and records them as the history action `restored`.

### Permissions

Role checks are centralized in `services/permissions.py` (user, band leader/responsible person, group admin scoped to
their own group, DSVR, admin). Users in `INITIAL_ADMIN_TELEGRAM_IDS` become admins on `/start` and cannot be demoted.

## Gotchas

- **Schema = Alembic.** `prepare_database()` (`app/db/session.py`) runs `alembic upgrade head` (`app/db/migrate.py`) on
  every backend start, then `seed_reference_data()` (default `club_settings`, groups, initial admins: rows, not schema).
  To change the schema edit `app/db/models.py`, then `alembic revision --autogenerate -m "..."` against a database at
  `head`, and review the file. `tests/test_migrations.py` fails when the models and the revisions drift apart.
  `0001_baseline` is frozen (no model imports) and also adopts databases made by the old `create_all` + `ALTER` startup
  code; its Postgres-only legacy branch is not covered by tests.
- **Postgres vs SQLite.** Tests run on in-memory SQLite, so Postgres-specific code (the legacy branch of `0001_baseline` and the advisory
  lock `_lock_bookings` in `services/bookings.py` and `services/planning.py`) is not exercised by tests; the `session`
  fixture monkeypatches the locks away. Verify such changes against a real Postgres.
- Tests force `weekly_planning_enabled=False` because planning closes next-week days and would make date-relative tests
  depend on the weekday; planning tests opt in via the `planning_on` fixture.
- Time is club-local (`TIMEZONE`, default `Asia/Almaty`); use `core/timezone.now_local()` rather than naive `datetime.now()`.
- The backend refuses to start with an empty `INTERNAL_API_TOKEN`; with `ENVIRONMENT` other than `local` it must also be
  non-default and at least 32 chars (`core/config.py`). `.env.example` ships `ENVIRONMENT=production` and an empty token.
- On this Windows/Git-Bash setup the Bash tool mangles backslashes in heredocs (e.g. `\\n` becomes a real newline). For
  patch scripts or edits containing backslashes, use the Write/Edit tools instead of heredocs.
