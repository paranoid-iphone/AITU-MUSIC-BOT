import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.db import models
from app.services import permissions, roles
from app.services.errors import ConflictError, ForbiddenError, NotFoundError


async def _group_id(session, slug: str) -> int:
    return (await session.execute(select(models.Group.id).where(models.Group.slug == slug))).scalar_one()


async def _role_id(session, name: str) -> int:
    return (await session.execute(select(models.Role.id).where(models.Role.name == name))).scalar_one()


async def _admin(make_user, grant):
    user = await make_user()
    await grant(user, "Админ")
    return user


async def _manager(session, make_user, grant):
    """Holds `roles.manage` through a custom role, but is not an admin."""
    session.add(models.Role(name="Менеджер ролей", permissions=[models.RolePermission(permission="roles.manage")]))
    await session.commit()
    user = await make_user()
    await grant(user, "Менеджер ролей")
    return user


async def _actions(session, action: str) -> list[models.AuditLog]:
    result = await session.execute(select(models.AuditLog).where(models.AuditLog.action == action))
    return list(result.scalars())


async def test_create_role_stores_permissions_and_logs(session, make_user, grant):
    admin = await _admin(make_user, grant)

    role = await roles.create_role(session, admin.telegram_id, "  Модератор ", ["users.moderate", "bands.manage"])

    assert role.name == "Модератор"
    assert sorted(item.permission for item in role.permissions) == ["bands.manage", "users.moderate"]
    assert not role.is_system
    [entry] = await _actions(session, "role.create")
    assert "Модератор" in entry.description and entry.actor_id == admin.id


@pytest.mark.parametrize("name", ["", "   ", "x" * 61])
async def test_create_role_rejects_bad_names(session, make_user, grant, name):
    """Review Focus 4."""
    admin = await _admin(make_user, grant)
    with pytest.raises(ConflictError, match="Role name must be 1-60 characters"):
        await roles.create_role(session, admin.telegram_id, name, [])


async def test_create_role_rejects_a_taken_name_in_any_case(session, make_user, grant):
    admin = await _admin(make_user, grant)
    await roles.create_role(session, admin.telegram_id, "Модератор", [])
    for taken in ("Модератор", "МОДЕРАТОР", "админ"):
        with pytest.raises(ConflictError, match="A role with this name already exists"):
            await roles.create_role(session, admin.telegram_id, taken, [])


async def test_create_role_rejects_unknown_permission_keys(session, make_user, grant):
    admin = await _admin(make_user, grant)
    with pytest.raises(ConflictError, match="Unknown permission"):
        await roles.create_role(session, admin.telegram_id, "Странная", ["users.moderate", "everything.allowed"])
    assert (await session.execute(select(func.count()).select_from(models.Role).where(models.Role.name == "Странная"))).scalar_one() == 0


async def test_only_roles_manage_holders_may_use_the_service(session, make_user, grant):
    plain = await make_user()
    group_admin = await make_user()
    await grant(group_admin, "Админ группы")
    for user in (plain, group_admin):
        with pytest.raises(ForbiddenError):
            await roles.create_role(session, user.telegram_id, "Своя", [])
        with pytest.raises(ForbiddenError):
            await roles.list_roles(session, user.telegram_id)

    manager = await _manager(session, make_user, grant)
    assert (await roles.create_role(session, manager.telegram_id, "Своя", [])).name == "Своя"


async def test_update_role_renames_and_replaces_permissions(session, make_user, grant):
    admin = await _admin(make_user, grant)
    role = await roles.create_role(session, admin.telegram_id, "Модератор", ["users.moderate"])

    updated = await roles.update_role(session, admin.telegram_id, role.id, "Старший модератор", ["bands.manage"])

    assert updated.name == "Старший модератор"
    assert [item.permission for item in updated.permissions] == ["bands.manage"]
    assert len(await _actions(session, "role.update")) == 1

    renamed_only = await roles.update_role(session, admin.telegram_id, role.id, "Модератор", None)
    assert [item.permission for item in renamed_only.permissions] == ["bands.manage"]
    same_name = await roles.update_role(session, admin.telegram_id, role.id, "модератор", None)  # own name, other case
    assert same_name.name == "модератор"

    other = await roles.create_role(session, admin.telegram_id, "Другая", [])
    with pytest.raises(ConflictError, match="A role with this name already exists"):
        await roles.update_role(session, admin.telegram_id, other.id, "МОДЕРАТОР", None)


async def test_update_role_keeps_the_permissions_that_stay(session, make_user, grant):
    """Editing a role in the Mini App resends the whole list: the keys that stay must not be inserted twice."""
    admin = await _admin(make_user, grant)
    role = await roles.create_role(session, admin.telegram_id, "Модератор", ["users.moderate", "bands.manage"])

    updated = await roles.update_role(session, admin.telegram_id, role.id, None, ["users.moderate", "bookings.manage", "users.moderate"])

    assert sorted(item.permission for item in updated.permissions) == ["bookings.manage", "users.moderate"]
    stored = (await session.execute(select(models.RolePermission.permission).where(models.RolePermission.role_id == role.id))).scalars()
    assert sorted(stored) == ["bookings.manage", "users.moderate"]


