from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Role, RolePermission
from app.services.permission_catalog import ADMIN_ROLE_NAME, DEFAULT_ROLES


async def ensure_admin_role(session: AsyncSession) -> Role:
    """The system admin role must always exist; it holds the whole permission catalog without rows of its own."""
    result = await session.execute(select(Role).where(Role.is_system.is_(True)))
    role = result.scalars().first()
    if role is None:
        role = Role(name=ADMIN_ROLE_NAME, is_system=True)
        session.add(role)
        await session.flush()
    return role


async def seed_default_roles(session: AsyncSession) -> None:
    """The roles a fresh club starts with, only into an empty `roles` table. Migrations seed real databases; this is
    for schemas built by `create_all` (tests). No commit."""
    if (await session.execute(select(Role.id).limit(1))).first() is not None:
        return
    await ensure_admin_role(session)
    for name, permissions in DEFAULT_ROLES.items():
        session.add(Role(name=name, permissions=[RolePermission(permission=key) for key in permissions]))
    await session.flush()
