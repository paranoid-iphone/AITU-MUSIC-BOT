from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.models import Group, Role, RoleAssignment, RolePermission, User, UserStatus
from app.services import outbox
from app.services.errors import ConflictError, ForbiddenError, NotFoundError
from app.services.permission_catalog import ADMIN_ROLE_NAME, DEFAULT_ROLES, PERMISSIONS
from app.services.permissions import holds_admin_role, require_permission
from app.services.users import get_user_by_identifier, get_user_by_telegram_id

MAX_NAME_LENGTH = 60


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


async def ensure_initial_admin(session: AsyncSession, user: User) -> None:
    """Gives a user from `INITIAL_ADMIN_TELEGRAM_IDS` the club-wide admin role if they lack it. No commit; the caller
    decides who is an initial admin and sets the status."""
    role = await ensure_admin_role(session)
    await session.flush()  # a new user needs an id
    held = await session.execute(
        select(RoleAssignment.id).where(
            RoleAssignment.user_id == user.id, RoleAssignment.role_id == role.id, RoleAssignment.group_id.is_(None)
        )
    )
    if held.first() is None:
        session.add(RoleAssignment(user_id=user.id, role_id=role.id, group_id=None))
        await session.flush()


async def revoke_all(session: AsyncSession, user: User) -> None:
    """Takes every role away from a user (ban, deletion). No commit, no journal entry: the caller records why."""
    await session.execute(delete(RoleAssignment).where(RoleAssignment.user_id == user.id))


async def _actor(session: AsyncSession, telegram_id: int) -> User:
    actor = await get_user_by_telegram_id(session, telegram_id)
    if actor is None:
        raise NotFoundError("User not found")
    await require_permission(session, actor, "roles.manage")
    return actor


def _clean_name(name: str) -> str:
    name = name.strip()
    if not 1 <= len(name) <= MAX_NAME_LENGTH:
        raise ConflictError("Role name must be 1-60 characters")
    return name


async def _ensure_name_free(session: AsyncSession, name: str, own_id: int | None = None) -> None:
    """Names are compared without regard to case (done here: SQLite and Postgres fold non-ASCII differently)."""
    rows = await session.execute(select(Role.id, Role.name))
    if any(role_id != own_id and other.casefold() == name.casefold() for role_id, other in rows.all()):
        raise ConflictError("A role with this name already exists")


def _clean_permissions(permissions: list[str]) -> list[str]:
    if any(key not in PERMISSIONS for key in permissions):
        raise ConflictError("Unknown permission")
    return list(dict.fromkeys(permissions))


async def _load_role(session: AsyncSession, role_id: int) -> Role:
    result = await session.execute(select(Role).where(Role.id == role_id).options(selectinload(Role.permissions)))
    role = result.scalar_one_or_none()
    if role is None:
        raise NotFoundError("Role not found")
    return role


def _require_editable(role: Role) -> None:
    if role.is_system:
        raise ConflictError("System role cannot be changed")


async def _require_admin_holder(session: AsyncSession, actor: User) -> None:
    if not await holds_admin_role(session, actor):
        raise ForbiddenError("Only an admin can assign or revoke the admin role")


def _person(user: User) -> str:
    return f"@{user.telegram_username}" if user.telegram_username else str(user.telegram_id)


def _notify(session: AsyncSession, target: User, text: str) -> None:
    if target.notifications_enabled:
        outbox.queue_notification(session, target.telegram_id, text)


async def user_assignments(session: AsyncSession, user: User) -> list[RoleAssignment]:
    """The roles a user holds, with the role and the group loaded. No permission check: for cards and /me."""
    result = await session.execute(
        select(RoleAssignment)
        .where(RoleAssignment.user_id == user.id)
        .options(selectinload(RoleAssignment.role), selectinload(RoleAssignment.group))
        .order_by(RoleAssignment.id)
    )
    return list(result.scalars())


async def list_roles(session: AsyncSession, actor_telegram_id: int) -> list[dict]:
    await _actor(session, actor_telegram_id)
    roles = await session.execute(select(Role).options(selectinload(Role.permissions)).order_by(Role.id))
    holders = dict(
        (await session.execute(select(RoleAssignment.role_id, func.count()).group_by(RoleAssignment.role_id))).all()
    )
    return [
        {
            "id": role.id,
            "name": role.name,
            "is_system": role.is_system,
            "permissions": list(PERMISSIONS) if role.is_system else [item.permission for item in role.permissions],
            "holders": holders.get(role.id, 0),
        }
        for role in roles.scalars()
    ]


async def create_role(session: AsyncSession, actor_telegram_id: int, name: str, permissions: list[str]) -> Role:
    from app.services.audit import log_action

    actor = await _actor(session, actor_telegram_id)
    name = _clean_name(name)
    keys = _clean_permissions(permissions)
    await _ensure_name_free(session, name)
    role = Role(name=name, permissions=[RolePermission(permission=key) for key in keys])
    session.add(role)
    await session.flush()
    await log_action(session, actor.id, "role.create", "role", role.id, f"Создал роль «{name}»: {', '.join(keys) or '-'}")
    await session.commit()
    return role


