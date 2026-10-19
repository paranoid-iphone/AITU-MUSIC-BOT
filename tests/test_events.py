"""Concerts: who may create and read them, the lists a member sees, personal history, and the internal HTTP routes."""
from datetime import date

import pytest
from sqlalchemy import select

from app.db import models
from app.services import events
from app.services.errors import ForbiddenError, NotFoundError
from tests.test_api import AUTH, client  # noqa: F401  (`client` is a fixture)

TODAY = date(2026, 10, 14)


async def _event(session, admin, **data):
    payload = {"title": "Concert", "event_date": date(2026, 11, 1), "submission_deadline": None}
    payload.update(data)
    return await events.create_event(session, admin.telegram_id, **payload)


async def _band_with_member(session, make_user, make_band, name="Rockers"):
    owner = await make_user()
    band = await make_band(owner, name)
    member_id = (await session.execute(select(models.BandMember.id).where(models.BandMember.band_id == band.id))).scalar_one()
    return owner, band, member_id


async def _audit_actions(session) -> list[str]:
    return list((await session.execute(select(models.AuditLog.action).order_by(models.AuditLog.id))).scalars())


# --- creating and updating -------------------------------------------------------------------------------------------


async def test_only_an_approved_admin_creates_a_concert_and_it_is_journalled(session, make_user):
    admin = await make_user(role="admin")
    with pytest.raises(ForbiddenError):
        await _event(session, await make_user())
    with pytest.raises(ForbiddenError):
        await _event(session, await make_user(role="admin", status="pending"))
    with pytest.raises(NotFoundError):
        await events.create_event(session, 424242, title="x", event_date=TODAY)

    event = await _event(session, admin, title="Autumn", location="Hall")

    assert (event.title, event.location, event.status, event.created_by_id) == ("Autumn", "Hall", "open", admin.id)
    assert await _audit_actions(session) == ["event.create"]


async def test_updating_a_concert_is_for_admins_and_changes_only_what_was_sent(session, make_user):
    admin = await make_user(role="admin")
    event = await _event(session, admin, title="Autumn", location="Hall")

    with pytest.raises(ForbiddenError):
        await events.update_event(session, event.id, (await make_user()).telegram_id, title="Hijacked")
    with pytest.raises(NotFoundError):
        await events.update_event(session, 9999, admin.telegram_id, title="Ghost")

    updated = await events.update_event(session, event.id, admin.telegram_id, title="Winter", status="closed")

    assert (updated.title, updated.location, updated.status) == ("Winter", "Hall", "closed")
    assert await _audit_actions(session) == ["event.create", "event.update"]


async def test_concerts_are_listed_newest_first(session, make_user):
    admin = await make_user(role="admin")
    await _event(session, admin, title="Old", event_date=date(2026, 1, 1))
    await _event(session, admin, title="New", event_date=date(2026, 12, 1))
    await _event(session, admin, title="Middle", event_date=date(2026, 6, 1))

    assert [event.title for event in await events.list_events(session)] == ["New", "Middle", "Old"]


# --- applications ----------------------------------------------------------------------------------------------------


async def test_admins_read_the_applications_of_a_concert_with_the_members(session, make_user, make_band):
    admin = await make_user(role="admin")
    owner, band, member_id = await _band_with_member(session, make_user, make_band)
    event = await _event(session, admin)
    await events.submit_application(session, event.id, owner.telegram_id, band.id, "Song", [member_id])

    with pytest.raises(ForbiddenError):
        await events.list_event_applications(session, owner.telegram_id, event.id)

    rows = await events.list_event_applications(session, admin.telegram_id, event.id)

    assert [(row["band_name"], row["song_title"]) for row in rows] == [("Rockers", "Song")]
    assert [member["user"]["telegram_id"] for member in rows[0]["members"]] == [owner.telegram_id]
    assert await events.list_event_applications(session, admin.telegram_id, 9999) == []


async def test_a_band_owner_sees_and_opens_only_the_applications_of_their_own_bands(session, make_user, make_band):
    admin = await make_user(role="admin")
    owner, band, member_id = await _band_with_member(session, make_user, make_band, "Rockers")
    other_owner, other_band, other_member = await _band_with_member(session, make_user, make_band, "Rivals")
    event = await _event(session, admin)
    mine = await events.submit_application(session, event.id, owner.telegram_id, band.id, "Mine", [member_id])
    await events.submit_application(session, event.id, other_owner.telegram_id, other_band.id, "Theirs", [other_member])

    rows = await events.list_my_applications(session, owner.telegram_id)
    detail = await events.get_application_detail(session, owner.telegram_id, mine.id)

    assert [(row["band_name"], row["song_title"], row["event_title"]) for row in rows] == [("Rockers", "Mine", "Concert")]
    assert detail["id"] == mine.id and detail["event_date"] == "2026-11-01"
    with pytest.raises(ForbiddenError):
        await events.get_application_detail(session, other_owner.telegram_id, mine.id)
    with pytest.raises(ForbiddenError):
        await events.get_application_detail(session, admin.telegram_id, mine.id)  # admins read the list, not the owner's card
    with pytest.raises(NotFoundError):
        await events.get_application_detail(session, owner.telegram_id, 9999)


async def test_an_owner_can_resubmit_and_edit_a_list_that_keeps_the_same_members(session, make_user, make_band):
    admin = await make_user(role="admin")
    owner, band, member_id = await _band_with_member(session, make_user, make_band)
    event = await _event(session, admin)
    first = await events.submit_application(session, event.id, owner.telegram_id, band.id, "Song", [member_id])

    again = await events.submit_application(session, event.id, owner.telegram_id, band.id, "New song", [member_id])
    changed = await events.update_application(session, first.id, owner.telegram_id, member_ids=[member_id], song_title="Third")

    assert again.id == first.id == changed.id and changed.song_title == "Third"
    rows = await events.list_event_applications(session, admin.telegram_id, event.id)
    assert [len(row["members"]) for row in rows] == [1]


