from datetime import date, time

import pytest

from app.services import admin as admin_service
from app.services import audit, events
from app.services.errors import ForbiddenError
from tests.test_webapp import client, tg  # noqa: F401  (`client` is a fixture)

API = "/webapp/api/admin"
SETTINGS, USERS, HISTORY, ACTIONS = f"{API}/settings", f"{API}/users", f"{API}/history", f"{API}/actions"


async def test_settings_manage_holder_changes_the_settings_others_are_refused(session, role_holder):
    holder = await role_holder("Настройки", ["settings.manage"])
    outsider = await role_holder("Только брони", ["bookings.manage"])

    assert "days" in await admin_service.get_settings(session, holder.telegram_id)
    window = await admin_service.update_booking_window(session, holder.telegram_id, time(9), time(20))
    assert window["start_time"] == "09:00"
    assert (await admin_service.get_personal_bookings_enabled(session, holder.telegram_id, require_admin_access=True))["personal_bookings_enabled"]

    with pytest.raises(ForbiddenError, match="Admin access required"):
        await admin_service.get_settings(session, outsider.telegram_id)
    with pytest.raises(ForbiddenError):
        await admin_service.update_booking_window(session, outsider.telegram_id, time(9), time(20))
    with pytest.raises(ForbiddenError):
        await admin_service.get_personal_bookings_enabled(session, outsider.telegram_id, require_admin_access=True)


async def test_audit_view_opens_the_journal(session, role_holder):
    holder = await role_holder("Журнал", ["audit.view"])
    outsider = await role_holder("Только брони", ["bookings.manage"])

    assert isinstance(await audit.list_actions(session, holder.telegram_id), list)
    with pytest.raises(ForbiddenError):
        await audit.list_actions(session, outsider.telegram_id)


async def test_events_manage_runs_the_concerts(session, role_holder):
    holder = await role_holder("Концерты", ["events.manage"])
    outsider = await role_holder("Только брони", ["bookings.manage"])
    payload = {"title": "Concert", "event_date": date(2026, 11, 1), "submission_deadline": None}

    event = await events.create_event(session, holder.telegram_id, **payload)
    updated = await events.update_event(session, event.id, holder.telegram_id, title="Autumn")
    assert updated.title == "Autumn"
    assert await events.list_event_applications(session, holder.telegram_id, event.id) == []

    with pytest.raises(ForbiddenError):
        await events.create_event(session, outsider.telegram_id, **payload)
    with pytest.raises(ForbiddenError):
        await events.list_event_applications(session, outsider.telegram_id, event.id)


async def test_custom_role_opens_only_its_endpoints(client, role_holder):
    holders = {
        SETTINGS: await role_holder("Настройки", ["settings.manage"]),
        USERS: await role_holder("Модерация", ["users.moderate"], "dance"),
        HISTORY: await role_holder("История", ["schedule.history"]),
        ACTIONS: await role_holder("Журнал", ["audit.view"]),
    }
    for allowed, user in holders.items():
        for path in holders:
            response = await client.get(path, headers=tg(user.telegram_id))
            assert response.status_code == (200 if path == allowed else 403), (allowed, path)


async def test_settings_writes_follow_the_permission(client, role_holder):
    holder = await role_holder("Настройки", ["settings.manage"])
    moderator = await role_holder("Модерация", ["users.moderate"])

    assert (await client.put(f"{SETTINGS}/personal", json={"enabled": False}, headers=tg(holder.telegram_id))).status_code == 200
    assert (await client.put(f"{SETTINGS}/personal", json={"enabled": True}, headers=tg(moderator.telegram_id))).status_code == 403


async def test_history_lists_only_the_permitted_groups(client, role_holder):
    scoped = await role_holder("История Денс", ["schedule.history"], "dance")
    club_wide = await role_holder("История", ["schedule.history"])

    scoped_groups = (await client.get(HISTORY, headers=tg(scoped.telegram_id))).json()["groups"]
    all_groups = (await client.get(HISTORY, headers=tg(club_wide.telegram_id))).json()["groups"]

    assert [group["slug"] for group in scoped_groups] == ["dance"]
    assert {group["slug"] for group in all_groups} == {"music", "dance", "cheer"}
