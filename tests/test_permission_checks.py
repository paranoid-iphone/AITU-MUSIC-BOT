import pytest
from sqlalchemy import select

from app.db import models
from app.services import permissions
from app.services.errors import ForbiddenError
from app.services.permission_catalog import PERMISSIONS

CLUB_PERMISSIONS = [key for key, scope in PERMISSIONS.items() if scope == "club"]
GROUP_PERMISSIONS = [key for key, scope in PERMISSIONS.items() if scope == "group"]
DSVR_PERMISSIONS = {"bookings.priority", "bookings.manage", "schedule.history"}
GROUP_ADMIN_PERMISSIONS = {"users.moderate", "bands.manage", "bookings.manage", "schedule.history"}


async def _group_id(session, slug: str) -> int:
    return (await session.execute(select(models.Group.id).where(models.Group.slug == slug))).scalar_one()


async def _matrix(session, user) -> dict[tuple[str, str | None], bool]:
    """has_permission for every permission, for no group and for each seeded group."""
    result = {}
    for perm in PERMISSIONS:
        result[(perm, None)] = await permissions.has_permission(session, user, perm)
        for slug in ("music", "dance", "cheer"):
            result[(perm, slug)] = await permissions.has_permission(session, user, perm, await _group_id(session, slug))
    return result


async def test_admin_role_has_every_permission_everywhere(session, make_user, grant):
    user = await make_user()
    await grant(user, "Админ")
    assert all((await _matrix(session, user)).values())
    assert await permissions.holds_admin_role(session, user)


async def test_default_roles_match_legacy(session, make_user, grant):
    """Who can do what today: DSVR works club-wide, a group admin only inside their own group."""
    dsvr = await make_user()
    await grant(dsvr, "ДСВР")
    matrix = await _matrix(session, dsvr)
    for (perm, _group), allowed in matrix.items():
        assert allowed == (perm in DSVR_PERMISSIONS), perm

    group_admin = await make_user()
    await grant(group_admin, "Админ группы", "music")
    matrix = await _matrix(session, group_admin)
    for (perm, group), allowed in matrix.items():
        assert allowed == (perm in GROUP_ADMIN_PERMISSIONS and group == "music"), (perm, group)


async def test_club_only_permission_ignores_group_scoped_assignment(session, make_user, grant):
    """Review Focus 1: `audit.view` exists only for the whole club; a role scoped to a group must not leak it."""
    role = models.Role(name="Наблюдатель", permissions=[models.RolePermission(permission="audit.view")])
    session.add(role)
    await session.commit()
    user = await make_user()
    await grant(user, "Наблюдатель", "music")
    music = await _group_id(session, "music")
    assert not await permissions.has_permission(session, user, "audit.view")
    assert not await permissions.has_permission(session, user, "audit.view", music)
    assert await permissions.permitted_group_ids(session, user, "audit.view") == set()


@pytest.mark.parametrize("status", ["pending", "rejected", "banned", "deleted"])
async def test_unapproved_holder_has_nothing(session, make_user, grant, status):
    """Review Focus 2."""
    user = await make_user(status=status)
    await grant(user, "Админ")
    assert not any((await _matrix(session, user)).values())
    assert not await permissions.holds_admin_role(session, user)
    assert await permissions.permitted_group_ids(session, user, "users.moderate") == set()
    assert not await permissions.has_permission_anywhere(session, user, "users.moderate")


async def test_permitted_group_ids(session, make_user, grant):
    music = await _group_id(session, "music")
    club_wide = await make_user()
    await grant(club_wide, "Админ группы")
    assert await permissions.permitted_group_ids(session, club_wide, "users.moderate") is None

    scoped = await make_user()
    await grant(scoped, "Админ группы", "music")
    assert await permissions.permitted_group_ids(session, scoped, "users.moderate") == {music}
    assert await permissions.has_permission_anywhere(session, scoped, "users.moderate")

    nobody = await make_user()
    assert await permissions.permitted_group_ids(session, nobody, "users.moderate") == set()
    assert not await permissions.has_permission_anywhere(session, nobody, "users.moderate")


async def test_two_assignments_combine(session, make_user, grant):
    music, dance = await _group_id(session, "music"), await _group_id(session, "dance")
    user = await make_user()
    await grant(user, "Админ группы", "music")
    await grant(user, "Админ группы", "dance")
    assert await permissions.permitted_group_ids(session, user, "bands.manage") == {music, dance}


async def test_legacy_columns_still_grant(session, make_user):
    admin = await make_user(role="admin")
    assert await permissions.holds_admin_role(session, admin)
    assert all((await _matrix(session, admin)).values())

    dsvr = await make_user(role="dsvr")
    assert not await permissions.holds_admin_role(session, dsvr)
    assert await permissions.has_permission(session, dsvr, "bookings.priority")
    assert not await permissions.has_permission(session, dsvr, "users.moderate")

    music = await _group_id(session, "music")
    group_admin = await make_user()
    session.add(models.GroupAdmin(group_id=music, user_id=group_admin.id))
    await session.commit()
    assert await permissions.has_permission(session, group_admin, "users.moderate", music)
    assert not await permissions.has_permission(session, group_admin, "users.moderate", await _group_id(session, "dance"))


async def test_assignments_and_legacy_columns_agree(session, make_user, grant):
    """The bridge must give exactly what the default roles give."""
    music = await _group_id(session, "music")
    legacy_dsvr, new_dsvr = await make_user(role="dsvr"), await make_user()
    await grant(new_dsvr, "ДСВР")
    assert await _matrix(session, legacy_dsvr) == await _matrix(session, new_dsvr)

    legacy_admin, new_admin = await make_user(role="admin"), await make_user()
    await grant(new_admin, "Админ")
    assert await _matrix(session, legacy_admin) == await _matrix(session, new_admin)

    legacy_group, new_group = await make_user(), await make_user()
    session.add(models.GroupAdmin(group_id=music, user_id=legacy_group.id))
    await session.commit()
    await grant(new_group, "Админ группы", "music")
    assert await _matrix(session, legacy_group) == await _matrix(session, new_group)


async def test_has_any_role(session, make_user, grant):
    plain, assigned, legacy = await make_user(), await make_user(), await make_user(role="dsvr")
    await grant(assigned, "ДСВР", "music")
    assert not await permissions.has_any_role(session, plain)
    assert await permissions.has_any_role(session, assigned)
    assert await permissions.has_any_role(session, legacy)


async def test_holders_of_lists_club_and_group_holders_and_skips_unapproved(session, make_user, grant):
    music = await _group_id(session, "music")
    club = await make_user()
    await grant(club, "Админ группы")
    scoped_music = await make_user()
    await grant(scoped_music, "Админ группы", "music")
    scoped_dance = await make_user()
    await grant(scoped_dance, "Админ группы", "dance")
    banned = await make_user(status="banned")
    await grant(banned, "Админ")
    await make_user()  # no role

    ids = {user.id for user in await permissions.holders_of(session, "users.moderate", music)}
    assert ids == {club.id, scoped_music.id}
    club_wide = {user.id for user in await permissions.holders_of(session, "users.moderate")}
    assert club_wide == {club.id}


async def test_require_permission_raises_forbidden(session, make_user, grant):
    user = await make_user()
    with pytest.raises(ForbiddenError, match="Admin access required"):
        await permissions.require_permission(session, user, "settings.manage")
    await grant(user, "Админ")
    await permissions.require_permission(session, user, "settings.manage")