async def test_system_role_cannot_be_changed_or_deleted(session, make_user, grant):
    admin = await _admin(make_user, grant)
    admin_role = await _role_id(session, "Админ")
    with pytest.raises(ConflictError, match="System role cannot be changed"):
        await roles.update_role(session, admin.telegram_id, admin_role, "Другое", None)
    with pytest.raises(ConflictError, match="System role cannot be changed"):
        await roles.delete_role(session, admin.telegram_id, admin_role)


async def test_unknown_role_is_not_found(session, make_user, grant):
    admin = await _admin(make_user, grant)
    with pytest.raises(NotFoundError, match="Role not found"):
        await roles.update_role(session, admin.telegram_id, 9999, "x", None)
    with pytest.raises(NotFoundError, match="Role not found"):
        await roles.delete_role(session, admin.telegram_id, 9999)


async def test_delete_role_removes_assignments(session, make_user, grant):
    admin = await _admin(make_user, grant)
    first, second = await make_user(), await make_user()
    await grant(first, "ДСВР")
    await grant(second, "ДСВР", "music")

    removed = await roles.delete_role(session, admin.telegram_id, await _role_id(session, "ДСВР"))

    assert removed == 2
    assert (await session.execute(select(models.Role.id).where(models.Role.name == "ДСВР"))).first() is None
    assert (await session.execute(select(func.count()).select_from(models.RoleAssignment))).scalar_one() == 1  # the admin's
    assert not await permissions.has_permission(session, first, "bookings.priority")
    assert len(await _actions(session, "role.delete")) == 1


async def test_actor_may_delete_a_role_they_hold(session, make_user, grant):
    """Review Focus 5: the delete goes through, the rights are gone from the next check."""
    manager = await _manager(session, make_user, grant)

    removed = await roles.delete_role(session, manager.telegram_id, await _role_id(session, "Менеджер ролей"))

    assert removed == 1
    assert not await permissions.has_permission(session, manager, "roles.manage")


async def test_assign_role_for_the_club_and_for_one_group(session, make_user, grant):
    admin = await _admin(make_user, grant)
    dsvr, group_admin = await make_user(), await make_user()
    music = await _group_id(session, "music")

    club_wide = await roles.assign_role(session, admin.telegram_id, str(dsvr.telegram_id), await _role_id(session, "ДСВР"), None)
    scoped = await roles.assign_role(
        session, admin.telegram_id, f"@{group_admin.telegram_username}", await _role_id(session, "Админ группы"), music
    )

    assert club_wide.group_id is None and scoped.group_id == music
    assert await permissions.has_permission(session, dsvr, "bookings.priority")
    assert await permissions.has_permission(session, group_admin, "users.moderate", music)
    assert not await permissions.has_permission(session, group_admin, "users.moderate", await _group_id(session, "dance"))
    assert len(await _actions(session, "role.assign")) == 2


async def test_duplicate_assignment_is_refused(session, make_user, grant):
    """Review Focus 3: the database cannot enforce this for a club-wide assignment (NULL group), the service does."""
    admin = await _admin(make_user, grant)
    user = await make_user()
    role_id = await _role_id(session, "Админ группы")
    music, dance = await _group_id(session, "music"), await _group_id(session, "dance")

    await roles.assign_role(session, admin.telegram_id, str(user.telegram_id), role_id, None)
    await roles.assign_role(session, admin.telegram_id, str(user.telegram_id), role_id, music)
    with pytest.raises(ConflictError, match="Role is already assigned"):
        await roles.assign_role(session, admin.telegram_id, str(user.telegram_id), role_id, None)
    with pytest.raises(ConflictError, match="Role is already assigned"):
        await roles.assign_role(session, admin.telegram_id, str(user.telegram_id), role_id, music)
    await roles.assign_role(session, admin.telegram_id, str(user.telegram_id), role_id, dance)  # another group is fine


async def test_assign_requires_an_approved_target(session, make_user, grant):
    admin = await _admin(make_user, grant)
    pending = await make_user(status="pending")
    with pytest.raises(ConflictError, match="User must be approved to receive a role"):
        await roles.assign_role(session, admin.telegram_id, str(pending.telegram_id), await _role_id(session, "ДСВР"), None)


async def test_assign_rejects_unknown_things(session, make_user, grant):
    admin = await _admin(make_user, grant)
    user = await make_user()
    role_id = await _role_id(session, "ДСВР")
    with pytest.raises(NotFoundError, match="Group not found"):
        await roles.assign_role(session, admin.telegram_id, str(user.telegram_id), role_id, 9999)
    with pytest.raises(NotFoundError, match="Role not found"):
        await roles.assign_role(session, admin.telegram_id, str(user.telegram_id), 9999, None)
    with pytest.raises(NotFoundError, match="User not found"):
        await roles.assign_role(session, admin.telegram_id, "424242", role_id, None)


