from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import User, UserRole, UserStatus
from app.services.errors import ConflictError, ForbiddenError, NotFoundError


async def get_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def get_user_by_username(session: AsyncSession, username: str) -> User | None:
    normalized = username.strip().lstrip("@")
    result = await session.execute(select(User).where(User.telegram_username == normalized))
    return result.scalar_one_or_none()


async def get_user_by_identifier(session: AsyncSession, identifier: str) -> User | None:
    raw = identifier.strip()
    if raw.isdigit():
        return await get_user_by_telegram_id(session, int(raw))
    return await get_user_by_username(session, raw)


async def upsert_telegram_user(
    session: AsyncSession,
    telegram_id: int,
    telegram_username: str | None,
    telegram_full_name: str | None,
) -> User:
    user = await get_user_by_telegram_id(session, telegram_id)
    is_initial_admin = telegram_id in settings.initial_admin_ids
    role = UserRole.ADMIN.value if is_initial_admin else UserRole.USER.value
    status = UserStatus.APPROVED.value if is_initial_admin else UserStatus.PENDING.value
    if user is None:
        user = User(
            telegram_id=telegram_id,
            telegram_username=telegram_username,
            telegram_full_name=telegram_full_name,
            language=settings.default_language,
            role=role,
            status=status,
        )
        session.add(user)
    else:
        user.telegram_username = telegram_username
        user.telegram_full_name = telegram_full_name
        if user.status == UserStatus.DELETED.value and not is_initial_admin:
            user.status = UserStatus.PENDING.value
            user.role = UserRole.USER.value
            user.first_name = None
            user.last_name = None
            user.study_group = None
            user.language = settings.default_language
            user.moderation_reason = None
            user.registration_retry_after = None
            user.approved_by_id = None
            user.approved_at = None
        if user.telegram_id in settings.initial_admin_ids:
            user.role = UserRole.ADMIN.value
            user.status = UserStatus.APPROVED.value
    await session.commit()
    await session.refresh(user)
    return user


async def update_profile(
    session: AsyncSession,
    telegram_id: int,
    first_name: str | None = None,
    last_name: str | None = None,
    study_group: str | None = None,
    language: str | None = None,
    notifications_enabled: bool | None = None,
) -> User:
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    if first_name is not None:
        user.first_name = first_name
    if last_name is not None:
        user.last_name = last_name
    if study_group is not None:
        user.study_group = study_group
    if language is not None:
        user.language = language
    if notifications_enabled is not None:
        user.notifications_enabled = notifications_enabled
    await session.commit()
    await session.refresh(user)
    return user


def require_admin(user: User) -> None:
    if user.role != UserRole.ADMIN.value:
        raise ForbiddenError("Admin access required")


def require_approved(user: User) -> None:
    if user.role == UserRole.ADMIN.value:
        return
    if user.status != UserStatus.APPROVED.value:
        raise ForbiddenError("User is not approved")


async def list_users_by_status(session: AsyncSession, admin_telegram_id: int, status: str | None = None) -> list[User]:
    admin = await get_user_by_telegram_id(session, admin_telegram_id)
    if admin is None:
        raise NotFoundError("Admin not found")
    require_admin(admin)
    stmt = select(User).order_by(User.created_at.desc())
    if status is not None:
        stmt = stmt.where(User.status == status)
    result = await session.execute(stmt)
    return list(result.scalars())


async def approve_user(session: AsyncSession, admin_telegram_id: int, target_identifier: str) -> User:
    from app.services.audit import log_action

    admin = await get_user_by_telegram_id(session, admin_telegram_id)
    target = await get_user_by_identifier(session, target_identifier)
    if admin is None or target is None:
        raise NotFoundError("User not found")
    require_admin(admin)
    target.status = UserStatus.APPROVED.value
    target.moderation_reason = None
    target.registration_retry_after = None
    target.approved_by_id = admin.id
    target.approved_at = datetime.utcnow()
    await log_action(
        session,
        admin.id,
        "user.approve",
        "user",
        target.telegram_id,
        f"Одобрил регистрацию: @{target.telegram_username or target.telegram_id}",
    )
    await session.commit()
    await session.refresh(target)
    return target


