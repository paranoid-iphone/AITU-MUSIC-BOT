from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.config import settings
from app.core.timezone import now_local
from app.db import models
from app.db.session import get_session
from app.main import app
from app.services import bands, bookings, users
from app.services.errors import ForbiddenError, NotFoundError

AUTH = {"X-Internal-Token": settings.internal_api_token}


@pytest_asyncio.fixture
async def client(session):
    async def _session_override():
        yield session

    app.dependency_overrides[get_session] = _session_override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def _group_id(session, slug: str) -> int:
    return (await session.execute(select(models.Group.id).where(models.Group.slug == slug))).scalar_one()


async def _make_group_admin(make_user, grant, slug: str):
    group_admin = await make_user(group_slug=slug)
    await grant(group_admin, "Админ группы", slug)
    return group_admin


async def _band_with_member(session, make_user, make_band, group_slug: str = "music"):
    owner = await make_user(group_slug=group_slug)
    member = await make_user(group_slug=group_slug)
    band = await make_band(owner, "Rockers", group_id=await _group_id(session, group_slug))
    await bands.join_band(session, member.telegram_id, band.invite_code, "гитара")
    return owner, member, band


# --- reading a band ---------------------------------------------------------------------------------------------


async def test_member_and_owner_can_read_band(session, make_user, make_band):
    owner, member, band = await _band_with_member(session, make_user, make_band)

    assert (await bands.get_band_for_requester(session, band.id, owner.telegram_id)).id == band.id
    assert (await bands.get_band_for_requester(session, band.id, member.telegram_id)).id == band.id


async def test_stranger_cannot_read_band(session, make_user, make_band):
    _, _, band = await _band_with_member(session, make_user, make_band)
    stranger = await make_user()

    with pytest.raises(ForbiddenError):
        await bands.get_band_for_requester(session, band.id, stranger.telegram_id)


async def test_unknown_requester_or_band_is_not_found(session, make_user, make_band):
    owner, _, band = await _band_with_member(session, make_user, make_band)

    with pytest.raises(NotFoundError):
        await bands.get_band_for_requester(session, band.id, 999_999_999)
    with pytest.raises(NotFoundError):
        await bands.get_band_for_requester(session, 999_999, owner.telegram_id)


async def test_unapproved_member_cannot_read_band(session, make_user, make_band):
    owner, member, band = await _band_with_member(session, make_user, make_band)
    member.status = "banned"
    await session.commit()

    with pytest.raises(ForbiddenError):
        await bands.get_band_for_requester(session, band.id, member.telegram_id)


async def test_admins_read_bands_in_their_scope_only(session, make_user, make_band, grant):
    main_admin = await make_user(role="admin")
    dance_admin = await _make_group_admin(make_user, grant, "dance")
    _, _, dance_band = await _band_with_member(session, make_user, make_band, "dance")
    _, _, music_band = await _band_with_member(session, make_user, make_band, "music")

    assert (await bands.get_band_for_requester(session, music_band.id, main_admin.telegram_id)).id == music_band.id
    assert (await bands.get_band_for_requester(session, dance_band.id, dance_admin.telegram_id)).id == dance_band.id
    with pytest.raises(ForbiddenError):
        await bands.get_band_for_requester(session, music_band.id, dance_admin.telegram_id)


# --- invite code visibility -------------------------------------------------------------------------------------


async def test_invite_code_visible_to_owner_responsible_and_admins_only(session, make_user, make_band, grant):
    main_admin = await make_user(role="admin")
    music_admin = await _make_group_admin(make_user, grant, "music")
    dance_admin = await _make_group_admin(make_user, grant, "dance")
    owner, member, band = await _band_with_member(session, make_user, make_band)
    responsible = await make_user()
    await bands.join_band(session, responsible.telegram_id, band.invite_code, "бас")
    await session.refresh(band, ["members"])  # the first join cached the collection on this long-lived session
    responsible_member = next(m for m in band.members if m.user_id == responsible.id)
    await bands.update_band_responsible(session, band.id, owner.telegram_id, responsible_member.id)

    for viewer in (owner, responsible, main_admin, music_admin):
        assert await bands.invite_code_visible_ids(session, viewer.telegram_id, [band]) == {band.id}, viewer.telegram_id
    for viewer in (member, dance_admin):
        assert await bands.invite_code_visible_ids(session, viewer.telegram_id, [band]) == set(), viewer.telegram_id


async def test_unapproved_owner_does_not_see_invite_code(session, make_user, make_band):
    owner, _, band = await _band_with_member(session, make_user, make_band)
    owner.status = "banned"
    await session.commit()

    assert await bands.invite_code_visible_ids(session, owner.telegram_id, [band]) == set()


async def test_http_get_band_hides_invite_code_from_plain_member(client, session, make_user, make_band):
    owner, member, band = await _band_with_member(session, make_user, make_band)

    as_owner = await client.get(f"/bands/{band.id}", params={"requester_telegram_id": owner.telegram_id}, headers=AUTH)
    as_member = await client.get(f"/bands/{band.id}", params={"requester_telegram_id": member.telegram_id}, headers=AUTH)

    assert as_owner.json()["invite_code"] == band.invite_code
    assert as_member.status_code == 200
    assert as_member.json()["invite_code"] is None


async def test_http_my_bands_hide_invite_code_from_plain_member(client, session, make_user, make_band):
    owner, member, band = await _band_with_member(session, make_user, make_band)

    mine = await client.get("/bands", params={"telegram_id": member.telegram_id}, headers=AUTH)
    owner_mine = await client.get("/bands", params={"telegram_id": owner.telegram_id}, headers=AUTH)

    assert [b["invite_code"] for b in mine.json()] == [None]
    assert [b["invite_code"] for b in owner_mine.json()] == [band.invite_code]