async def test_admin_role_is_club_wide_and_only_admin_holders_assign_it(session, make_user, grant):
    admin = await _admin(make_user, grant)
    manager = await _manager(session, make_user, grant)
    target = await make_user()
    admin_role = await _role_id(session, "Админ")

    with pytest.raises(ConflictError, match="The admin role applies to the whole club"):
        await roles.assign_role(session, admin.telegram_id, str(target.telegram_id), admin_role, await _group_id(session, "music"))
    with pytest.raises(ForbiddenError, match="Only an admin can assign or revoke the admin role"):
        await roles.assign_role(session, manager.telegram_id, str(target.telegram_id), admin_role, None)

    await roles.assign_role(session, admin.telegram_id, str(target.telegram_id), admin_role, None)
    assert await permissions.holds_admin_role(session, target)


async def test_revoke_assignment_takes_the_rights_away(session, make_user, grant):
    admin = await _admin(make_user, grant)
    user = await make_user()
    assignment = await grant(user, "ДСВР")

    revoked = await roles.revoke_assignment(session, admin.telegram_id, assignment.id)

    assert revoked.id == user.id
    assert not await permissions.has_permission(session, user, "bookings.priority")
    assert len(await _actions(session, "role.revoke")) == 1
    with pytest.raises(NotFoundError, match="Assignment not found"):
        await roles.revoke_assignment(session, admin.telegram_id, assignment.id)


async def test_admin_cannot_revoke_their_own_admin_role(session, make_user, grant):
    admin = await make_user()
    assignment = await grant(admin, "Админ")
    other = await _admin(make_user, grant)
    with pytest.raises(ForbiddenError, match="You cannot remove your own admin role"):
        await roles.revoke_assignment(session, admin.telegram_id, assignment.id)
    # another admin may remove it, though
    await roles.revoke_assignment(session, other.telegram_id, assignment.id)


async def test_initial_admin_keeps_the_admin_role(session, make_user, grant, monkeypatch):
    founder = await make_user()
    assignment = await grant(founder, "Админ")
    other = await _admin(make_user, grant)
    monkeypatch.setattr(type(settings), "initial_admin_ids", property(lambda self: {founder.telegram_id}))
    with pytest.raises(ForbiddenError, match="Initial admin cannot be demoted"):
        await roles.revoke_assignment(session, other.telegram_id, assignment.id)


async def test_the_club_always_keeps_an_admin(session, make_user, grant):
    """Only an admin can revoke the admin role and nobody can revoke it from themselves, so one always remains."""
    first, second = await make_user(), await make_user()
    first_assignment = await grant(first, "Админ")
    second_assignment = await grant(second, "Админ")

    await roles.revoke_assignment(session, first.telegram_id, second_assignment.id)

    with pytest.raises(ForbiddenError):
        await roles.revoke_assignment(session, second.telegram_id, first_assignment.id)
    assert await permissions.holds_admin_role(session, first)


async def test_only_admin_holders_revoke_the_admin_role(session, make_user, grant):
    admin = await make_user()
    assignment = await grant(admin, "Админ")
    manager = await _manager(session, make_user, grant)
    with pytest.raises(ForbiddenError, match="Only an admin can assign or revoke the admin role"):
        await roles.revoke_assignment(session, manager.telegram_id, assignment.id)


async def test_assign_and_revoke_notify_the_target(session, make_user, grant, published):
    admin = await _admin(make_user, grant)
    user = await make_user()
    silent = await make_user(notifications_enabled=False)
    role_id = await _role_id(session, "ДСВР")

    assignment = await roles.assign_role(session, admin.telegram_id, str(user.telegram_id), role_id, None)
    await roles.assign_role(session, admin.telegram_id, str(silent.telegram_id), role_id, None)
    await roles.revoke_assignment(session, admin.telegram_id, assignment.id)

    notified = [(event["telegram_id"], event["text"]) for event in published if event.get("type") == "notify"]
    assert [telegram_id for telegram_id, _ in notified] == [user.telegram_id, user.telegram_id]
    assert "ДСВР" in notified[0][1] and "ДСВР" in notified[1][1]


async def test_list_roles_counts_holders(session, make_user, grant):
    admin = await _admin(make_user, grant)
    for _ in range(2):
        await grant(await make_user(), "ДСВР")

    listed = {item["name"]: item for item in await roles.list_roles(session, admin.telegram_id)}

    assert listed["ДСВР"]["holders"] == 2 and listed["ДСВР"]["is_system"] is False
    assert sorted(listed["ДСВР"]["permissions"]) == ["bookings.manage", "bookings.priority", "schedule.history"]
    assert listed["Админ"]["is_system"] is True and listed["Админ"]["holders"] == 1
    assert set(listed["Админ"]["permissions"]) == set(permissions.PERMISSIONS)


async def test_list_assignments(session, make_user, grant):
    admin = await _admin(make_user, grant)
    user = await make_user()
    await grant(user, "Админ группы", "dance")

    [row] = await roles.list_assignments(session, admin.telegram_id, await _role_id(session, "Админ группы"))

    assert row.user_id == user.id and row.group_id == await _group_id(session, "dance")
