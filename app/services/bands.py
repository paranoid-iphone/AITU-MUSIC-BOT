from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.timezone import now_local
from app.db.models import (
    AvailabilityRequest,
    Band,
    BandMember,
    Booking,
    BookingStatus,
    EventApplication,
    EventApplicationMember,
    UserStatus,
)
from app.services.errors import ForbiddenError, NotFoundError
from app.services.outbox import commit_and_publish
from app.services.permissions import (
    administered_group_ids,
    can_manage_band,
    can_view_band,
    can_view_invite_code,
    is_global_admin,
)
from app.services.schedule_history import record_change, snapshot
from app.services.users import get_user_by_telegram_id, require_approved


def band_load_options():
    return (
        selectinload(Band.members).selectinload(BandMember.user),
        selectinload(Band.responsible),
        selectinload(Band.group),
    )


async def _require_band_manager(session: AsyncSession, band_id: int, requester_telegram_id: int):
    requester = await get_user_by_telegram_id(session, requester_telegram_id)
    if requester is None:
        raise NotFoundError("User not found")
    band = (await session.execute(select(Band).where(Band.id == band_id))).scalar_one_or_none()
    if band is None:
        raise NotFoundError("Band not found")
    if not await can_manage_band(session, requester, band):
        raise ForbiddenError("Only band owner or group admin can manage this band")
    return requester


async def create_band(session: AsyncSession, creator_telegram_id: int, name: str, creator_role: str) -> Band:
    creator = await get_user_by_telegram_id(session, creator_telegram_id)
    if creator is None:
        raise NotFoundError("User not found")
    require_approved(creator)
    band = Band(name=name, creator_id=creator.id, group_id=creator.group_id, responsible_user_id=creator.id)
    session.add(band)
    await session.flush()
    session.add(BandMember(band_id=band.id, user_id=creator.id, instrument_role=creator_role, is_owner=True))
    await session.commit()
    await session.refresh(band)
    return band


async def list_user_bands(session: AsyncSession, telegram_id: int) -> list[Band]:
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        return []
    result = await session.execute(
        select(Band)
        .join(BandMember)
        .where(BandMember.user_id == user.id)
        .options(*band_load_options())
        .order_by(Band.created_at.desc())
    )
    return list(result.scalars().unique())


async def list_all_bands(session: AsyncSession, admin_telegram_id: int) -> list[Band]:
    admin = await get_user_by_telegram_id(session, admin_telegram_id)
    if admin is None:
        raise NotFoundError("Admin not found")
    stmt = select(Band).options(*band_load_options()).order_by(Band.created_at.desc())
    if not is_global_admin(admin):
        group_ids = await administered_group_ids(session, admin)
        if not group_ids or admin.status != UserStatus.APPROVED.value:
            raise ForbiddenError("Admin access required")
        stmt = stmt.where(Band.group_id.in_(group_ids))
    result = await session.execute(stmt)
    return list(result.scalars().unique())


async def get_band_detail(session: AsyncSession, band_id: int) -> Band:
    result = await session.execute(
        select(Band).where(Band.id == band_id).options(*band_load_options())
    )
    band = result.scalar_one_or_none()
    if band is None:
        raise NotFoundError("Band not found")
    return band


async def get_band_for_requester(session: AsyncSession, band_id: int, requester_telegram_id: int) -> Band:
    requester = await get_user_by_telegram_id(session, requester_telegram_id)
    if requester is None:
        raise NotFoundError("User not found")
    band = await get_band_detail(session, band_id)
    if not await can_view_band(session, requester, band):
        raise ForbiddenError("You do not have access to this band")
    return band


async def invite_code_visible_ids(session: AsyncSession, requester_telegram_id: int, bands: list[Band]) -> set[int]:
    """Ids of the bands whose join code the requester may see; the API blanks the code for the rest."""
    requester = await get_user_by_telegram_id(session, requester_telegram_id)
    if requester is None:
        return set()
    return {band.id for band in bands if await can_view_invite_code(session, requester, band)}


async def join_band(session: AsyncSession, telegram_id: int, invite_code: str, instrument_role: str) -> Band:
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_approved(user)
    result = await session.execute(select(Band).where(Band.invite_code == invite_code.upper()))
    band = result.scalar_one_or_none()
    if band is None:
        raise NotFoundError("Invite code not found")
    existing = await session.execute(select(BandMember).where(BandMember.band_id == band.id, BandMember.user_id == user.id))
    member = existing.scalar_one_or_none()
    if member is None:
        session.add(BandMember(band_id=band.id, user_id=user.id, instrument_role=instrument_role))
    else:
        member.instrument_role = instrument_role
    await session.commit()
    return await get_band_detail(session, band.id)


async def update_member_role(
    session: AsyncSession,
    band_id: int,
    member_id: int,
    requester_telegram_id: int,
    instrument_role: str,
) -> BandMember:
    from app.services.audit import log_action

    requester = await _require_band_manager(session, band_id, requester_telegram_id)
    result = await session.execute(select(BandMember).where(BandMember.id == member_id, BandMember.band_id == band_id))
    member = result.scalar_one_or_none()
    if member is None:
        raise NotFoundError("Member not found")
    member.instrument_role = instrument_role
    await log_action(
        session,
        requester.id,
        "band.member_role",
        "band_member",
        member.id,
        f"Изменил роль участника #{member.id} в коллективе #{band_id}: {instrument_role}",
    )
    await session.commit()
    await session.refresh(member)
    return member


