"""Mini App role constructor API: roles, their permissions, who holds them, and what /me reports."""
import pytest
from sqlalchemy import select

from app.db import models
from app.services.permission_catalog import PERMISSIONS
from tests.test_webapp import client, tg  # noqa: F401  (`client` is a fixture)

API = "/webapp/api/admin"


async def _admin(make_user, grant):
    user = await make_user()
    await grant(user, "Админ")
    return user


async def _group_id(session, slug: str) -> int:
    return (await session.execute(select(models.Group.id).where(models.Group.slug == slug))).scalar_one()


async def test_role_screens_need_roles_manage(client, make_user, grant, role_holder):
    admin = await _admin(make_user, grant)
    outsiders = [await make_user(), await role_holder("Модерация", ["users.moderate"]), await role_holder("Настройки", ["settings.manage"])]
    manager = await role_holder("Менеджер ролей", ["roles.manage"])

    for user in outsiders:
        assert (await client.get(f"{API}/roles", headers=tg(user.telegram_id))).status_code == 403
        assert (await client.post(f"{API}/roles", json={"name": "Х", "permissions": []}, headers=tg(user.telegram_id))).status_code == 403
    assert (await client.get(f"{API}/roles")).status_code == 401
    assert (await client.get(f"{API}/roles", headers=tg(admin.telegram_id))).status_code == 200
    assert (await client.get(f"{API}/roles", headers=tg(manager.telegram_id))).status_code == 200


async def test_create_list_edit_and_delete_a_role(client, make_user, grant):
    admin = await _admin(make_user, grant)
    headers = tg(admin.telegram_id)

    created = await client.post(f"{API}/roles", json={"name": "Модератор", "permissions": ["users.moderate"]}, headers=headers)
    assert created.status_code == 200
    role = created.json()
    assert role == {"id": role["id"], "name": "Модератор", "is_system": False, "permissions": ["users.moderate"], "holders": 0}

    listing = (await client.get(f"{API}/roles", headers=headers)).json()
    assert {item["name"] for item in listing["roles"]} == {"Админ", "ДСВР", "Админ группы", "Модератор"}
    assert {item["key"]: item["scope"] for item in listing["catalog"]} == PERMISSIONS
    assert {item["slug"] for item in listing["groups"]} == {"music", "dance", "cheer"}  # the scope choices of the assign form

    edited = await client.patch(
        f"{API}/roles/{role['id']}", json={"name": "Старший модератор", "permissions": ["users.moderate", "bands.manage"]}, headers=headers
    )
    assert edited.status_code == 200
    assert edited.json()["name"] == "Старший модератор" and sorted(edited.json()["permissions"]) == ["bands.manage", "users.moderate"]

    holder = await make_user()
    assign = await client.post(
        f"{API}/role-assignments", json={"target": str(holder.telegram_id), "role_id": role["id"]}, headers=headers
    )
    assert assign.status_code == 200

    deleted = await client.delete(f"{API}/roles/{role['id']}", headers=headers)
    assert deleted.status_code == 200 and deleted.json() == {"removed_assignments": 1}
    assert role["id"] not in {item["id"] for item in (await client.get(f"{API}/roles", headers=headers)).json()["roles"]}


async def test_role_changes_show_up_in_the_action_journal(client, make_user, grant):
    admin = await _admin(make_user, grant)
    headers = tg(admin.telegram_id)
    await client.post(f"{API}/roles", json={"name": "Модератор", "permissions": []}, headers=headers)

    journal = await client.get(f"{API}/actions?category=role", headers=headers)

    assert journal.status_code == 200
    assert [item["action"] for item in journal.json()["items"]] == ["role.create"]


