# AITU Music Club Bot

Telegram bot + FastAPI backend that plans a **shared rehearsal room** for several club groups (music, dance,
cheerleading). Members book rehearsals in a Telegram Mini App on a sliding 7-day window; on top of that there are DSVR
priority, a waiting list, "ask to move" requests, schedule history and a Google Sheets backup. An optional weekly planning
mode (requests with priorities and an automatic Sunday allocation) can replace the sliding window.

## How booking works

By default (`WEEKLY_PLANNING_ENABLED=false`) the room uses a **sliding 7-day window**: at any moment a band can book any
free 30-minute-step slot that starts within the next 7 days, first come first served. Everyone sees who booked each slot
and who is responsible for that band, so people can agree among themselves. Admins can cap how much one band takes:
the Mini App's *Админ → Настройки* tab has the weekly and daily limits and **«Макс. броней на номер»** (how many
not-yet-started bookings a band may hold at once; 0 = no cap). Users who switched notifications off in their profile get no bot messages
about schedule changes; the change itself is still recorded in the history.

## Optional: weekly planning

Set `WEEKLY_PLANNING_ENABLED=true` to replace the sliding window with the Sunday allocation (the planning buttons appear
in the bot only in this mode):

1. **Mon–Sun:** a band's leader (or its responsible person) submits a request for *next* week in
   *Репетиции → Заявка на неделю*: rehearsal length plus up to five time windows ordered by priority
   ("first Tue 18:00-21:00, otherwise Wed 14:00-18:00, otherwise Sat").
2. **Sunday at `ALLOCATION_TIME`:** the system splits the room between all requests and publishes next week's
   schedule. Priorities are served in rounds (everybody's first wish before anybody's second); tighter windows go
   first, bands that received less time this week are preferred, and each request gets the earliest free start in its
   window. Example: dance asks Wed 13:00-15:00 (2h) and music asks Wed 14:00-18:00 (3h) → dance 13:00-15:00,
   music 15:00-18:00.
3. **After publication** every band's responsible person is notified of the result (or that no place was found), and
   the remaining free slots can be booked manually as before.
4. Days of an upcoming week stay closed for manual booking until that week is published.

If the server was down on Sunday evening, the missed allocation runs automatically on the next start.

## Roles

| Role | Can do |
|------|--------|
| user | book for own bands or personally, edit own bookings, see own band history |
| band leader / **responsible person** | submit weekly requests (weekly planning mode); the responsible person is notified about every change of the band's schedule and is shown in the schedule table |
| group admin (music / dance / cheer) | approve registrations and manage bands of **their own group** |
| **DSVR** | take the room at any time up to 30 days ahead: overlapping bookings are cancelled and their people notified; can edit any booking |
| admin | everything, including groups, group admins, DSVR role, limits, working hours, allocation preview/run (weekly planning mode) |

New users pick their group (music / dance / cheer) during registration; the request goes to that group's admins and
to the main admins. Registration requests, user moderation (approve / reject / ban / delete), the admin, DSVR and
group-admin roles, settings and the schedule history are managed in the Mini App's **Админ** tab
(*Заявки / Пользователи / Роли / Настройки / История*); the bot's admin menu only opens it. A user whose DSVR role is
granted or removed gets a bot message. Group admins see only their own group and have no *Роли* tab.

**How admins hear about a new registration request** is a setting of the main admin (*Админ → Настройки → Уведомления о
заявках*): buttons «Одобрить / Отклонить» in the bot chat (default), a single button that opens *Админ → Заявки*, or no
message at all (the *Заявки* tab and its counter are always there).

**Banning or deleting a member** frees what only they held: their future *personal* rehearsals are cancelled (the time is
offered to the waiting list), they leave every waiting list and their open move requests are closed. Band bookings and
band membership stay as they are; an admin changes those on purpose.

## Schedule history and notifications