async def update_role(
    session: AsyncSession, actor_telegram_id: int, role_id: int, name: str | None, permissions: list[str] | None
) -> Role:
    from app.services.audit import log_action

    actor = await _actor(session, actor_telegram_id)
    role = await _load_role(session, role_id)
    _require_editable(role)
    if name is not None:
        name = _clean_name(name)
        await _ensure_name_free(session, name, own_id=role.id)
        role.name = name
    if permissions is not None:
        keys = _clean_permissions(permissions)
        # Only the difference is written: re-inserting a key that stays would hit the unique constraint, because the
        # unit of work inserts before it deletes.
        for item in [item for item in role.permissions if item.permission not in keys]:
            role.permissions.remove(item)
        kept = {item.permission for item in role.permissions}
        role.permissions.extend(RolePermission(permission=key) for key in keys if key not in kept)
    await session.flush()
    kept = ", ".join(item.permission for item in role.permissions) or "-"
    await log_action(session, actor.id, "role.update", "role", role.id, f"Изменил роль «{role.name}»: {kept}")
    await session.commit()
    return role


async def delete_role(session: AsyncSession, actor_telegram_id: int, role_id: int) -> int:
    """Deletes the role together with its assignments; returns how many people lost it."""
    from app.services.audit import log_action

    actor = await _actor(session, actor_telegram_id)
    role = await _load_role(session, role_id)
    _require_editable(role)
    holders = (
        await session.execute(select(func.count()).select_from(RoleAssignment).where(RoleAssignment.role_id == role.id))
    ).scalar_one()
    name = role.name
    await session.delete(role)
    await log_action(session, actor.id, "role.delete", "role", role_id, f"Удалил роль «{name}» (была у {holders})")
    await session.commit()
    return holders


async def list_assignments(session: AsyncSession, actor_telegram_id: int, role_id: int) -> list[RoleAssignment]:
    await _actor(session, actor_telegram_id)
    await _load_role(session, role_id)
    result = await session.execute(
        select(RoleAssignment)
        .where(RoleAssignment.role_id == role_id)
        .options(selectinload(RoleAssignment.user), selectinload(RoleAssignment.group))
        .order_by(RoleAssignment.id)
    )
    return list(result.scalars())


async def assign_role(
    session: AsyncSession, actor_telegram_id: int, target_identifier: str, role_id: int, group_id: int | None
) -> RoleAssignment:
    from app.services.audit import log_action

    actor = await _actor(session, actor_telegram_id)
    target = await get_user_by_identifier(session, target_identifier)
    if target is None:
        raise NotFoundError("User not found")
    role = await _load_role(session, role_id)
    group = None
    if group_id is not None:
        group = await session.get(Group, group_id)
        if group is None:
            raise NotFoundError("Group not found")
    if role.is_system:
        await _require_admin_holder(session, actor)
        if group_id is not None:
            raise ConflictError("The admin role applies to the whole club")
    if target.status != UserStatus.APPROVED.value:
        raise ConflictError("User must be approved to receive a role")
    same_scope = RoleAssignment.group_id.is_(None) if group_id is None else RoleAssignment.group_id == group_id
    duplicate = await session.execute(
        select(RoleAssignment.id).where(RoleAssignment.user_id == target.id, RoleAssignment.role_id == role.id, same_scope)
    )
    if duplicate.first() is not None:
        raise ConflictError("Role is already assigned")

    assignment = RoleAssignment(user_id=target.id, role_id=role.id, group_id=group_id)
    session.add(assignment)
    await session.flush()
    scope = f" ({group.title})" if group else ""
    await log_action(
        session, actor.id, "role.assign", "role", role.id, f"Выдал роль «{role.name}»{scope}: {_person(target)}"
    )
    _notify(session, target, f"Вам выдана роль «{role.name}»{scope} в AITU Music Club Bot.")
    await outbox.commit_and_publish(session)
    return assignment


async def revoke_assignment(session: AsyncSession, actor_telegram_id: int, assignment_id: int) -> User:
    from app.services.audit import log_action

    actor = await _actor(session, actor_telegram_id)
    result = await session.execute(
        select(RoleAssignment)
        .where(RoleAssignment.id == assignment_id)
        .options(selectinload(RoleAssignment.role), selectinload(RoleAssignment.user), selectinload(RoleAssignment.group))
    )
    assignment = result.scalar_one_or_none()
    if assignment is None:
        raise NotFoundError("Assignment not found")
    role, target = assignment.role, assignment.user
    if role.is_system:
        await _require_admin_holder(session, actor)
        if target.id == actor.id:
            raise ForbiddenError("You cannot remove your own admin role")
        if target.telegram_id in settings.initial_admin_ids:
            raise ForbiddenError("Initial admin cannot be demoted")
    scope = f" ({assignment.group.title})" if assignment.group else ""
    await session.delete(assignment)
    await log_action(
        session, actor.id, "role.revoke", "role", role.id, f"Снял роль «{role.name}»{scope}: {_person(target)}"
    )
    _notify(session, target, f"С вас сняли роль «{role.name}»{scope} в AITU Music Club Bot.")
    await outbox.commit_and_publish(session)
    return target
