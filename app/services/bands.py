from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Band, BandMember
from app.services.errors import ForbiddenError, NotFoundError
from app.services.users import get_user_by_telegram_id, require_approved


async def create_band(session: AsyncSession, creator_telegram_id: int, name: str, creator_role: str) -> Band:
    creator = await get_user_by_telegram_id(session, creator_telegram_id)
    if creator is None:
        raise NotFoundError("User not found")
    require_approved(creator)
    band = Band(name=name, creator_id=creator.id)
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
        .options(selectinload(Band.members).selectinload(BandMember.user))
        .order_by(Band.created_at.desc())
    )
    return list(result.scalars().unique())


async def get_band_detail(session: AsyncSession, band_id: int) -> Band:
    result = await session.execute(
        select(Band).where(Band.id == band_id).options(selectinload(Band.members).selectinload(BandMember.user))
    )
    band = result.scalar_one_or_none()
    if band is None:
        raise NotFoundError("Band not found")
    return band


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
    requester = await get_user_by_telegram_id(session, requester_telegram_id)
    if requester is None:
        raise NotFoundError("User not found")
    owner_result = await session.execute(
        select(BandMember).where(BandMember.band_id == band_id, BandMember.user_id == requester.id, BandMember.is_owner.is_(True))
    )
    if owner_result.scalar_one_or_none() is None:
        raise ForbiddenError("Only band owner can update roles")
    result = await session.execute(select(BandMember).where(BandMember.id == member_id, BandMember.band_id == band_id))
    member = result.scalar_one_or_none()
    if member is None:
        raise NotFoundError("Member not found")
    member.instrument_role = instrument_role
    await session.commit()
    await session.refresh(member)
    return member
