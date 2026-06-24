# AITU Music Club Bot MVP

MVP for a Telegram bot and FastAPI backend for AITU Music Club room booking, band management, concert participant lists, and user profiles.

## Features

- First-time user profile onboarding: first name, last name, study group, language.
- Admin approval for new users before they can create bands or book rehearsals.
- Admins receive registration requests with inline approve/reject buttons.
- Rejected registration requests are not bans; users can submit again after `REGISTRATION_RETRY_HOURS`.
- Personal cabinet/profile editing.
- User/admin roles.
- Bands/collectives with member roles.
- Invite codes for joining bands.
- Room booking with admin-managed weekdays and time slots.
- Booking is limited to the next 7 days from the current time.
- Each booking has a song title and purpose: concert preparation or personal rehearsal.
- Public weekly schedule is shown to users when they open the bot.
- Concerts/events with participant list submissions.
- Excel export for concert lists.
- Telegram bot UI with `aiogram 3`.
- Redis-backed bot state storage, so registration dialogs survive bot restarts.
- FastAPI backend with SQLAlchemy, Alembic-ready structure, and PostgreSQL.

## Quick Start

1. Put your Telegram bot token into `.env`:

```env
BOT_TOKEN=123456:your-token
```

2. Add first admin Telegram IDs if needed:

```env
INITIAL_ADMIN_TELEGRAM_IDS=123456789,987654321
```

3. Start services:

```bash
docker compose up --build
```

4. Open API docs:

```text
http://localhost:8000/docs
```

## First Admin

Set `INITIAL_ADMIN_TELEGRAM_IDS` in `.env`:

```env
INITIAL_ADMIN_TELEGRAM_IDS=123456789,987654321
```

When these users open `/start`, they become admins automatically.

## MVP Telegram Flows

- `/start` creates or updates a Telegram user.
- New users must fill first name, last name, study group, and language.
- Registration fields are saved step by step; if the bot restarts mid-registration, it resumes from the missing field.
- After registration, new users are pending until an admin approves them.
- Registration approval can be done directly from the admin notification buttons.
- If an admin rejects a request, the user can retry after the configured cooldown.
- Pending users can only view the weekly rehearsal schedule.
- Users can edit profile data and notification preference in personal cabinet.
- Users can create bands and share invite codes.
- Users can join a band by invite code.
- Users can view members with names, study groups, and instrument roles.
- Users can book available rehearsal slots from a numbered list for the next 7 days.
- Users add rehearsal purpose and song title during booking.
- Users can view concerts and submit participant lists.
- Admins can approve/reject/ban/soft-delete users by `@username` or Telegram ID.
- Admins can view booking history.
- Admins can enable/disable weekdays and create concerts.
- Main navigation uses inline keyboards and edits the current menu message.

## MVP API Highlights

- `GET /health`
- `POST /users/telegram`
- `PATCH /users/{telegram_id}/profile`
- `GET /users?admin_telegram_id=...&status=pending`
- `POST /users/approve`
- `POST /users/reject`
- `POST /users/ban`
- `POST /users/delete`
- `POST /bands`
- `POST /bands/join`
- `GET /bookings/available-slots`
- `GET /bookings/schedule`
- `POST /bookings`
- `GET /admin/bookings/history`
- `POST /events`
- `POST /events/{event_id}/applications`
- `GET /events/{event_id}/export.xlsx`

All API routes except `/health` require the internal header:

```text
X-Internal-Token: change-me
```

## Project Layout

```text
app/
  api/         FastAPI routes
  bot/         Telegram bot handlers, keyboards, states
  core/        settings and security
  db/          database engine and models
  schemas/     Pydantic schemas
  services/    business logic
  locales/     ru/kz/en texts
```

## Notes

This is an MVP foundation. For production, add HTTPS, backups, monitoring, stronger admin auth, tests, migrations in CI, and a deployment pipeline.

Current MVP limitations:

- Reminder delivery is not implemented yet; notification preferences are stored.
- Slot creation exists in the API, but the Telegram admin UI currently only toggles weekdays.
- Excel export is available through the API endpoint.
- User deletion is soft deletion, so old booking history remains auditable.
- If you already ran an older database, recreate it or add an Alembic migration because the MVP uses `create_all` for fresh setup.