async def promote_to_admin(session: AsyncSession, admin_telegram_id: int, target_identifier: str) -> User:
    from app.services.audit import log_action

    admin = await get_user_by_telegram_id(session, admin_telegram_id)
    target = await get_user_by_identifier(session, target_identifier)
    if admin is None or target is None:
        raise NotFoundError("User not found")
    require_admin(admin)
    if admin.telegram_id not in settings.initial_admin_ids:
        raise ForbiddenError("Only initial admins can add admins")
    if target.status == UserStatus.BANNED.value:
        raise ForbiddenError("User is banned")

    target.role = UserRole.ADMIN.value
    target.status = UserStatus.APPROVED.value
    target.moderation_reason = None
    target.registration_retry_after = None
    target.approved_by_id = admin.id
    target.approved_at = datetime.utcnow()
    await log_action(
        session,
        admin.id,
        "admin.add",
        "user",
        target.telegram_id,
        f"Added admin: @{target.telegram_username or target.telegram_id}",
    )
    await session.commit()
    await session.refresh(target)
    return target


async def reject_user(session: AsyncSession, admin_telegram_id: int, target_identifier: str, reason: str | None) -> User:
    from app.services.audit import log_action

    admin = await get_user_by_telegram_id(session, admin_telegram_id)
    target = await get_user_by_identifier(session, target_identifier)
    if admin is None or target is None:
        raise NotFoundError("User not found")
    require_admin(admin)
    target.status = UserStatus.REJECTED.value
    target.moderation_reason = reason
    target.registration_retry_after = datetime.utcnow() + timedelta(hours=settings.registration_retry_hours)
    await log_action(
        session,
        admin.id,
        "user.reject",
        "user",
        target.telegram_id,
        f"Отклонил заявку: @{target.telegram_username or target.telegram_id}. Причина: {reason or '-'}",
    )
    await session.commit()
    await session.refresh(target)
    return target


async def ban_user(session: AsyncSession, admin_telegram_id: int, target_identifier: str, reason: str | None) -> User:
    from app.services.audit import log_action

    admin = await get_user_by_telegram_id(session, admin_telegram_id)
    target = await get_user_by_identifier(session, target_identifier)
    if admin is None or target is None:
        raise NotFoundError("User not found")
    require_admin(admin)
    target.status = UserStatus.BANNED.value
    target.moderation_reason = reason
    target.registration_retry_after = None
    await log_action(
        session,
        admin.id,
        "user.ban",
        "user",
        target.telegram_id,
        f"Забанил пользователя: @{target.telegram_username or target.telegram_id}. Причина: {reason or '-'}",
    )
    await session.commit()
    await session.refresh(target)
    return target


async def soft_delete_user(session: AsyncSession, admin_telegram_id: int, target_identifier: str, reason: str | None) -> User:
    from app.services.audit import log_action

    admin = await get_user_by_telegram_id(session, admin_telegram_id)
    target = await get_user_by_identifier(session, target_identifier)
    if admin is None or target is None:
        raise NotFoundError("User not found")
    require_admin(admin)
    target.status = UserStatus.DELETED.value
    target.first_name = None
    target.last_name = None
    target.study_group = None
    target.telegram_username = None
    target.telegram_full_name = None
    target.moderation_reason = reason
    target.registration_retry_after = None
    await log_action(
        session,
        admin.id,
        "user.delete",
        "user",
        target.telegram_id,
        f"Удалил пользователя: {target_identifier}. Причина: {reason or '-'}",
    )
    await session.commit()
    await session.refresh(target)
    return target


async def resubmit_registration(session: AsyncSession, telegram_id: int) -> User:
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    if user.status == UserStatus.BANNED.value:
        raise ForbiddenError("User is banned")
    if user.status == UserStatus.APPROVED.value:
        return user
    if not user.profile_completed:
        raise ConflictError("Profile is incomplete")
    now = datetime.utcnow()
    if user.status == UserStatus.REJECTED.value and user.registration_retry_after and user.registration_retry_after > now:
        raise ConflictError(f"You can submit again after {user.registration_retry_after.isoformat()}")
    user.status = UserStatus.PENDING.value
    user.moderation_reason = None
    user.registration_retry_after = None
    await session.commit()
    await session.refresh(user)
    return user
