from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import AuditLog
from app.services.errors import NotFoundError
from app.services.users import get_user_by_telegram_id, require_admin


async def log_action(
    session: AsyncSession,
    actor_id: int | None,
    action: str,
    entity_type: str,
    entity_id: str | int | None = None,
    description: str = "",
) -> None:
    session.add(
        AuditLog(
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            description=description[:500],
        )
    )


async def list_recent_actions(session: AsyncSession, telegram_id: int, limit: int = 30) -> list[dict]:
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    result = await session.execute(
        select(AuditLog)
        .options(selectinload(AuditLog.actor))
        .order_by(AuditLog.created_at.desc())
        .limit(max(1, min(limit, 100)))
    )
    rows = []
    for item in result.scalars():
        actor = item.actor
        actor_name = "system"
        actor_telegram_id = None
        if actor is not None:
            actor_name = (
                f"{actor.last_name or ''} {actor.first_name or ''}".strip()
                or actor.telegram_username
                or str(actor.telegram_id)
            )
            actor_telegram_id = actor.telegram_id
        rows.append(
            {
                "id": item.id,
                "actor_id": item.actor_id,
                "actor_telegram_id": actor_telegram_id,
                "actor_name": actor_name,
                "action": item.action,
                "entity_type": item.entity_type,
                "entity_id": item.entity_id,
                "description": item.description,
                "created_at": item.created_at.isoformat(),
            }
        )
    return rows
