import pytest

from app.services import bands, groups, permissions, roles, users
from app.services.errors import ForbiddenError


async def _admin(make_user, grant):
    user = await make_user()
    await grant(user, "Админ")
    return user


async def _role_holder(session, make_user, admin, name: str, permission_keys: list[str], group_slug: str | None = None):
    """A user holding a freshly made custom role, for the whole club or for one group."""
    from sqlalchemy import select

    from app.db import models

    role = await roles.create_role(session, admin.telegram_id, name, permission_keys)
    group_id = None
    if group_slug is not None:
        group_id = (await session.execute(select(models.Group.id).where(models.Group.slug == group_slug))).scalar_one()
    holder = await make_user()
    await roles.assign_role(session, admin.telegram_id, str(holder.telegram_id), role.id, group_id)
    return holder


async def test_custom_role_moderates_only_its_group(session, make_user, grant):
    admin = await _admin(make_user, grant)
    moderator = await _role_holder(session, make_user, admin, "Модератор Денс", ["users.moderate"], "dance")
    dancer = await make_user(status="pending", group_slug="dance")
    musician = await make_user(status="pending", group_slug="music")

    approved = await users.approve_user(session, moderator.telegram_id, str(dancer.telegram_id))

    assert approved.status == "approved"
    with pytest.raises(ForbiddenError):
        await users.approve_user(session, moderator.telegram_id, str(musician.telegram_id))


async def test_club_wide_moderator_reaches_every_group(session, make_user, grant):
    admin = await _admin(make_user, grant)
    moderator = await _role_holder(session, make_user, admin, "Модератор", ["users.moderate"])
    musician = await make_user(status="pending", group_slug="music")
    stateless = await make_user(status="pending", group_slug=None)

    assert (await users.approve_user(session, moderator.telegram_id, str(musician.telegram_id))).status == "approved"
    assert (await users.reject_user(session, moderator.telegram_id, str(stateless.telegram_id), "нет")).status == "rejected"


async def test_a_role_without_the_permission_cannot_moderate(session, make_user, grant):
    admin = await _admin(make_user, grant)
    other = await _role_holder(session, make_user, admin, "Только брони", ["bookings.manage"])
    pending = await make_user(status="pending")
    with pytest.raises(ForbiddenError):
        await users.approve_user(session, other.telegram_id, str(pending.telegram_id))


async def test_moderator_cannot_touch_people_with_roles(session, make_user, grant):
    admin = await _admin(make_user, grant)
    moderator = await _role_holder(session, make_user, admin, "Модератор", ["users.moderate"])
    staff = await make_user()
    await grant(staff, "ДСВР")

    with pytest.raises(ForbiddenError):
        await users.ban_user(session, moderator.telegram_id, str(staff.telegram_id), "x")

    assert (await users.ban_user(session, admin.telegram_id, str(staff.telegram_id), "x")).status == "banned"


async def test_only_the_admin_role_restores_banned_users(session, make_user, grant):
    admin = await _admin(make_user, grant)
    moderator = await _role_holder(session, make_user, admin, "Модератор", ["users.moderate"])
    banned = await make_user(status="banned")

    with pytest.raises(ForbiddenError, match="Only a main admin can restore"):
        await users.approve_user(session, moderator.telegram_id, str(banned.telegram_id))
    with pytest.raises(ForbiddenError, match="Only a main admin can change"):
        await users.reject_user(session, moderator.telegram_id, str(banned.telegram_id), "x")

    assert (await users.approve_user(session, admin.telegram_id, str(banned.telegram_id))).status == "approved"


async def test_scoped_user_list_follows_the_permission(session, make_user, grant):
    admin = await _admin(make_user, grant)
    club_wide = await _role_holder(session, make_user, admin, "Модератор", ["users.moderate"])
    scoped = await _role_holder(session, make_user, admin, "Модератор Денс", ["users.moderate"], "dance")
    nobody = await make_user()
    dancer = await make_user(status="pending", group_slug="dance")
    musician = await make_user(status="pending", group_slug="music")

    everyone, _ = await users.search_users(session, club_wide.telegram_id, "pending", None)
    only_dance, counts = await users.search_users(session, scoped.telegram_id, "pending", None)

    assert {u.telegram_id for u in everyone} == {dancer.telegram_id, musician.telegram_id}
    assert [u.telegram_id for u in only_dance] == [dancer.telegram_id]
    assert counts["pending"] == 1
    with pytest.raises(ForbiddenError):
        await users.search_users(session, nobody.telegram_id, None, None)


async def test_moderators_see_profiles_of_their_group_only(session, make_user, grant):
    admin = await _admin(make_user, grant)
    scoped = await _role_holder(session, make_user, admin, "Модератор Денс", ["users.moderate"], "dance")
    dancer = await make_user(group_slug="dance")
    musician = await make_user(group_slug="music")

    assert (await users.get_user_for_requester(session, dancer.telegram_id, scoped.telegram_id)).id == dancer.id
    with pytest.raises(ForbiddenError):
        await users.get_user_for_requester(session, musician.telegram_id, scoped.telegram_id)


async def test_bands_manage_scopes_the_band_list_and_band_access(session, make_user, make_band, grant):
    from sqlalchemy import select

    from app.db import models

    admin = await _admin(make_user, grant)
    dance_manager = await _role_holder(session, make_user, admin, "Коллективы Денс", ["bands.manage"], "dance")
    club_manager = await _role_holder(session, make_user, admin, "Коллективы", ["bands.manage"])
    nobody = await make_user()
    dance_id = (await session.execute(select(models.Group.id).where(models.Group.slug == "dance"))).scalar_one()
    music_id = (await session.execute(select(models.Group.id).where(models.Group.slug == "music"))).scalar_one()
    dance_band = await make_band(await make_user(), "Танцы", dance_id)
    music_band = await make_band(await make_user(), "Музыка", music_id)

    scoped = await bands.list_all_bands(session, dance_manager.telegram_id)
    everything = await bands.list_all_bands(session, club_manager.telegram_id)

    assert [b.id for b in scoped] == [dance_band.id]
    assert {b.id for b in everything} == {dance_band.id, music_band.id}
    assert await permissions.can_manage_band(session, dance_manager, dance_band)
    assert not await permissions.can_manage_band(session, dance_manager, music_band)
    assert await permissions.can_manage_band(session, club_manager, music_band)
    with pytest.raises(ForbiddenError):
        await bands.list_all_bands(session, nobody.telegram_id)


async def test_registration_reviewers_come_from_roles(session, make_user, grant):
    from sqlalchemy import select

    from app.db import models

    admin = await _admin(make_user, grant)
    club_wide = await _role_holder(session, make_user, admin, "Модератор", ["users.moderate"])
    music = await _role_holder(session, make_user, admin, "Модератор Мьюзик", ["users.moderate"], "music")
    await _role_holder(session, make_user, admin, "Модератор Денс", ["users.moderate"], "dance")
    await _role_holder(session, make_user, admin, "Только брони", ["bookings.manage"])
    music_id = (await session.execute(select(models.Group.id).where(models.Group.slug == "music"))).scalar_one()

    for_music = await groups.registration_reviewer_ids(session, music_id)
    without_group = await groups.registration_reviewer_ids(session, None)

    assert for_music == sorted({admin.telegram_id, club_wide.telegram_id, music.telegram_id})
    assert without_group == sorted({admin.telegram_id, club_wide.telegram_id})


async def test_an_unapproved_admin_is_not_waved_through(session, make_user):
    pending_admin = await make_user(role="admin", status="pending")
    with pytest.raises(ForbiddenError, match="User is not approved"):
        users.require_approved(pending_admin)