async def test_bad_input_comes_back_with_a_code(client, session, make_user, grant):
    admin = await _admin(make_user, grant)
    headers = tg(admin.telegram_id)
    admin_role_id = (await session.execute(select(models.Role.id).where(models.Role.is_system.is_(True)))).scalar_one()

    blank = await client.post(f"{API}/roles", json={"name": "  ", "permissions": []}, headers=headers)
    assert (blank.status_code, blank.json()["code"]) == (409, "role_name_invalid")
    unknown = await client.post(f"{API}/roles", json={"name": "Х", "permissions": ["nope"]}, headers=headers)
    assert (unknown.status_code, unknown.json()["code"]) == (409, "unknown_permission")
    taken = await client.post(f"{API}/roles", json={"name": "дсвр", "permissions": []}, headers=headers)
    assert (taken.status_code, taken.json()["code"]) == (409, "role_name_taken")
    system = await client.patch(f"{API}/roles/{admin_role_id}", json={"name": "Другое"}, headers=headers)
    assert (system.status_code, system.json()["code"]) == (409, "system_role")
    missing = await client.delete(f"{API}/roles/9999", headers=headers)
    assert (missing.status_code, missing.json()["code"]) == (404, "role_not_found")


async def test_assign_list_and_revoke_over_http(client, session, make_user, grant):
    admin = await _admin(make_user, grant)
    headers = tg(admin.telegram_id)
    user = await make_user(group_slug="music")
    dance = await _group_id(session, "dance")
    role_id = (await session.execute(select(models.Role.id).where(models.Role.name == "Админ группы"))).scalar_one()

    assigned = await client.post(
        f"{API}/role-assignments", json={"target": f"@{user.telegram_username}", "role_id": role_id, "group_id": dance}, headers=headers
    )
    assert assigned.status_code == 200
    body = assigned.json()
    assert body["role_id"] == role_id and body["group"]["slug"] == "dance" and body["person"]["telegram_id"] == user.telegram_id

    holders = (await client.get(f"{API}/roles/{role_id}/assignments", headers=headers)).json()
    assert [(item["id"], item["person"]["telegram_id"], item["group"]["slug"]) for item in holders] == [(body["id"], user.telegram_id, "dance")]

    duplicate = await client.post(
        f"{API}/role-assignments", json={"target": str(user.telegram_id), "role_id": role_id, "group_id": dance}, headers=headers
    )
    assert (duplicate.status_code, duplicate.json()["code"]) == (409, "role_already_assigned")

    revoked = await client.delete(f"{API}/role-assignments/{body['id']}", headers=headers)
    assert revoked.status_code == 200 and revoked.json()["telegram_id"] == user.telegram_id
    assert (await client.get(f"{API}/roles/{role_id}/assignments", headers=headers)).json() == []
    again = await client.delete(f"{API}/role-assignments/{body['id']}", headers=headers)
    assert (again.status_code, again.json()["code"]) == (404, "assignment_not_found")


async def test_me_reports_roles_and_permissions(client, session, make_user, grant):
    music = await _group_id(session, "music")
    user = await make_user()
    await grant(user, "Админ группы", "music")
    await grant(user, "ДСВР")
    plain = await make_user()

    me = (await client.get("/webapp/api/me", headers=tg(user.telegram_id))).json()
    assert sorted((item["name"], (item["group"] or {}).get("slug")) for item in me["roles"]) == [("Админ группы", "music"), ("ДСВР", None)]
    assert me["permissions"]["users.moderate"] == {"all": False, "group_ids": [music]}
    assert me["permissions"]["bookings.priority"] == {"all": True, "group_ids": []}
    # DSVR holds `bookings.manage` for the club and a group admin for one group: the club-wide grant wins
    assert me["permissions"]["bookings.manage"] == {"all": True, "group_ids": []}
    assert "settings.manage" not in me["permissions"]

    nothing = (await client.get("/webapp/api/me", headers=tg(plain.telegram_id))).json()
    assert nothing["roles"] == [] and nothing["permissions"] == {}


async def test_the_person_card_lists_roles(client, session, make_user, grant):
    admin = await _admin(make_user, grant)
    user = await make_user()
    await grant(user, "Админ группы", "cheer")
    plain = await make_user()

    card = (await client.get(f"{API}/users/{user.telegram_id}", headers=tg(admin.telegram_id))).json()
    assert [(item["name"], item["group"]["slug"]) for item in card["roles"]] == [("Админ группы", "cheer")]
    assert isinstance(card["roles"][0]["assignment_id"], int)
    assert (await client.get(f"{API}/users/{plain.telegram_id}", headers=tg(admin.telegram_id))).json()["roles"] == []
