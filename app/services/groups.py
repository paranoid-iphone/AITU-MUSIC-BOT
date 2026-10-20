from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group
from app.services.permissions import holders_of


async def list_groups(session: AsyncSession) -> list[Group]:
    result = await session.execute(select(Group).order_by(Group.id))
    return list(result.scalars())


async def registration_reviewer_ids(session: AsyncSession, group_id: int | None) -> list[int]:
    """Telegram ids who should be asked to approve a registration: holders of `users.moderate` for the whole club and,
    when the group is known, for that group."""
    return sorted(user.telegram_id for user in await holders_of(session, "users.moderate", group_id))
