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


def is_approved(user: User) -> bool:
    return user.status == UserStatus.APPROVED.value


async def is_band_member(session: AsyncSession, user: User, band_id: int) -> bool:
    result = await session.execute(select(BandMember.id).where(BandMember.band_id == band_id, BandMember.user_id == user.id))
    return result.first() is not None


async def can_manage_band(session: AsyncSession, user: User, band: Band) -> bool:
    if not is_approved(user):
        return False
    if await is_group_admin(session, user, band.group_id):
        return True
    return await is_band_owner(session, user, band.id)


async def can_view_band(session: AsyncSession, user: User, band: Band) -> bool:
    """Band details (members, contacts) are for its members and for the admins who manage it."""
    if not is_approved(user):
        return False
    if await can_manage_band(session, user, band):
        return True
    return await is_band_member(session, user, band.id)


async def can_view_invite_code(session: AsyncSession, user: User, band: Band) -> bool:
    """The join code lets anyone enter the band, so only the people who run it may see it."""
    if not is_approved(user):
        return False
    if band.responsible_user_id == user.id:
        return True
    return await can_manage_band(session, user, band)


async def can_view_user(session: AsyncSession, viewer: User, target: User) -> bool:
    """A profile is visible to its owner and to the admins who moderate that user's group."""
    if viewer.id == target.id:
        return True
    if not is_approved(viewer):
        return False
    return await is_group_admin(session, viewer, target.group_id)


async def can_manage_schedule(session: AsyncSession, user: User, booking: Booking) -> bool:
    """Who may edit or cancel a booking: admins, DSVR, the band's group admins, its creator and band owners."""
    if not is_approved(user):
        return False
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
