"""Mini App admin API: requests, users and roles, plus the user notifications those actions queue."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, func, select

from app.core.config import settings
from app.db import models
from tests.test_webapp import client, tg  # noqa: F401  (`client` is a fixture)
from tests.test_webapp_auth import init_data

API = "/webapp/api/admin"
APPROVED_TEXT = "Ваша регистрация одобрена. Теперь доступны все функции AITU Music Club Bot."
REJECTED_TEXT = (
    "Ваша регистрация отклонена, но это не бан.\n"
    "Повторно подать заявку можно после: {retry_after}.\n"
    "Когда срок пройдет, отправьте /start."
)


async def group_id(session, slug: str) -> int:
    return (await session.execute(select(models.Group.id).where(models.Group.slug == slug))).scalar_one()


async def staff(make_user, make_group_admin):
    """A main admin, a group admin of `music`, a pending `music` user and a pending user of another group."""
    main_admin = await make_user(role="admin", group_slug=None)
    group_admin = await make_user(group_slug="music")
    await make_group_admin(group_admin, "music")
    pending_music = await make_user(status="pending", group_slug="music")
    pending_other = await make_user(status="pending", group_slug="dance")
    return main_admin, group_admin, pending_music, pending_other


def notifications(published) -> list[dict]:
    return [event for event in published if event["type"] == "notify"]


async def test_admin_endpoints_reject_everyone_but_admins(client, make_user):
    plain = await make_user()
    dsvr = await make_user(role="dsvr")
    pending = await make_user(status="pending")
    for user in (plain, dsvr, pending):
        assert (await client.get(f"{API}/users", headers=tg(user.telegram_id))).status_code == 403
    assert (await client.get(f"{API}/users")).status_code == 401


async def test_group_admin_sees_only_their_group_and_counts_match(client, session, make_user, make_group_admin):
    main_admin, group_admin, pending_music, pending_other = await staff(make_user, make_group_admin)

    scoped = (await client.get(f"{API}/users", headers=tg(group_admin.telegram_id))).json()
    ids = {item["telegram_id"] for item in scoped["users"]}
    assert pending_music.telegram_id in ids and pending_other.telegram_id not in ids
    assert scoped["counts"]["pending"] == 1

    everything = (await client.get(f"{API}/users", headers=tg(main_admin.telegram_id))).json()
    ids = {item["telegram_id"] for item in everything["users"]}
    assert {pending_music.telegram_id, pending_other.telegram_id} <= ids
    assert everything["counts"]["pending"] == 2


async def test_users_filter_by_status_and_search(client, session, make_user, make_group_admin):
    main_admin, _, pending_music, pending_other = await staff(make_user, make_group_admin)
    named = await make_user(last_name="Уникальный", telegram_username="needle_guy")
    headers = tg(main_admin.telegram_id)

    pending = (await client.get(f"{API}/users", params={"status": "pending"}, headers=headers)).json()
    assert {item["telegram_id"] for item in pending["users"]} == {pending_music.telegram_id, pending_other.telegram_id}
    assert pending["counts"]["approved"] >= 1  # counts ignore the status filter

    for query in ("Уникальн", "needle", "@needle_guy", str(named.telegram_id)):
        found = (await client.get(f"{API}/users", params={"q": query}, headers=headers)).json()["users"]
        assert [item["telegram_id"] for item in found] == [named.telegram_id], query

    assert (await client.get(f"{API}/users", params={"q": "%"}, headers=headers)).json()["users"] == []
    assert (await client.get(f"{API}/users", params={"q": "ne_dle"}, headers=headers)).json()["users"] == []
    blank = (await client.get(f"{API}/users", params={"q": "   "}, headers=headers)).json()["users"]
    unfiltered = (await client.get(f"{API}/users", headers=headers)).json()["users"]
    assert len(blank) == len(unfiltered) > 1


async def test_approve_notifies_once_and_is_idempotent(client, session, make_user, make_group_admin, published):
    main_admin, _, pending_music, _ = await staff(make_user, make_group_admin)
    url = f"{API}/users/{pending_music.telegram_id}/approve"

    first = await client.post(url, headers=tg(main_admin.telegram_id))
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "approved"
    assert notifications(published) == [{"type": "notify", "telegram_id": pending_music.telegram_id, "text": APPROVED_TEXT}]

    second = await client.post(url, headers=tg(main_admin.telegram_id))
    assert second.status_code == 200 and second.json()["status"] == "approved"
    assert len(notifications(published)) == 1


async def test_reject_ban_delete_store_reason_and_only_reject_notifies(client, session, make_user, make_group_admin, published):
    main_admin, _, pending_music, pending_other = await staff(make_user, make_group_admin)
    victim = await make_user()
    headers = tg(main_admin.telegram_id)

    rejected = await client.post(
        f"{API}/users/{pending_music.telegram_id}/reject", json={"reason": "не студент"}, headers=headers
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected" and rejected.json()["moderation_reason"] == "не студент"
    sent = notifications(published)
    assert len(sent) == 1 and sent[0]["telegram_id"] == pending_music.telegram_id
    assert sent[0]["text"].startswith("Ваша регистрация отклонена, но это не бан.")
    assert "{retry_after}" not in sent[0]["text"]

    banned = await client.post(f"{API}/users/{pending_other.telegram_id}/ban", json={"reason": "спам"}, headers=headers)
    assert banned.json()["status"] == "banned"
    deleted = await client.post(f"{API}/users/{victim.telegram_id}/delete", json={}, headers=headers)
    assert deleted.json()["status"] == "deleted"
    assert len(notifications(published)) == 1  # ban and delete stay silent

    too_long = await client.post(f"{API}/users/{victim.telegram_id}/ban", json={"reason": "x" * 256}, headers=headers)
    assert too_long.status_code == 422


async def test_reject_shows_the_retry_moment_in_club_time(client, session, make_user, make_group_admin, published, freeze_now):
    main_admin, _, pending_music, _ = await staff(make_user, make_group_admin)
    freeze_now(datetime(2026, 10, 20, 12, 0))

    rejected = await client.post(
        f"{API}/users/{pending_music.telegram_id}/reject", json={"reason": "x"}, headers=tg(main_admin.telegram_id)
    )

    assert rejected.status_code == 200, rejected.text
    expected = (datetime(2026, 10, 20, 12, 0) + timedelta(hours=settings.registration_retry_hours)).strftime("%d.%m %H:%M")
    assert notifications(published)[0]["text"] == REJECTED_TEXT.format(retry_after=expected)
    stored = (await session.execute(select(models.User.registration_retry_after).where(models.User.id == pending_music.id))).scalar_one()
    assert stored == datetime(2026, 10, 20, 12, 0) + timedelta(hours=settings.registration_retry_hours)


async def test_group_admin_cannot_touch_staff_other_groups_or_themselves(client, session, make_user, make_group_admin):
    main_admin, group_admin, _, pending_other = await staff(make_user, make_group_admin)
    headers = tg(group_admin.telegram_id)
    for target in (main_admin, pending_other, group_admin):
        assert (await client.post(f"{API}/users/{target.telegram_id}/ban", json={}, headers=headers)).status_code == 403
    target = await make_user(group_slug="music")
    music = await group_id(session, "music")
    assert (await client.get(f"{API}/roles", headers=headers)).status_code == 403
    body = {"target": str(target.telegram_id), "role_id": 1, "group_id": music}
    assert (await client.post(f"{API}/role-assignments", json=body, headers=headers)).status_code == 403


async def test_group_admin_cannot_restore_a_banned_user_but_main_admin_can(client, session, make_user, make_group_admin):
    main_admin, group_admin, _, _ = await staff(make_user, make_group_admin)
    banned = await make_user(status="banned", group_slug="music")
    url = f"{API}/users/{banned.telegram_id}/approve"
    assert (await client.post(url, headers=tg(group_admin.telegram_id))).status_code == 403
    restored = await client.post(url, headers=tg(main_admin.telegram_id))
    assert restored.status_code == 200 and restored.json()["status"] == "approved"


async def test_users_without_a_group_are_invisible_to_group_admins(client, session, make_user, make_group_admin):
    main_admin, group_admin, _, _ = await staff(make_user, make_group_admin)
    loner = await make_user(status="pending", group_slug=None)

    listed = (await client.get(f"{API}/users", headers=tg(group_admin.telegram_id))).json()["users"]
    assert loner.telegram_id not in {item["telegram_id"] for item in listed}
    assert (await client.get(f"{API}/users/{loner.telegram_id}", headers=tg(group_admin.telegram_id))).status_code == 403
    assert (await client.post(f"{API}/users/{loner.telegram_id}/approve", headers=tg(group_admin.telegram_id))).status_code == 403

    as_main = (await client.get(f"{API}/users", headers=tg(main_admin.telegram_id))).json()["users"]
    assert loner.telegram_id in {item["telegram_id"] for item in as_main}
    assert (await client.get(f"{API}/users/{loner.telegram_id}", headers=tg(main_admin.telegram_id))).status_code == 200


async def test_initial_admin_and_self_are_protected(client, session, make_user, make_group_admin, monkeypatch):
    main_admin, _, pending_music, _ = await staff(make_user, make_group_admin)
    headers = tg(main_admin.telegram_id)
    monkeypatch.setattr(type(settings), "initial_admin_ids", property(lambda self: {pending_music.telegram_id}))

    assert (await client.post(f"{API}/users/{pending_music.telegram_id}/ban", json={}, headers=headers)).status_code == 403
    assert (await client.post(f"{API}/users/{main_admin.telegram_id}/ban", json={}, headers=headers)).status_code == 403


async def test_stale_init_data_reads_but_cannot_change(client, session, make_user, make_group_admin):
    main_admin, _, pending_music, _ = await staff(make_user, make_group_admin)
    stale = {"X-Telegram-Init-Data": init_data(main_admin.telegram_id, age=2 * 60 * 60)}
    assert (await client.get(f"{API}/users", headers=stale)).status_code == 200
    assert (await client.post(f"{API}/users/{pending_music.telegram_id}/approve", headers=stale)).status_code == 401


async def test_notifications_respect_the_opt_out(client, session, make_user, make_group_admin, published):
    main_admin, _, _, _ = await staff(make_user, make_group_admin)
    quiet = await make_user(status="pending", notifications_enabled=False)
    audits_before = (await session.execute(select(func.count()).select_from(models.AuditLog))).scalar_one()

    response = await client.post(f"{API}/users/{quiet.telegram_id}/approve", headers=tg(main_admin.telegram_id))
    assert response.status_code == 200 and response.json()["status"] == "approved"
    assert notifications(published) == []
    audits_after = (await session.execute(select(func.count()).select_from(models.AuditLog))).scalar_one()
    assert audits_after == audits_before + 1


@pytest.mark.parametrize("action", ["reject", "delete"])
async def test_group_admin_cannot_lift_a_ban_through_reject_or_delete(client, session, make_user, make_group_admin, action):
    main_admin, group_admin, _, _ = await staff(make_user, make_group_admin)
    banned = await make_user(status="banned", group_slug="music")
    url = f"{API}/users/{banned.telegram_id}/{action}"

    assert (await client.post(url, json={}, headers=tg(group_admin.telegram_id))).status_code == 403
    await session.refresh(banned)
    assert banned.status == "banned"

    assert (await client.post(url, json={}, headers=tg(main_admin.telegram_id))).status_code == 200


class _Callback:
    def __init__(self, data: str):
        self.data = data
        self.from_user = type("User", (), {"id": 1})()
        self.answers: list[tuple] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


@pytest.mark.parametrize("action", ["approve", "reject"])
@pytest.mark.parametrize("status, acted", [("approved", False), ("banned", False), ("rejected", False), ("pending", True)])
async def test_registration_buttons_only_act_on_pending_requests(monkeypatch, action, status, acted):
    from app.bot import handlers

    calls: list[tuple] = []

    class FakeApi:
        async def get_user(self, telegram_id, requester=None):
            return {"telegram_id": telegram_id, "status": status}

        async def moderate_user(self, *args):
            calls.append(args)
            raise RuntimeError("stop here")  # the handler answers "no access" and returns

    monkeypatch.setattr(handlers, "api", FakeApi())
    callback = _Callback(f"registration:{action}:5")
    handler = handlers.inline_registration_approve if action == "approve" else handlers.inline_registration_reject
    await handler(callback)

    assert bool(calls) is acted
    if not acted:
        assert callback.answers == [("Заявка уже обработана", True)]


SETTINGS = f"{API}/settings"


async def test_settings_are_for_the_main_admin_only(client, session, make_user, make_group_admin):
    _, group_admin, _, _ = await staff(make_user, make_group_admin)
    dsvr = await make_user(role="dsvr")
    plain = await make_user()
    bodies = {
        "days": {"day_of_week": 1, "is_enabled": False},
        "window": {"start_time": "09:00", "end_time": "20:00"},
        "limits": {"weekly_hours": 3},
        "personal": {"enabled": False},
    }
    for user in (group_admin, dsvr, plain):
        headers = tg(user.telegram_id)
        assert (await client.get(SETTINGS, headers=headers)).status_code == 403
        for name, body in bodies.items():
            assert (await client.put(f"{SETTINGS}/{name}", json=body, headers=headers)).status_code == 403
    assert (await client.get(SETTINGS)).status_code == 401


async def test_settings_start_with_the_club_defaults(client, session, make_user):
    admin = await make_user(role="admin", group_slug=None)
    await session.execute(delete(models.AvailableDay))  # the test database seeds every day open; an unseeded club has the defaults
    await session.commit()
    data = (await client.get(SETTINGS, headers=tg(admin.telegram_id))).json()

    assert [item["day_of_week"] for item in data["days"]] == list(range(7))
    assert [item["is_enabled"] for item in data["days"]] == [True] * 6 + [False]
    assert data["window"] == {"start_time": "08:00", "end_time": "21:00"}
    assert (data["weekly_hours"], data["daily_hours"], data["max_future_bookings"]) == (4, 0, 0)
    assert data["personal_enabled"] is True


async def test_closing_a_day_is_saved_announced_and_reopening_clears_the_note(client, make_user, published):
    admin = await make_user(role="admin", group_slug=None)
    headers = tg(admin.telegram_id)

    closed = await client.put(
        f"{SETTINGS}/days", json={"day_of_week": 2, "is_enabled": False, "occupied_by": " Debate Club "}, headers=headers
    )
    assert closed.status_code == 200
    assert closed.json() == {"day_of_week": 2, "is_enabled": False, "occupied_by": "Debate Club"}
    assert {"type": "schedule_changed"} in published
    days = (await client.get(SETTINGS, headers=headers)).json()["days"]
    assert days[2] == {"day_of_week": 2, "is_enabled": False, "occupied_by": "Debate Club"}

    reopened = await client.put(f"{SETTINGS}/days", json={"day_of_week": 2, "is_enabled": True}, headers=headers)
    assert reopened.json()["occupied_by"] is None
    assert (await client.put(f"{SETTINGS}/days", json={"day_of_week": 7, "is_enabled": True}, headers=headers)).status_code == 422


async def test_the_room_hours_reach_the_schedule_and_must_be_ordered(client, make_user, published):
    admin = await make_user(role="admin", group_slug=None)
    headers = tg(admin.telegram_id)

    saved = await client.put(f"{SETTINGS}/window", json={"start_time": "10:00", "end_time": "20:30"}, headers=headers)
    assert saved.status_code == 200 and saved.json() == {"start_time": "10:00", "end_time": "20:30"}
    assert {"type": "schedule_changed"} in published
    schedule = (await client.get("/webapp/api/schedule", headers=headers)).json()
    assert (schedule["window_start_minutes"], schedule["window_end_minutes"]) == (600, 1230)

    for start, end in (("20:00", "10:00"), ("10:00", "10:00")):
        response = await client.put(f"{SETTINGS}/window", json={"start_time": start, "end_time": end}, headers=headers)
        assert response.status_code == 409
    assert (await client.get(SETTINGS, headers=headers)).json()["window"] == {"start_time": "10:00", "end_time": "20:30"}


async def test_limits_change_only_what_is_sent(client, make_user):
    admin = await make_user(role="admin", group_slug=None)
    headers = tg(admin.telegram_id)

    first = await client.put(f"{SETTINGS}/limits", json={"weekly_hours": 3.5, "max_future_bookings": 3}, headers=headers)
    assert first.json() == {"weekly_hours": 3.5, "daily_hours": 0, "max_future_bookings": 3}
    second = await client.put(f"{SETTINGS}/limits", json={"daily_hours": 1.5}, headers=headers)
    assert second.json() == {"weekly_hours": 3.5, "daily_hours": 1.5, "max_future_bookings": 3}
    off = await client.put(f"{SETTINGS}/limits", json={"daily_hours": 0, "max_future_bookings": 0}, headers=headers)
    assert off.json() == {"weekly_hours": 3.5, "daily_hours": 0, "max_future_bookings": 0}

    for bad in ({"weekly_hours": 0}, {"weekly_hours": 25}, {"daily_hours": -1}, {"max_future_bookings": 101}):
        assert (await client.put(f"{SETTINGS}/limits", json=bad, headers=headers)).status_code == 422


async def test_room_hours_must_sit_on_the_half_hour_grid(client, make_user):
    admin = await make_user(role="admin", group_slug=None)
    headers = tg(admin.telegram_id)

    for start, end in (("10:15", "20:00"), ("10:00", "20:45")):
        response = await client.put(f"{SETTINGS}/window", json={"start_time": start, "end_time": end}, headers=headers)
        assert response.status_code == 409
    assert (await client.get(SETTINGS, headers=headers)).json()["window"] == {"start_time": "08:00", "end_time": "21:00"}


async def test_a_day_cannot_be_longer_than_a_week_and_a_rejected_change_stores_nothing(client, make_user):
    admin = await make_user(role="admin", group_slug=None)
    headers = tg(admin.telegram_id)
    await client.put(f"{SETTINGS}/limits", json={"weekly_hours": 3, "daily_hours": 2}, headers=headers)

    too_long_day = await client.put(
        f"{SETTINGS}/limits", json={"daily_hours": 5, "max_future_bookings": 9}, headers=headers
    )
    shorter_week = await client.put(f"{SETTINGS}/limits", json={"weekly_hours": 1}, headers=headers)

    assert too_long_day.status_code == 409 and shorter_week.status_code == 409
    data = (await client.get(SETTINGS, headers=headers)).json()
    assert (data["weekly_hours"], data["daily_hours"], data["max_future_bookings"]) == (3, 2, 0)


async def test_user_list_accepts_only_known_statuses(client, session, make_user):
    admin = await make_user(role="admin", group_slug=None)
    headers = tg(admin.telegram_id)

    assert (await client.get(f"{API}/users", params={"status": "pending"}, headers=headers)).status_code == 200
    assert (await client.get(f"{API}/users", params={"status": "nonsense"}, headers=headers)).status_code == 422


async def test_personal_rehearsals_can_be_switched_off_for_everyone(client, make_user):
    admin = await make_user(role="admin", group_slug=None)
    member = await make_user()

    off = await client.put(f"{SETTINGS}/personal", json={"enabled": False}, headers=tg(admin.telegram_id))
    assert off.json() == {"personal_enabled": False}
    assert (await client.get(SETTINGS, headers=tg(admin.telegram_id))).json()["personal_enabled"] is False
    assert (await client.get("/webapp/api/my-bands", headers=tg(member.telegram_id))).json()["personal_enabled"] is False


async def add_changes(session, group_slug: str | None, count: int, name: str, start_id_hour: int = 0) -> list[models.ScheduleChange]:
    gid = await group_id(session, group_slug) if group_slug else None
    rows = [
        models.ScheduleChange(
            band_name=name,
            group_id=gid,
            action="created",
            after={"date": "2026-01-05", "start": "18:00", "end": "19:00", "purpose": "band", "song_title": None, "status": "booked"},
            created_at=datetime(2026, 1, 1, 20, 0) + timedelta(minutes=start_id_hour + index),
        )
        for index in range(count)
    ]
    session.add_all(rows)
    await session.commit()
    return rows


async def test_history_is_open_to_admins_dsvr_and_group_admins_only(client, session, make_user, make_group_admin):
    main_admin, group_admin, _, _ = await staff(make_user, make_group_admin)
    dsvr = await make_user(role="dsvr")
    plain = await make_user()
    for user in (main_admin, group_admin, dsvr):
        assert (await client.get(f"{API}/history", headers=tg(user.telegram_id))).status_code == 200
    assert (await client.get(f"{API}/history", headers=tg(plain.telegram_id))).status_code == 403
    assert (await client.get(f"{API}/history")).status_code == 401
    # DSVR get the history only; the rest of the admin screen stays closed to them.
    assert (await client.get(f"{API}/users", headers=tg(dsvr.telegram_id))).status_code == 403


async def test_history_is_scoped_by_role_and_lists_visible_groups(client, session, make_user, make_group_admin):
    main_admin, group_admin, _, _ = await staff(make_user, make_group_admin)
    dsvr = await make_user(role="dsvr")
    await add_changes(session, "music", 1, "Music")
    await add_changes(session, "dance", 1, "Dance")

    def names(body):
        return {item["band_name"] for item in body["items"]}

    scoped = (await client.get(f"{API}/history", headers=tg(group_admin.telegram_id))).json()
    assert names(scoped) == {"Music"}
    assert [group["slug"] for group in scoped["groups"]] == ["music"]
    for viewer in (main_admin, dsvr):
        everything = (await client.get(f"{API}/history", headers=tg(viewer.telegram_id))).json()
        assert names(everything) == {"Music", "Dance"}
        assert {"music", "dance"} <= {group["slug"] for group in everything["groups"]}

    dance = await group_id(session, "dance")
    filtered = (await client.get(f"{API}/history?group_id={dance}", headers=tg(main_admin.telegram_id))).json()
    assert names(filtered) == {"Dance"}
    # A group admin asking for a group they do not run gets nothing rather than someone else's rows.
    foreign = (await client.get(f"{API}/history?group_id={dance}", headers=tg(group_admin.telegram_id))).json()
    assert foreign["items"] == []


async def test_history_pages_through_a_cursor_without_overlap(client, session, make_user, make_group_admin):
    main_admin, _, _, _ = await staff(make_user, make_group_admin)
    created = await add_changes(session, "music", 5, "Music")
    headers = tg(main_admin.telegram_id)

    first = (await client.get(f"{API}/history?limit=2", headers=headers)).json()
    assert [item["id"] for item in first["items"]] == [created[4].id, created[3].id]
    assert first["next_before_id"] == created[3].id
    second = (await client.get(f"{API}/history?limit=2&before_id={first['next_before_id']}", headers=headers)).json()
    assert [item["id"] for item in second["items"]] == [created[2].id, created[1].id]
    last = (await client.get(f"{API}/history?limit=2&before_id={second['next_before_id']}", headers=headers)).json()
    assert [item["id"] for item in last["items"]] == [created[0].id]
    assert last["next_before_id"] is None

    exact = (await client.get(f"{API}/history?limit=5", headers=headers)).json()
    assert len(exact["items"]) == 5 and exact["next_before_id"] is None
    assert (await client.get(f"{API}/history?limit=51", headers=headers)).status_code == 422


async def test_history_times_are_shown_in_club_time(client, session, make_user, make_group_admin):
    main_admin, _, _, _ = await staff(make_user, make_group_admin)
    await add_changes(session, "music", 1, "Music")  # stored as 2026-01-01 20:00 UTC
    item = (await client.get(f"{API}/history", headers=tg(main_admin.telegram_id))).json()["items"][0]
    expected = datetime(2026, 1, 1, 20, 0, tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone))
    assert item["created_local"] == expected.strftime("%Y-%m-%dT%H:%M")
    assert item["created_at"].startswith("2026-01-01T20:00")
