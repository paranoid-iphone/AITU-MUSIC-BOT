"""Who holds a booking and why it changed, as data the Mini App translates (the Russian text stays for the bot and Sheets)."""
from datetime import datetime

import pytest

from app.bot.i18n import load_locale
from app.db import models
from app.services.schedule_history import FIXED_REASONS, owner_kind, reason_parts
from tests.test_webapp import NOW, booking_body, client, tg  # noqa: F401  (`client` is a fixture)
from tests.test_webapp_admin import API, staff

SCHEDULE = "/webapp/api/schedule"


def test_fixed_reasons_map_to_a_code():
    assert reason_parts("Коллектив удалён") == {"reason_code": "band_deleted"}
    assert reason_parts("Пользователь заблокирован") == {"reason_code": "user_banned"}
    assert reason_parts("Пользователь удалён") == {"reason_code": "user_deleted"}


def test_preemption_reasons_split_into_who_and_title():
    assert reason_parts("Вытеснено ДСВР: Концерт") == {"reason_code": "preempted", "reason_by": "dsvr", "reason_title": "Концерт"}
    assert reason_parts("Вытеснено админом: ") == {"reason_code": "preempted", "reason_by": "admin", "reason_title": ""}
    assert reason_parts("Вытеснено админом: два\nстроки")["reason_title"] == "два\nстроки"


def test_free_text_and_empty_reasons_have_no_parts():
    assert reason_parts("Репетиция переехала") == {}
    assert reason_parts("") == {}
    assert reason_parts(None) == {}


@pytest.mark.parametrize("language", ["ru", "en", "kz"])
def test_every_reason_code_has_a_translation(language):
    strings = load_locale(language)
    for code in FIXED_REASONS.values():
        assert strings.get(f"webapp.admin.history.reason_code.{code}"), f"{language}: {code}"
    for by in ("admin", "dsvr"):
        assert "{title}" in strings[f"webapp.admin.history.reason_preempted_{by}"], f"{language}: {by}"
    for key in ("personal", "staff", "system"):
        assert strings.get(f"webapp.label.{key}"), f"{language}: {key}"


def test_owner_kind_comes_from_the_snapshot_and_the_band():
    def change(band_id, **snapshots):
        return models.ScheduleChange(band_id=band_id, action="created", **snapshots)

    assert owner_kind(change(None, after={"purpose": "personal"})) == "personal"
    assert owner_kind(change(None, before={"purpose": "staff"}, after=None)) == "staff"
    assert owner_kind(change(7, after={"purpose": "personal"})) == "band"  # a band always wins
    assert owner_kind(change(None, after={"purpose": "self"})) == "band"
    assert owner_kind(change(None)) == "band"


async def test_schedule_marks_who_holds_each_booking_without_naming_a_person(client, session, make_user, make_band, freeze_now):
    freeze_now(NOW)
    leader = await make_user()
    band = await make_band(leader, "Rockers")
    solo = await make_user(last_name="Petrov", first_name="Ivan")
    assert (await client.post("/webapp/api/bookings", json=booking_body(band.id), headers=tg(leader.telegram_id))).status_code == 200
    personal = booking_body(None, start="12:00", end="13:00", purpose="personal")
    assert (await client.post("/webapp/api/bookings", json=personal, headers=tg(solo.telegram_id))).status_code == 200

    items = (await client.get(SCHEDULE, headers=tg(leader.telegram_id))).json()["bookings"]

    by_start = {item["start_time"]: item for item in items}
    assert (by_start["10:00:00"]["owner_kind"], by_start["10:00:00"]["owner_person"]) == ("band", None)
    assert (by_start["12:00:00"]["owner_kind"], by_start["12:00:00"]["owner_person"]) == ("personal", None)


async def test_my_bookings_name_the_person_of_a_personal_rehearsal(client, session, make_user, freeze_now):
    freeze_now(NOW)
    solo = await make_user(last_name="Petrov", first_name="Ivan")
    personal = booking_body(None, purpose="personal")
    assert (await client.post("/webapp/api/bookings", json=personal, headers=tg(solo.telegram_id))).status_code == 200

    items = (await client.get("/webapp/api/my-bookings", headers=tg(solo.telegram_id))).json()["bookings"]

    assert [(item["owner_kind"], item["owner_person"]) for item in items] == [("personal", "Petrov Ivan")]


async def test_history_rows_carry_owner_actor_and_reason_parts(client, session, make_user, make_group_admin):
    main_admin, _, _, _ = await staff(make_user, make_group_admin)
    session.add_all(
        [
            models.ScheduleChange(
                band_name="Личная репетиция",
                action="cancelled",
                reason="Пользователь заблокирован",
                before={"date": "2026-01-05", "start": "18:00", "end": "19:00", "purpose": "personal"},
                created_at=datetime(2026, 1, 1, 20, 0),
            ),
            models.ScheduleChange(
                band_name="Rockers",
                action="preempted",
                reason="Вытеснено ДСВР: Концерт",
                after={"date": "2026-01-05", "start": "18:00", "end": "19:00", "purpose": "band"},
                created_at=datetime(2026, 1, 1, 21, 0),
            ),
        ]
    )
    await session.commit()

    body = (await client.get(f"{API}/history", headers=tg(main_admin.telegram_id))).json()

    newest, oldest = body["items"]
    assert (newest["owner_kind"], newest["reason_code"], newest["reason_by"], newest["reason_title"]) == ("band", "preempted", "dsvr", "Концерт")
    assert (oldest["owner_kind"], oldest["reason_code"], oldest["actor_is_system"]) == ("personal", "user_banned", True)
