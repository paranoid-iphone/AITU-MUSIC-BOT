from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Group, GroupAdmin, User, UserRole, UserStatus
from app.services.errors import ConflictError, NotFoundError
from app.services.users import get_user_by_identifier, get_user_by_telegram_id, require_admin


async def list_groups(session: AsyncSession) -> list[Group]:
    result = await session.execute(select(Group).order_by(Group.id))
    return list(result.scalars())


async def get_group(session: AsyncSession, group_id: int) -> Group:
    result = await session.execute(select(Group).where(Group.id == group_id))
    group = result.scalar_one_or_none()
    if group is None:
        raise NotFoundError("Group not found")
    return group


async def list_group_admins(session: AsyncSession, telegram_id: int, group_id: int) -> list[User]:
    actor = await get_user_by_telegram_id(session, telegram_id)
    if actor is None:
        raise NotFoundError("User not found")
    require_admin(actor)
    await get_group(session, group_id)
    result = await session.execute(
        select(GroupAdmin).where(GroupAdmin.group_id == group_id).options(selectinload(GroupAdmin.user))
    )
    return [item.user for item in result.scalars()]


async def add_group_admin(session: AsyncSession, telegram_id: int, group_id: int, target_identifier: str) -> User:
    from app.services.audit import log_action

    actor = await get_user_by_telegram_id(session, telegram_id)
    target = await get_user_by_identifier(session, target_identifier)
    if actor is None or target is None:
        raise NotFoundError("User not found")
    require_admin(actor)
    group = await get_group(session, group_id)
    if target.status != UserStatus.APPROVED.value:
        raise ConflictError("User must be approved before becoming a group admin")
    existing = await session.execute(
        select(GroupAdmin).where(GroupAdmin.group_id == group_id, GroupAdmin.user_id == target.id)
    )
    if existing.scalar_one_or_none() is None:
        session.add(GroupAdmin(group_id=group_id, user_id=target.id))
    await log_action(
        session,
        actor.id,
        "group.admin.add",
        "group",
        group_id,
        f"Назначил админа группы {group.title}: @{target.telegram_username or target.telegram_id}",
    )
    await session.commit()
    return target


async def remove_group_admin(session: AsyncSession, telegram_id: int, group_id: int, target_identifier: str) -> User:
    from app.services.audit import log_action

    actor = await get_user_by_telegram_id(session, telegram_id)
    target = await get_user_by_identifier(session, target_identifier)
    if actor is None or target is None:
        raise NotFoundError("User not found")
    require_admin(actor)
    group = await get_group(session, group_id)
    result = await session.execute(
        select(GroupAdmin).where(GroupAdmin.group_id == group_id, GroupAdmin.user_id == target.id)
    )
    link = result.scalar_one_or_none()
    if link is None:
        raise NotFoundError("User is not an admin of this group")
    await session.delete(link)
    await log_action(
        session,
        actor.id,
        "group.admin.remove",
        "group",
        group_id,
        f"Снял админа группы {group.title}: @{target.telegram_username or target.telegram_id}",
    )
    await session.commit()
    return target


async def registration_reviewer_ids(session: AsyncSession, group_id: int | None) -> list[int]:
    """Telegram ids who should be asked to approve a registration: main admins plus admins of that group."""
    result = await session.execute(
        select(User.telegram_id).where(User.role == UserRole.ADMIN.value, User.status == UserStatus.APPROVED.value)
    )
    ids = {row[0] for row in result.all()}
    if group_id is not None:
        group_result = await session.execute(
            select(User.telegram_id)
            .join(GroupAdmin, GroupAdmin.user_id == User.id)
            .where(GroupAdmin.group_id == group_id, User.status == UserStatus.APPROVED.value)
        )
        ids.update(row[0] for row in group_result.all())
    return sorted(ids)
