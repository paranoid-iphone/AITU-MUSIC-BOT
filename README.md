# AITU Music Club Bot

Telegram bot + FastAPI backend that plans a **shared rehearsal room** for several club groups (music, dance,
cheerleading): weekly requests with priorities, an automatic Sunday allocation, DSVR priority, schedule history and a
Google Sheets backup.

## How the week works

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
Set `WEEKLY_PLANNING_ENABLED=false` to go back to plain first-come-first-served booking.

## Roles

| Role | Can do |
|------|--------|
| user | request/book for own bands, edit own bookings, see own band history |
| band leader / **responsible person** | submit weekly requests; the responsible person is notified about every change of the band's schedule and is shown in the schedule table |
| group admin (music / dance / cheer) | approve registrations and manage bands of **their own group** |
| **DSVR** | take the room at any time up to 30 days ahead: overlapping bookings are cancelled and their people notified; can edit any booking |
| admin | everything, including groups, group admins, DSVR role, limits, working hours, allocation preview/run |

New users pick their group (music / dance / cheer) during registration; the request goes to that group's admins and
to the main admins. Main admins assign group admins in *Админ → Группы* and DSVR in *Админ → Админы*.

## Schedule history and notifications

Every create / edit / cancel / DSVR takeover / automatic assignment is stored with who did it and before/after values
(*Админ → История → История расписания*, and *История изменений* on a band card). Deleting a booking cancels it, so
it stays in the history. The backend pushes notifications to a Redis list (`bot:outbox`); the bot delivers them and
refreshes the schedule menus.

## Google Sheets backup

One-way (bot → sheet) so the schedule survives a server outage:

- sheet **Расписание** – current and next week's bookings, rewritten after each change;
- sheet **История** – every schedule change appended once.

Setup:

1. Google Cloud: create a project, enable the *Google Sheets API*, create a **service account** and download its JSON
   key to `secrets/google-service-account.json` (the folder is git-ignored).
2. Create a Google Sheet and share it with the service account e-mail as **Editor**.
3. Put the sheet id (the part of its URL between `/d/` and `/edit`) into `.env`:

```env
GOOGLE_SHEETS_ID=1AbC...
GOOGLE_SERVICE_ACCOUNT_FILE=secrets/google-service-account.json
```

The sheet contains names and Telegram usernames, so share it carefully. Restoring *from* the sheet is not automated.

## Quick start

```bash
cp .env.example .env     # set BOT_TOKEN, NGROK_AUTHTOKEN, INITIAL_ADMIN_TELEGRAM_IDS, a strong INTERNAL_API_TOKEN
docker compose up --build
```

API docs: <http://localhost:8001/docs>. All routes except `/health` and `/webapp/*` need the header
`X-Internal-Token`. Through the ngrok tunnel only `/webapp/*` and `/health` are reachable.
With `ENVIRONMENT` other than `local` the backend refuses to start with the default `INTERNAL_API_TOKEN`.

Users listed in `INITIAL_ADMIN_TELEGRAM_IDS` become admins on `/start` and cannot be demoted or banned.

## Configuration

| Variable | Meaning |
|----------|---------|
| `BOT_TOKEN`, `INTERNAL_API_TOKEN` | Telegram bot token; shared secret between bot and backend |
| `TIMEZONE` (`Asia/Almaty`) | club clock used for bookings, deadlines and the Sunday job |
| `WEEKLY_PLANNING_ENABLED`, `ALLOCATION_TIME` | weekly planning switch; Sunday allocation time `HH:MM` |
| `GOOGLE_SHEETS_ID`, `GOOGLE_SERVICE_ACCOUNT_FILE` | enable the Sheets backup |
| `REGISTRATION_RETRY_HOURS` | cooldown before a rejected registration may be resubmitted |

Booking limits, room hours and closed weekdays are changed by admins in the bot.

## Project layout

```text
app/
  api/routes/   FastAPI routes (users, groups, bands, bookings, planning, history, events, admin)
  bot/          aiogram handlers (handlers*.py), keyboards, states, API client, schedule image
  core/         settings, timezone, internal-token and Telegram initData validation
  db/           engine, models, startup migrations
  services/     business logic: bookings, planning, allocator (pure), schedule_history, outbox, sheets_sync, permissions
tests/          pytest suite (SQLite in memory)
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Known limitations

- The schema is created with `create_all` plus idempotent `ALTER TABLE` statements at startup (Alembic is set up but has
  no migrations yet).
- The Sheets sync is one-way; Telegram messages are best-effort (a blocked bot is skipped).
- Excel export of concert lists is available through `GET /events/{id}/export.xlsx?telegram_id=<admin>`.
