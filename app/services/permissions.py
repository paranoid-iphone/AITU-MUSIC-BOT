from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Band, BandMember, Booking, GroupAdmin, User, UserRole, UserStatus
from app.services.errors import ForbiddenError


def is_global_admin(user: User) -> bool:
    return user.role == UserRole.ADMIN.value and user.status == UserStatus.APPROVED.value


def is_dsvr(user: User) -> bool:
    return user.role == UserRole.DSVR.value and user.status == UserStatus.APPROVED.value


async def is_group_admin(session: AsyncSession, user: User, group_id: int | None) -> bool:
    """Main admins manage every group; group admins only their own."""
    if is_global_admin(user):
        return True
    if group_id is None or user.status != UserStatus.APPROVED.value:
        return False
    result = await session.execute(
        select(GroupAdmin.id).where(GroupAdmin.group_id == group_id, GroupAdmin.user_id == user.id)
    )
    return result.first() is not None


async def require_group_admin(session: AsyncSession, user: User, group_id: int | None) -> None:
    if not await is_group_admin(session, user, group_id):
        raise ForbiddenError("Group admin access required")


async def administered_group_ids(session: AsyncSession, user: User) -> list[int]:
    result = await session.execute(select(GroupAdmin.group_id).where(GroupAdmin.user_id == user.id))
    return [row[0] for row in result.all()]


async def is_band_owner(session: AsyncSession, user: User, band_id: int | None) -> bool:
    if band_id is None:
        return False
    result = await session.execute(
        select(BandMember.id).where(
            BandMember.band_id == band_id,
            BandMember.user_id == user.id,
            BandMember.is_owner.is_(True),
        )
    )
    return result.first() is not None


async def can_manage_band(session: AsyncSession, user: User, band: Band) -> bool:
    if await is_group_admin(session, user, band.group_id):
        return True
    return await is_band_owner(session, user, band.id)


async def can_manage_schedule(session: AsyncSession, user: User, booking: Booking) -> bool:
    """Who may edit or cancel a booking: admins, DSVR, the band's group admins, its creator and band owners."""
    if is_global_admin(user) or is_dsvr(user):
        return True
    if booking.created_by_id == user.id:
        return True
    if booking.band_id is None:
        return False
    band = booking.band
    if band is not None and await is_group_admin(session, user, band.group_id):
        return True
    return await is_band_owner(session, user, booking.band_id)
