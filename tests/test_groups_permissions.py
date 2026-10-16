import pytest
from sqlalchemy import select

from app.db import models
from app.services import bands, bookings, groups, users
from app.services.errors import ConflictError, ForbiddenError


async def _group_id(session, slug: str) -> int:
    return (await session.execute(select(models.Group.id).where(models.Group.slug == slug))).scalar_one()


async def test_group_admin_can_approve_only_own_group(session, make_user):
    main_admin = await make_user(role="admin")
    dance_admin = await make_user(group_slug="dance")
    await groups.add_group_admin(session, main_admin.telegram_id, await _group_id(session, "dance"), str(dance_admin.telegram_id))

    dancer = await make_user(status="pending", group_slug="dance")
    musician = await make_user(status="pending", group_slug="music")

    approved = await users.approve_user(session, dance_admin.telegram_id, str(dancer.telegram_id))
    assert approved.status == "approved"
    with pytest.raises(ForbiddenError):
        await users.approve_user(session, dance_admin.telegram_id, str(musician.telegram_id))


async def test_group_admin_lists_only_own_group_users(session, make_user):
    main_admin = await make_user(role="admin")
    cheer_admin = await make_user(group_slug="cheer")
    await groups.add_group_admin(session, main_admin.telegram_id, await _group_id(session, "cheer"), str(cheer_admin.telegram_id))
    cheerleader = await make_user(status="pending", group_slug="cheer")
    await make_user(status="pending", group_slug="music")

    visible = await users.list_users_by_status(session, cheer_admin.telegram_id, "pending")
    assert [u.telegram_id for u in visible] == [cheerleader.telegram_id]


async def test_regular_user_cannot_list_users(session, make_user):
    plain = await make_user()
    with pytest.raises(ForbiddenError):
        await users.list_users_by_status(session, plain.telegram_id)


async def test_only_main_admin_can_assign_group_admins(session, make_user):
    group_admin = await make_user()
    target = await make_user()
    with pytest.raises(ForbiddenError):
        await groups.add_group_admin(session, group_admin.telegram_id, await _group_id(session, "music"), str(target.telegram_id))


async def test_reviewers_are_main_admins_plus_group_admins(session, make_user):
    main_admin = await make_user(role="admin")
    dance_admin = await make_user(group_slug="dance")
    music_admin = await make_user(group_slug="music")
    await groups.add_group_admin(session, main_admin.telegram_id, await _group_id(session, "dance"), str(dance_admin.telegram_id))
    await groups.add_group_admin(session, main_admin.telegram_id, await _group_id(session, "music"), str(music_admin.telegram_id))

    ids = await groups.registration_reviewer_ids(session, await _group_id(session, "dance"))
    assert set(ids) == {main_admin.telegram_id, dance_admin.telegram_id}


async def test_band_defaults_to_creator_group_and_responsible(session, make_user):
    creator = await make_user(group_slug="dance")
    band = await bands.create_band(session, creator.telegram_id, "Dancers", "лидер")
    assert band.group_id == creator.group_id
    assert band.responsible_user_id == creator.id


async def test_owner_can_change_responsible_and_it_resets_on_removal(session, make_user, make_band):
    owner = await make_user()
    member_user = await make_user()
    band = await make_band(owner)
    await bands.join_band(session, member_user.telegram_id, band.invite_code, "гитара")
    detail = await bands.get_band_detail(session, band.id)
    member = next(m for m in detail.members if m.user_id == member_user.id)

    updated = await bands.update_band_responsible(session, band.id, owner.telegram_id, member.id)
    assert updated.responsible_user_id == member_user.id
    assert updated.responsible.id == member_user.id

    await bands.remove_band_member(session, band.id, member.id, owner.telegram_id)
    after = await bands.get_band_detail(session, band.id)
    assert after.responsible_user_id == owner.id


async def test_stranger_cannot_change_responsible(session, make_user, make_band):
    owner = await make_user()
    stranger = await make_user()
    band = await make_band(owner)
    detail = await bands.get_band_detail(session, band.id)
    with pytest.raises(ForbiddenError):
        await bands.update_band_responsible(session, band.id, stranger.telegram_id, detail.members[0].id)


async def test_group_admin_manages_bands_of_own_group_only(session, make_user, make_band):
    main_admin = await make_user(role="admin")
    dance_admin = await make_user(group_slug="dance")
    await groups.add_group_admin(session, main_admin.telegram_id, await _group_id(session, "dance"), str(dance_admin.telegram_id))
    dance_band = await make_band(await make_user(group_slug="dance"), "Dance", group_id=await _group_id(session, "dance"))
    music_band = await make_band(await make_user(group_slug="music"), "Music", group_id=await _group_id(session, "music"))

    visible = await bands.list_all_bands(session, dance_admin.telegram_id)
    assert [b.id for b in visible] == [dance_band.id]
    renamed = await bands.update_band_name(session, dance_band.id, dance_admin.telegram_id, "Renamed")
    assert renamed.name == "Renamed"
    with pytest.raises(ForbiddenError):
        await bands.update_band_name(session, music_band.id, dance_admin.telegram_id, "Hacked")


async def test_banned_admin_loses_access_and_role(session, make_user):
    main_admin = await make_user(role="admin")
    other_admin = await make_user(role="admin")
    await users.ban_user(session, main_admin.telegram_id, str(other_admin.telegram_id), "test")
    await session.refresh(other_admin)
    assert other_admin.role == "user"
    with pytest.raises(ForbiddenError):
        await users.list_admins(session, other_admin.telegram_id)


async def test_admin_cannot_ban_self(session, make_user):
    admin = await make_user(role="admin")
    with pytest.raises(ForbiddenError):
        await users.ban_user(session, admin.telegram_id, str(admin.telegram_id), "oops")


async def test_schedule_rows_carry_group_and_responsible(session, make_user, make_band):
    from datetime import timedelta

    from app.core.timezone import now_local

    owner = await make_user(group_slug="dance", last_name="Петрова", first_name="Мария")
    band = await make_band(owner, "Dancers", group_id=await _group_id(session, "dance"))
    start = (now_local() + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
    await bookings.create_booking(
        session, owner.telegram_id, band.id, start.date(), start.time(), (start + timedelta(hours=1)).time(), "self", "Song"
    )

    rows = await bookings.list_schedule(session)
    assert len(rows) == 1
    assert rows[0]["group_title"] == "Денс"
    assert rows[0]["responsible_name"] == "Петрова Мария"


async def test_dsvr_can_manage_any_booking_but_not_admin_actions(session, make_user, make_band):
    from datetime import timedelta

    from app.core.timezone import now_local

    owner = await make_user()
    dsvr = await make_user(role="dsvr", group_slug=None)
    band = await make_band(owner)
    start = (now_local() + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
    booking = await bookings.create_booking(
        session, owner.telegram_id, band.id, start.date(), start.time(), (start + timedelta(hours=1)).time(), "self", "Song"
    )

    moved = await bookings.update_booking(session, dsvr.telegram_id, booking.id, song_title="Changed by DSVR")
    assert moved.song_title == "Changed by DSVR"
    with pytest.raises(ForbiddenError):
        await users.list_admins(session, dsvr.telegram_id)
