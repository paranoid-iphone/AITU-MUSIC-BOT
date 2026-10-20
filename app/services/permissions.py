from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Band, BandMember, Booking, Role, RoleAssignment, User, UserStatus
from app.services.errors import ForbiddenError
from app.services.permission_catalog import PERMISSIONS


class Grant(NamedTuple):
    permission: str
    group_id: int | None  # None = the whole club


async def user_grants(session: AsyncSession, user: User) -> list[Grant]:
    """Everything the user may do, as (permission, group) pairs. Only approved users hold anything.

    A permission that exists only for the whole club is dropped from an assignment scoped to a group."""
    if user.status != UserStatus.APPROVED.value:
        return []
    result = await session.execute(
        select(RoleAssignment)
        .where(RoleAssignment.user_id == user.id)
        .options(selectinload(RoleAssignment.role).selectinload(Role.permissions))
    )
    grants: list[Grant] = []
    for assignment in result.scalars():
        keys = PERMISSIONS if assignment.role.is_system else [item.permission for item in assignment.role.permissions]
        for key in keys:
            scope = PERMISSIONS.get(key)
            if scope is None:
                continue
            if assignment.group_id is None:
                grants.append(Grant(key, None))
            elif scope == "group":
                grants.append(Grant(key, assignment.group_id))
    return grants


async def has_permission(session: AsyncSession, user: User, perm: str, group_id: int | None = None) -> bool:
    """Whether `user` holds `perm`; with `group_id` also through an assignment scoped to that group."""
    for grant in await user_grants(session, user):
        if grant.permission != perm:
            continue
        if grant.group_id is None or (group_id is not None and grant.group_id == group_id):
            return True
    return False


async def require_permission(session: AsyncSession, user: User, perm: str, group_id: int | None = None) -> None:
    if not await has_permission(session, user, perm, group_id):
        raise ForbiddenError("Admin access required")


async def permitted_group_ids(session: AsyncSession, user: User, perm: str) -> set[int] | None:
    """`None` = every group (a club-wide grant); otherwise the groups the user holds `perm` for (maybe none)."""
    group_ids: set[int] = set()
    for grant in await user_grants(session, user):
        if grant.permission != perm:
            continue
        if grant.group_id is None:
            return None
        group_ids.add(grant.group_id)
    return group_ids


async def permission_summary(session: AsyncSession, user: User) -> dict[str, dict]:
    """What the clients need to show or hide things: per permission, `all` (held for the whole club) or the group ids
    it is held for."""
    held: dict[str, dict] = {}
    for grant in await user_grants(session, user):
        item = held.setdefault(grant.permission, {"all": False, "group_ids": set()})
        if grant.group_id is None:
            item["all"] = True
        else:
            item["group_ids"].add(grant.group_id)
    return {
        permission: {"all": item["all"], "group_ids": [] if item["all"] else sorted(item["group_ids"])}
        for permission, item in held.items()
    }


async def has_permission_anywhere(session: AsyncSession, user: User, perm: str) -> bool:
    group_ids = await permitted_group_ids(session, user, perm)
    return group_ids is None or bool(group_ids)


async def holds_admin_role(session: AsyncSession, user: User) -> bool:
    """The system admin role (the one that cannot be edited), held for the whole club."""
    if user.status != UserStatus.APPROVED.value:
        return False
    result = await session.execute(
        select(RoleAssignment.id)
        .join(Role, Role.id == RoleAssignment.role_id)
        .where(RoleAssignment.user_id == user.id, Role.is_system.is_(True), RoleAssignment.group_id.is_(None))
    )
    return result.first() is not None


async def has_any_role(session: AsyncSession, user: User) -> bool:
    """Whether the user holds any role at all; unlike the checks above this ignores the account status."""
    result = await session.execute(select(RoleAssignment.id).where(RoleAssignment.user_id == user.id).limit(1))
    return result.first() is not None


async def _role_holders(session: AsyncSession) -> list[User]:
    """Approved users who hold any role at all: the only ones that can hold a permission."""
    candidates = await session.execute(
        select(User)
        .where(User.status == UserStatus.APPROVED.value, User.id.in_(select(RoleAssignment.user_id)))
        .order_by(User.id)
    )
    return list(candidates.scalars())


async def holders_of(session: AsyncSession, perm: str, group_id: int | None = None) -> list[User]:
    """Approved users who hold `perm` for the whole club or, when `group_id` is given, for that group."""
    return [user for user in await _role_holders(session) if await has_permission(session, user, perm, group_id)]


async def holders_anywhere(session: AsyncSession, perm: str) -> list[User]:
    """Approved users who hold `perm` for the whole club or for at least one group."""
    return [user for user in await _role_holders(session) if await has_permission_anywhere(session, user, perm)]


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
    if await has_permission(session, user, "bands.manage", band.group_id):
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
    return await has_permission(session, viewer, "users.moderate", target.group_id)


async def can_manage_schedule(session: AsyncSession, user: User, booking: Booking) -> bool:
    """Who may edit or cancel a booking: holders of `bookings.manage` (club-wide, or for the band's group), its creator
    and band owners."""
    if not is_approved(user):
        return False
    if await has_permission(session, user, "bookings.manage"):
        return True
    if booking.created_by_id == user.id:
        return True
    if booking.band_id is None:
        return False
    band = booking.band
    if band is not None and await has_permission(session, user, "bookings.manage", band.group_id):
        return True
    return await is_band_owner(session, user, booking.band_id)