# --- the history of a member -----------------------------------------------------------------------------------------


async def test_event_history_mixes_admin_lists_and_personal_entries_newest_first(session, make_user, make_band):
    admin = await make_user(role="admin")
    owner, band, member_id = await _band_with_member(session, make_user, make_band)
    spring = await _event(session, admin, title="Spring", event_date=date(2026, 4, 1))
    await events.submit_application(session, spring.id, owner.telegram_id, band.id, "Song", [member_id])
    await events.create_personal_event_history(
        session, owner.telegram_id, title="Open mic", event_date=date(2026, 9, 1), location=None, role_at_event="vocals"
    )
    await events.create_personal_event_history(
        session, owner.telegram_id, title="Jam", event_date=date(2026, 1, 1), location="Bar", role_at_event="bass"
    )

    rows = await events.list_user_event_history(session, owner.telegram_id)

    assert [(row["event_title"], row["source"]) for row in rows] == [("Open mic", "personal"), ("Spring", "admin"), ("Jam", "personal")]
    assert await events.list_user_event_history(session, (await make_user()).telegram_id) == []
    with pytest.raises(ForbiddenError):
        await events.list_user_event_history(session, (await make_user(status="pending")).telegram_id)


async def test_personal_entries_can_be_read_changed_and_removed_only_by_their_author(session, make_user):
    author = await make_user()
    stranger = await make_user()
    item = await events.create_personal_event_history(
        session, author.telegram_id, title="Jam", event_date=date(2026, 1, 1), location="Bar", role_at_event="bass"
    )

    assert (await events.get_personal_event_history(session, author.telegram_id, item.id))["event_title"] == "Jam"
    for action in (
        events.get_personal_event_history(session, stranger.telegram_id, item.id),
        events.update_personal_event_history(session, stranger.telegram_id, item.id, title="Mine now"),
        events.delete_personal_event_history(session, stranger.telegram_id, item.id),
    ):
        with pytest.raises(NotFoundError):
            await action

    changed = await events.update_personal_event_history(session, author.telegram_id, item.id, role_at_event="drums")
    assert (changed.title, changed.role_at_event) == ("Jam", "drums")
    deleted = await events.delete_personal_event_history(session, author.telegram_id, item.id)
    assert deleted["event_title"] == "Jam"
    with pytest.raises(NotFoundError):
        await events.get_personal_event_history(session, author.telegram_id, item.id)
    assert await _audit_actions(session) == [
        "event.history.personal.create",
        "event.history.personal.update",
        "event.history.personal.delete",
    ]


# --- the internal HTTP routes ----------------------------------------------------------------------------------------


async def test_event_routes_need_the_internal_token_and_validate_the_body(client, make_user):
    admin = await make_user(role="admin")
    body = {"telegram_id": admin.telegram_id, "title": "Autumn", "event_date": "2026-11-01"}

    assert (await client.post("/events", json=body)).status_code == 401
    assert (await client.post("/events", json={**body, "title": ""}, headers=AUTH)).status_code == 422
    assert (await client.patch("/events/1", json={"telegram_id": admin.telegram_id, "status": "archived"}, headers=AUTH)).status_code == 422

    created = await client.post("/events", json=body, headers=AUTH)
    assert created.status_code == 200 and created.json()["title"] == "Autumn"
    listed = await client.get("/events", headers=AUTH)
    assert [item["title"] for item in listed.json()] == ["Autumn"]


async def test_event_routes_turn_service_errors_into_status_codes(client, make_user):
    admin = await make_user(role="admin")
    member = await make_user()
    event = (await client.post("/events", json={"telegram_id": admin.telegram_id, "title": "Autumn", "event_date": "2026-11-01"}, headers=AUTH)).json()

    forbidden = await client.patch(f"/events/{event['id']}", json={"telegram_id": member.telegram_id, "title": "x"}, headers=AUTH)
    missing = await client.patch("/events/9999", json={"telegram_id": admin.telegram_id, "title": "x"}, headers=AUTH)
    applications = await client.get(f"/events/{event['id']}/applications", params={"telegram_id": member.telegram_id}, headers=AUTH)
    export = await client.get(f"/events/{event['id']}/export.xlsx", params={"telegram_id": admin.telegram_id}, headers=AUTH)

    assert (forbidden.status_code, missing.status_code, applications.status_code) == (403, 404, 403)
    assert export.status_code == 200 and export.headers["content-type"].startswith("application/vnd.openxmlformats")


async def test_personal_history_round_trips_over_http(client, make_user):
    user = await make_user()
    body = {"telegram_id": user.telegram_id, "title": "Jam", "event_date": "2026-01-01", "role_at_event": "bass"}

    created = (await client.post("/events/history/personal", json=body, headers=AUTH)).json()
    item = f"/events/history/personal/{created['id']}"
    detail = await client.get(item, params={"telegram_id": user.telegram_id}, headers=AUTH)
    history = await client.get("/events/history", params={"telegram_id": user.telegram_id}, headers=AUTH)
    patched = await client.patch(item, json={"telegram_id": user.telegram_id, "role_at_event": "drums"}, headers=AUTH)
    removed = await client.delete(item, params={"telegram_id": user.telegram_id}, headers=AUTH)
    gone = await client.get(item, params={"telegram_id": user.telegram_id}, headers=AUTH)

    assert detail.json()["event_title"] == "Jam"
    assert [row["event_title"] for row in history.json()] == ["Jam"]
    assert patched.json()["role_at_event"] == "drums"
    assert removed.status_code == 200 and gone.status_code == 404