async def test_http_get_band_requires_requester_and_access(client, session, make_user, make_band):
    _, _, band = await _band_with_member(session, make_user, make_band)
    stranger = await make_user()

    assert (await client.get(f"/bands/{band.id}", headers=AUTH)).status_code == 422
    denied = await client.get(f"/bands/{band.id}", params={"requester_telegram_id": stranger.telegram_id}, headers=AUTH)
    assert denied.status_code == 403


# --- reading a user ---------------------------------------------------------------------------------------------


async def test_user_can_read_self_in_any_status(session, make_user):
    pending = await make_user(status="pending")

    assert (await users.get_user_for_requester(session, pending.telegram_id, pending.telegram_id)).id == pending.id


async def test_user_cannot_read_other_users(session, make_user):
    me = await make_user()
    other = await make_user()

    with pytest.raises(ForbiddenError):
        await users.get_user_for_requester(session, other.telegram_id, me.telegram_id)


async def test_admins_read_users_in_their_scope_only(session, make_user, grant):
    main_admin = await make_user(role="admin")
    dance_admin = await _make_group_admin(make_user, grant, "dance")
    dancer = await make_user(group_slug="dance")
    musician = await make_user(group_slug="music")

    assert (await users.get_user_for_requester(session, musician.telegram_id, main_admin.telegram_id)).id == musician.id
    assert (await users.get_user_for_requester(session, dancer.telegram_id, dance_admin.telegram_id)).id == dancer.id
    with pytest.raises(ForbiddenError):
        await users.get_user_for_requester(session, musician.telegram_id, dance_admin.telegram_id)


async def test_unknown_user_or_requester_is_not_found(session, make_user):
    me = await make_user()

    with pytest.raises(NotFoundError):
        await users.get_user_for_requester(session, 999_999_999, me.telegram_id)
    with pytest.raises(NotFoundError):
        await users.get_user_for_requester(session, me.telegram_id, 999_999_999)


async def test_http_get_user_defaults_nothing_and_enforces_access(client, session, make_user):
    me = await make_user()
    other = await make_user()

    assert (await client.get(f"/users/{me.telegram_id}", headers=AUTH)).status_code == 422
    own = await client.get(f"/users/{me.telegram_id}", params={"requester_telegram_id": me.telegram_id}, headers=AUTH)
    foreign = await client.get(f"/users/{other.telegram_id}", params={"requester_telegram_id": me.telegram_id}, headers=AUTH)
    assert own.status_code == 200
    assert own.json()["telegram_id"] == me.telegram_id
    assert foreign.status_code == 403


# --- M3: management rights require an approved account ----------------------------------------------------------


async def test_banned_band_owner_cannot_manage_band(session, make_user, make_band):
    owner = await make_user()
    band = await make_band(owner)
    owner.status = "banned"
    await session.commit()

    with pytest.raises(ForbiddenError):
        await bands.update_band_name(session, band.id, owner.telegram_id, "Hijacked")


async def test_banned_creator_cannot_cancel_or_edit_booking(session, make_user, make_band):
    owner = await make_user()
    band = await make_band(owner)
    start = (now_local() + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
    booking = await bookings.create_booking(
        session, owner.telegram_id, band.id, start.date(), start.time(), (start + timedelta(hours=1)).time(), "self", "Song"
    )
    owner.status = "banned"
    await session.commit()

    with pytest.raises(ForbiddenError):
        await bookings.delete_booking(session, owner.telegram_id, booking.id)
    with pytest.raises(ForbiddenError):
        await bookings.update_booking(session, owner.telegram_id, booking.id, song_title="Changed")


# --- the bot reads `is_admin` and `permissions` from the user payload -------------------------------------------------


async def test_annotate_access_reports_the_admin_role_and_permissions(session, make_user, grant):
    from app.services.permission_catalog import DEFAULT_ROLES, PERMISSIONS

    admin, dsvr, group_admin, plain = [await make_user() for _ in range(4)]
    await grant(admin, "Админ")
    await grant(dsvr, "ДСВР")
    await grant(group_admin, "Админ группы", "music")

    reports = {
        name: await users.annotate_access(session, person)
        for name, person in {"admin": admin, "dsvr": dsvr, "group_admin": group_admin, "plain": plain}.items()
    }

    assert (reports["admin"].is_admin, reports["admin"].permissions) == (True, sorted(PERMISSIONS))
    assert (reports["dsvr"].is_admin, reports["dsvr"].permissions) == (False, sorted(DEFAULT_ROLES["ДСВР"]))
    assert (reports["group_admin"].is_admin, reports["group_admin"].permissions) == (False, sorted(DEFAULT_ROLES["Админ группы"]))
    assert (reports["plain"].is_admin, reports["plain"].permissions) == (False, [])


async def test_annotate_access_gives_nothing_to_an_unapproved_holder(session, make_user, grant):
    pending = await make_user(status="pending")
    await grant(pending, "Админ")

    annotated = await users.annotate_access(session, pending)

    assert (annotated.is_admin, annotated.permissions) == (False, [])


async def test_http_user_payload_carries_is_admin_and_permissions(client, make_user, role_holder):
    moderator = await role_holder("Модератор", ["users.moderate"])

    body = (
        await client.get(f"/users/{moderator.telegram_id}", params={"requester_telegram_id": moderator.telegram_id}, headers=AUTH)
    ).json()

    assert body["is_admin"] is False and body["permissions"] == ["users.moderate"]