Every create / edit / cancel / DSVR takeover / automatic assignment is stored with who did it and before/after values
(the Mini App's *Админ → История* tab). The tab has a switch **Брони | Действия**: *Брони* is the schedule history, with
free-text search (band or the person who made the change), filters by action and by rehearsal date, and a group filter;
*Действия* (main admins only) is the action journal (settings, roles, moderation, concerts…) with a text search and a
category filter. The bot's *Админ → История* menu has a button that opens it and still keeps a short text list (*Последние действия*) as a fallback.
Deleting a booking cancels it, so it stays in the history. The backend stores notifications in the database (`outbox_events`) and pushes them to a Redis list (`bot:outbox`); events that missed Redis
are re-sent by a background job, the bot delivers them (at least once) and refreshes the schedule menus.

## Google Sheets backup

One-way (bot → sheet) so the schedule survives a server outage:

- sheet **Расписание** – bookings from the start of this week up to 30 days ahead, rewritten after each change; it carries
  the booking, band and author ids so the bookings can be restored;
- sheet **История** – every schedule change appended once;
- sheet **Концерты** – the concerts of the last 60 days and later with the members of every band application (one row per
  member, a concert without applications gets one row), rewritten after every concert or application change. It is the same
  list as the Excel export, always current: *File → Download → .xlsx* gives the file.

Setup:

1. Google Cloud: create a project, enable the *Google Sheets API*, create a **service account** and download its JSON
   key to `secrets/google-service-account.json` (the folder is git-ignored).
2. Create a Google Sheet and share it with the service account e-mail as **Editor**.
3. Put the sheet id (the part of its URL between `/d/` and `/edit`) into `.env`:

```env
GOOGLE_SHEETS_ID=1AbC...
GOOGLE_SERVICE_ACCOUNT_FILE=secrets/google-service-account.json
```

The sheet contains names and Telegram usernames, so share it carefully.

### Restoring from the sheet

If the database lost bookings, bring the missing future ones back from the sheet. First restore the users and bands
(a `pg_dump`): the sheet holds only their ids, and a row whose band or author is not in the database is reported and
skipped.

```bash
docker compose exec backend python -m app.tools.restore_from_sheets            # dry-run: only a report
docker compose exec backend python -m app.tools.restore_from_sheets --apply    # write the bookings
docker compose exec backend python -m app.tools.restore_from_sheets --csv schedule.csv   # a CSV export instead of Google
```

The tool only adds bookings (with their old ids, recorded in the history as "restored", nobody is notified). A booking
whose time or status differs from its row, and one that is in the database but not in the sheet, are listed and left
for an admin to fix in the Mini App. Running it twice changes nothing.

**Download the sheet as CSV before starting the backend on an empty database:** the sync rewrites the sheet with
what the database holds right after start, which would erase the backup.

## Database backups

The compose service `backup` runs `pg_dump` (custom format) right after start and then every `BACKUP_INTERVAL_HOURS`
(default 24) into `./backups/aitu_music-<UTC time>.dump`. Dumps older than `BACKUP_KEEP_DAYS` (default 14) are deleted,
but only after a new dump succeeded. A manual backup, e.g. before an update:

```bash
docker compose exec backup sh /ops/pg_backup.sh once
```

The dumps live on the same machine as the database: copy `./backups` somewhere else regularly.

To restore a dump and then catch up the bookings made after it from the Google Sheets backup:

```bash
docker compose stop backend bot
docker compose exec -T db pg_restore -U aitu -d aitu_music --clean --if-exists --no-owner < backups/<file>.dump
docker compose run --rm backend python -m app.tools.restore_from_sheets           # check the report
docker compose run --rm backend python -m app.tools.restore_from_sheets --apply
docker compose start backend bot
```

The one-off `run` container does not start the web server, so the sheet is not overwritten before it is read.

## Quick start

```bash
cp .env.example .env     # set BOT_TOKEN, NGROK_AUTHTOKEN, INITIAL_ADMIN_TELEGRAM_IDS, a strong INTERNAL_API_TOKEN
docker compose up -d --build
```

`docker-compose.yml` is the production setup: the code is baked into the image, every service restarts unless stopped,
`db`/`redis`/`backend` have healthchecks (`docker compose ps` shows `healthy`) and the others wait for them, Redis keeps
its data (FSM state, undelivered notifications) in an append-only file on the `redis_data` volume, and container logs are
rotated (3 × 10 MB per service). Only `secrets/` is mounted into the backend. To update:

```bash
git pull && docker compose up -d --build
```

For development use the overlay that mounts the working tree and runs the backend with `--reload`:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

The database password is `aitu` unless `POSTGRES_PASSWORD` is set in `.env`; it must match the password in
`DATABASE_URL`. Postgres applies it only when it creates the volume, so changing it later needs `ALTER USER` inside the
database (or a fresh volume).

API docs: <http://localhost:8001/docs>. All routes except `/health` and `/webapp/*` need the header
`X-Internal-Token`. Through the ngrok tunnel only `/webapp/*` and `/health` are reachable.
The backend refuses to start with an empty `INTERNAL_API_TOKEN`; with `ENVIRONMENT` other than `local` the token must also
be non-default and at least 32 characters (`python -c "import secrets; print(secrets.token_urlsafe(32))"`).
The ngrok inspector (port 4040) is published on `127.0.0.1` only.

Users listed in `INITIAL_ADMIN_TELEGRAM_IDS` become admins on `/start` and cannot be demoted or banned.

## Configuration

Everything is read from `.env` (see `.env.example`); `app/core/config.py` has the defaults.

| Variable | Default | Meaning |
|----------|---------|---------|
| `BOT_TOKEN` | – | Telegram bot token (also signs and checks the Mini App's `initData`) |
| `INTERNAL_API_TOKEN` | – | shared secret between bot and backend; required, and with `ENVIRONMENT` other than `local` at least 32 characters |
| `ENVIRONMENT` | `local` (`.env.example`: `production`) | `local` accepts a weak internal token (dev/tests only) |
| `APP_NAME` | `AITU Music Club` | title of the API docs |
| `DATABASE_URL` | local Postgres | SQLAlchemy URL; the password must match `POSTGRES_PASSWORD` (compose only, default `aitu`) |
| `REDIS_URL` | `redis://localhost:6379/0` | bot FSM state and the notification list |
| `BACKEND_BASE_URL` | `http://localhost:8000` | where the bot finds the backend (`http://backend:8000` in compose) |
| `WEBAPP_BASE_URL` | – | public https URL of the Mini App; overrides `NGROK_DOMAIN` and the ngrok API |
| `NGROK_AUTHTOKEN`, `NGROK_DOMAIN`, `NGROK_API_URL` | – | the tunnel. With a reserved `NGROK_DOMAIN` (`my-club.ngrok-free.app`) the Mini App link survives restarts; empty = random address, found through the ngrok API |
| `INITIAL_ADMIN_TELEGRAM_IDS` | – | comma-separated Telegram ids that become admins on `/start` and cannot be demoted or banned |
| `REGISTRATION_RETRY_HOURS` | `24` | cooldown before a rejected registration may be resubmitted (shown in club time) |
| `DEFAULT_LANGUAGE` | `ru` | language of users who have not chosen one (`ru`, `en`, `kz`) |
| `TIMEZONE` | `Asia/Almaty` | club clock used for bookings, deadlines and the Sunday job |
| `WEEKLY_PLANNING_ENABLED`, `ALLOCATION_TIME` | `false`, `18:00` | weekly planning switch (`false` = sliding 7-day window); Sunday allocation time `HH:MM` |
| `GOOGLE_SHEETS_ID`, `GOOGLE_SERVICE_ACCOUNT_FILE` | – | enable the Sheets backup (both needed) |
| `BACKUP_INTERVAL_HOURS`, `BACKUP_KEEP_DAYS` | `24`, `14` | the `backup` compose service (read by `ops/pg_backup.sh`, not by the app) |
| `LOG_LEVEL`, `SQL_ECHO` | `INFO`, `false` | log level of backend and bot; log every SQL statement |

Booking limits, room hours and closed weekdays are changed by the main admin in the Mini App (*Админ → Настройки*).
Taking the room with priority (DSVR) or for staff events, and changing or cancelling any booking, happen on the Mini App
schedule grid; the bot's admin menu only opens those screens.

## Migrations

The database schema is managed by Alembic (`alembic/versions/`). The backend applies pending migrations (`alembic
upgrade head`) every time it starts, so `docker compose up --build` is the whole upgrade procedure. A database created by
an older version (before Alembic) is adopted by the baseline revision `0001_baseline` in place: missing tables and
columns are added, data is kept. Take a dump first, `docker compose exec backup sh /ops/pg_backup.sh once`.

```bash
docker compose run --rm backend alembic upgrade head     # apply by hand
docker compose run --rm backend alembic current          # which revision the database is at
alembic revision --autogenerate -m "describe change"     # developers: after editing app/db/models.py, then review it
```

## Project layout

```text
app/
  api/routes/   FastAPI routes: internal ones, called only by the bot (users, groups, bands, bookings, move_requests,
                waitlist, planning, events, admin) and the Mini App API (webapp.py, webapp_admin.py, signed initData
                instead of the internal token)
  schemas/      Pydantic request/response models of those routes
  bot/          aiogram handlers (handlers*.py), keyboards, states, API client, outbox delivery, schedule image, Mini App menu button
  core/         settings, timezone (now_local / utcnow), logging, internal-token and Telegram initData validation
  db/           engine, models, startup (migrations run by Alembic, see alembic/)
  locales/      ru / en / kz strings for the bot and the Mini App
  services/     business logic: bookings, bands, users, groups, permissions, schedule_history, audit (action journal),
                admin (club settings), events (concerts), errors (error codes), search, outbox, sheets_sync, move_requests,
                waitlist, repeat_offers, planning + weeks + allocator (weekly mode; allocator is pure)
  tools/        one-off commands, e.g. restore_from_sheets
  webapp/       Telegram Mini App: vanilla-JS static files served at /webapp/static
alembic/        migration revisions (applied on backend start)
ops/            pg_backup.sh, the pg_dump sidecar script
tests/          pytest suite (SQLite in memory)
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Known limitations

- The Sheets sync is one-way; Telegram messages are best-effort (a blocked bot is skipped).
- Concerts (creating them, band applications, personal event history) and weekly planning are still managed in the bot, not
  in the Mini App.
- Without Google Sheets, the Excel export of a concert is `GET /events/{id}/export.xlsx?telegram_id=<admin>` with the internal
  token. It is not reachable through the ngrok tunnel, so call it from the host (`localhost:8001`).
- Profile editing and registration are bot flows; the Mini App only shows the profile.
- The public schedule (`/webapp/api/schedule` without `initData`) is read-only and open on purpose; booking needs a signed session.
- The test suite runs on SQLite. Postgres-only code (the legacy branch of the baseline migration and the advisory locks
  around booking) is not covered by tests; try such changes against a real Postgres.
