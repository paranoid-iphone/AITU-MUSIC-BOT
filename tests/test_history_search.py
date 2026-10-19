"""Searching the schedule history by text, action and rehearsal date, and the booking_date column behind it."""
from datetime import date, datetime, time

from alembic import command
from sqlalchemy import create_engine, text

from app.db import models
from app.db.migrate import alembic_config
from app.services import bookings
from tests.test_webapp import client, tg  # noqa: F401  (`client` is a fixture)
from tests.test_webapp_admin import API, add_changes, staff

NOW = datetime(2026, 10, 14, 12, 0)  # Wednesday
DAY = date(2026, 10, 15)


def _ids(body) -> list[int]:
    return [item["id"] for item in body["items"]]


async def _history(client, user, query: str = "") -> dict:
    response = await client.get(f"{API}/history{query}", headers=tg(user.telegram_id))
    assert response.status_code == 200, response.text
    return response.json()


async def test_search_matches_the_band_or_who_made_the_change_and_treats_wildcards_literally(client, session, make_user):
    main_admin, _, _, _ = await staff(make_user, session)
    actor = await make_user(last_name="Ivanov", first_name="Oleg", telegram_username="olegi")
    rockers = (await add_changes(session, "music", 1, "Rockers"))[0]
    percent = (await add_changes(session, "music", 1, "100% Jazz"))[0]
    underscore = (await add_changes(session, "music", 1, "A_B"))[0]
    rockers.actor_id = actor.id
    await session.commit()
    rockers_id, percent_id, underscore_id = rockers.id, percent.id, underscore.id

    assert _ids(await _history(client, main_admin, "?q=rock")) == [rockers_id]
    assert _ids(await _history(client, main_admin, "?q=ROCKERS")) == [rockers_id]
    assert _ids(await _history(client, main_admin, "?q=ivan")) == [rockers_id]  # last name of the actor
    assert _ids(await _history(client, main_admin, "?q=@olegi")) == [rockers_id]  # username, with the @
    assert _ids(await _history(client, main_admin, "?q=%25")) == [percent_id]  # a literal percent sign
    assert _ids(await _history(client, main_admin, "?q=a_b")) == [underscore_id]
    assert _ids(await _history(client, main_admin, "?q=a_")) == [underscore_id]  # "_" is not "any character"
    assert _ids(await _history(client, main_admin, "?q=nobody")) == []
    assert (await client.get(f"{API}/history?q={'x' * 101}", headers=tg(main_admin.telegram_id))).status_code == 422


async def test_filter_by_action_and_rehearsal_date_and_combine_them_with_paging(client, session, make_user):
    main_admin, _, _, _ = await staff(make_user, session)
    created = await add_changes(session, "music", 3, "Rockers")
    cancelled = (await add_changes(session, "music", 1, "Rockers"))[0]
    cancelled.action = "cancelled"
    for index, row in enumerate(created):
        row.booking_date = date(2026, 1, 5) if index < 2 else date(2026, 1, 6)
    cancelled.booking_date = date(2026, 1, 5)
    await session.commit()
    created_ids = [row.id for row in created]
    cancelled_id = cancelled.id

    assert _ids(await _history(client, main_admin, "?action=cancelled")) == [cancelled_id]
    assert _ids(await _history(client, main_admin, "?date=2026-01-06")) == [created_ids[2]]
    assert _ids(await _history(client, main_admin, "?date=2026-01-05&action=created")) == [created_ids[1], created_ids[0]]

    first = await _history(client, main_admin, "?date=2026-01-05&limit=2")
    assert _ids(first) == [cancelled_id, created_ids[1]] and first["next_before_id"] == created_ids[1]
    second = await _history(client, main_admin, f"?date=2026-01-05&limit=2&before_id={first['next_before_id']}")
    assert _ids(second) == [created_ids[0]] and second["next_before_id"] is None

    assert (await client.get(f"{API}/history?action=teleported", headers=tg(main_admin.telegram_id))).status_code == 422
    assert (await client.get(f"{API}/history?date=tomorrow", headers=tg(main_admin.telegram_id))).status_code == 422


async def test_searching_never_widens_what_a_group_admin_may_see(client, session, make_user):
    _, group_admin, _, _ = await staff(make_user, session)
    mine = (await add_changes(session, "music", 1, "Rockers"))[0]
    await add_changes(session, "dance", 1, "Rockers Dance")
    mine_id = mine.id

    assert _ids(await _history(client, group_admin, "?q=rockers")) == [mine_id]


async def test_a_new_change_remembers_the_rehearsal_date(session, make_user, make_band, freeze_now):
    freeze_now(NOW)
    leader = await make_user()
    band = await make_band(leader, "Rockers")
    await bookings.create_booking(session, leader.telegram_id, band.id, DAY, time(15), time(16), "self", None)

    change = (await session.execute(models.ScheduleChange.__table__.select())).first()
    assert change.action == "created" and change.booking_date == DAY


def test_migration_fills_the_date_of_existing_changes(tmp_path):
    path = tmp_path / "history.db"
    config = alembic_config(f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, "0002_drop_time_slots")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            for after, before in (('{"date": "2026-01-05"}', "null"), ("null", '{"date": "2026-02-01"}'), ("null", "null")):
                conn.execute(
                    text("INSERT INTO schedule_changes (action, before, after, created_at) VALUES ('x', :before, :after, '2026-01-01 10:00:00')"),
                    {"before": before, "after": after},
                )

        command.upgrade(config, "head")

        with engine.connect() as conn:
            dates = conn.execute(text("SELECT booking_date FROM schedule_changes ORDER BY id")).scalars().all()
        assert dates == ["2026-01-05", "2026-02-01", None]

        command.upgrade(config, "head")  # idempotent
        command.downgrade(config, "0002_drop_time_slots")
        with engine.connect() as conn:
            columns = [row[1] for row in conn.execute(text("PRAGMA table_info(schedule_changes)"))]
        assert "booking_date" not in columns
    finally:
        engine.dispose()
