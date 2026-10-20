from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Group, GroupAdmin, User, UserStatus
from app.services import outbox
from app.services.errors import ConflictError, NotFoundError
from app.services.permissions import holders_of
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
    if target.notifications_enabled:
        outbox.queue_notification(
            session, target.telegram_id, "Вас назначили админом группы. Откройте /start, чтобы увидеть админ-меню."
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
    """Telegram ids who should be asked to approve a registration: holders of `users.moderate` for the whole club and,
    when the group is known, for that group."""
    return sorted(user.telegram_id for user in await holders_of(session, "users.moderate", group_id))