async def update_band_name(session: AsyncSession, band_id: int, requester_telegram_id: int, name: str) -> Band:
    from app.services.audit import log_action

    requester = await _require_band_manager(session, band_id, requester_telegram_id)
    band = await get_band_detail(session, band_id)
    old_name = band.name
    band.name = name
    await log_action(
        session,
        requester.id,
        "band.rename",
        "band",
        band.id,
        f"Переименовал коллектив: {old_name} -> {name}",
    )
    await session.commit()
    return await get_band_detail(session, band_id)


async def update_band_responsible(session: AsyncSession, band_id: int, requester_telegram_id: int, member_id: int) -> Band:
    from app.services.audit import log_action

    requester = await _require_band_manager(session, band_id, requester_telegram_id)
    band = await get_band_detail(session, band_id)
    target = next((member for member in band.members if member.id == member_id), None)
    if target is None:
        raise NotFoundError("Member not found")
    band.responsible_user_id = target.user_id
    await log_action(
        session,
        requester.id,
        "band.responsible",
        "band",
        band_id,
        f"Назначил ответственного коллектива #{band_id}: участник #{member_id}",
    )
    await session.commit()
    session.expire(band)
    return await get_band_detail(session, band_id)


async def transfer_band_owner(session: AsyncSession, band_id: int, requester_telegram_id: int, member_id: int) -> Band:
    from app.services.audit import log_action

    requester = await _require_band_manager(session, band_id, requester_telegram_id)
    result = await session.execute(select(BandMember).where(BandMember.band_id == band_id))
    members = list(result.scalars())
    target = next((member for member in members if member.id == member_id), None)
    if target is None:
        raise NotFoundError("Member not found")
    for member in members:
        member.is_owner = member.id == member_id
    band = await get_band_detail(session, band_id)
    band.creator_id = target.user_id
    await log_action(
        session,
        requester.id,
        "band.owner",
        "band",
        band_id,
        f"Назначил лидера коллектива #{band_id}: участник #{member_id}",
    )
    await session.commit()
    return await get_band_detail(session, band_id)


async def remove_band_member(session: AsyncSession, band_id: int, member_id: int, requester_telegram_id: int) -> dict:
    from app.services.audit import log_action

    requester = await _require_band_manager(session, band_id, requester_telegram_id)
    result = await session.execute(
        select(BandMember).where(BandMember.id == member_id, BandMember.band_id == band_id).options(selectinload(BandMember.user))
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise NotFoundError("Member not found")
    if member.is_owner:
        raise ForbiddenError("Transfer ownership before removing the band owner")
    user_name = (
        f"{member.user.last_name or ''} {member.user.first_name or ''}".strip()
        or member.user.telegram_username
        or str(member.user.telegram_id)
    )
    band = await get_band_detail(session, band_id)
    if band.responsible_user_id == member.user_id:
        owner = next((m for m in band.members if m.is_owner), None)
        band.responsible_user_id = owner.user_id if owner is not None else None
    deleted = {"member_id": member.id, "band_id": band_id, "user_name": user_name}
    await log_action(
        session,
        requester.id,
        "band.member_remove",
        "band_member",
        member.id,
        f"Удалил участника из коллектива #{band_id}: {user_name}",
    )
    await session.delete(member)
    await session.commit()
    return deleted


async def delete_band(session: AsyncSession, band_id: int, requester_telegram_id: int) -> dict:
    from app.services.audit import log_action

    from app.services.bookings import _lock_bookings

    requester = await _require_band_manager(session, band_id, requester_telegram_id)
    await _lock_bookings(session)
    band = await get_band_detail(session, band_id)
    deleted = {"id": band.id, "name": band.name}

    now = now_local()
    bookings_result = await session.execute(select(Booking).where(Booking.band_id == band_id))
    for booking in bookings_result.scalars():
        if booking.status == BookingStatus.CONFIRMED.value and datetime.combine(booking.booking_date, booking.end_time) > now:
            before = snapshot(booking)
            booking.status = BookingStatus.CANCELLED.value
            booking.cancelled_by_id = requester.id
            booking.cancel_reason = "Коллектив удалён"
            await record_change(session, booking, "cancelled", requester, before=before, reason=booking.cancel_reason)
        # Past and cancelled bookings stay as history; ScheduleChange keeps the band name.
        booking.band_id = None

    requests_result = await session.execute(select(AvailabilityRequest).where(AvailabilityRequest.band_id == band_id))
    for availability_request in requests_result.scalars():
        await session.delete(availability_request)

    applications_result = await session.execute(select(EventApplication).where(EventApplication.band_id == band_id))
    for application in applications_result.scalars():
        members_result = await session.execute(
            select(EventApplicationMember).where(EventApplicationMember.application_id == application.id)
        )
        for member in members_result.scalars():
            await session.delete(member)
        await session.delete(application)

    await log_action(
        session,
        requester.id,
        "band.delete",
        "band",
        band.id,
        f"Удалил коллектив: {band.name}",
    )
    await session.delete(band)
    await commit_and_publish(session)
    return deleted
