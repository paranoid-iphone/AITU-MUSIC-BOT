from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import AuditLog, User
from app.services.errors import ConflictError, NotFoundError
from app.services.schedule_history import person_name
from app.services.search import contains
from app.services.users import get_user_by_telegram_id, require_admin

# The first segment of `AuditLog.action` ("user.ban" -> "user"); the journal filters by it.
AUDIT_CATEGORIES = (
    "user",
    "admin",
    "dsvr",
    "group",
    "band",
    "booking",
    "move_request",
    "event",
    "planning",
    "schedule",
    "settings",
)


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


async def list_actions(
    session: AsyncSession,
    telegram_id: int,
    limit: int = 30,
    before_id: int | None = None,
    search: str | None = None,
    category: str | None = None,
) -> list[dict]:
    """Newest first; pass the last id you got as `before_id` for the next page.

    `search` matches the description or the name/username of the actor, `category` the action prefix.
    """
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    if category is not None and category not in AUDIT_CATEGORIES:
        raise ConflictError("Unknown action category")
    stmt = (
        select(AuditLog)
        .options(selectinload(AuditLog.actor))
        .order_by(AuditLog.id.desc())
        .limit(max(1, min(limit, 100)))
    )
    if before_id is not None:
        stmt = stmt.where(AuditLog.id < before_id)
    if category is not None:
        stmt = stmt.where(AuditLog.action.like(f"{category}.%"))
    needle = (search or "").strip().lstrip("@")
    if needle:
        stmt = stmt.outerjoin(User, User.id == AuditLog.actor_id).where(
            or_(
                contains(AuditLog.description, needle),
                contains(User.first_name, needle),
                contains(User.last_name, needle),
                contains(User.telegram_username, needle),
            )
        )
    result = await session.execute(stmt)
    rows = []
    for item in result.scalars():
        actor = item.actor
        rows.append(
            {
                "id": item.id,
                "actor_id": item.actor_id,
                "actor_telegram_id": actor.telegram_id if actor is not None else None,
                "actor_name": person_name(actor),
                "action": item.action,
                "entity_type": item.entity_type,
                "entity_id": item.entity_id,
                "description": item.description,
                "created_at": item.created_at.isoformat(),
            }
        )
    return rows
