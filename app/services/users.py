from datetime import datetime, timedelta

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.timezone import format_moment, now_local
from app.db.models import User, UserRole, UserStatus
from app.services import outbox
from app.services.errors import ConflictError, ForbiddenError, NotFoundError
from app.services.permissions import administered_group_ids, can_view_user, is_global_admin, require_group_admin


async def get_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def annotate_group_admin(session: AsyncSession, user: User) -> User:
    """Attach a transient `is_group_admin` flag so API responses can drive the bot menus."""
    user.is_group_admin = bool(await administered_group_ids(session, user)) and user.status == UserStatus.APPROVED.value
    return user


async def get_user_for_requester(session: AsyncSession, telegram_id: int, requester_telegram_id: int) -> User:
    requester = await get_user_by_telegram_id(session, requester_telegram_id)
    if requester is None:
        raise NotFoundError("User not found")
    target = requester if telegram_id == requester_telegram_id else await get_user_by_telegram_id(session, telegram_id)
    if target is None:
        raise NotFoundError("User not found")
    if not await can_view_user(session, requester, target):
        raise ForbiddenError("You do not have access to this user")
    return await annotate_group_admin(session, target)


async def get_user_by_username(session: AsyncSession, username: str) -> User | None:
    normalized = username.strip().lstrip("@").lower()
    result = await session.execute(select(User).where(func.lower(User.telegram_username) == normalized).limit(2))
    matches = list(result.scalars())
    if len(matches) > 1:
        raise ConflictError("Several users share this username: use the numeric Telegram ID")
    return matches[0] if matches else None


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
    if telegram_username:
        # Telegram usernames are unique: whoever holds this one in our DB under another id is stale.
        await session.execute(
            update(User)
            .where(func.lower(User.telegram_username) == telegram_username.lower(), User.telegram_id != telegram_id)
            .values(telegram_username=None)
        )
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
            user.group_id = None
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
    return await annotate_group_admin(session, user)


async def update_profile(
    session: AsyncSession,
    telegram_id: int,
    first_name: str | None = None,
    last_name: str | None = None,
    study_group: str | None = None,
    language: str | None = None,
    notifications_enabled: bool | None = None,
    group_id: int | None = None,
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
    if group_id is not None:
        if user.status == UserStatus.APPROVED.value and user.group_id not in (None, group_id):
            raise ForbiddenError("Group can be changed only by an admin")
        user.group_id = group_id
    await session.commit()
    await session.refresh(user)
    return user


def require_admin(user: User) -> None:
    if user.role != UserRole.ADMIN.value or user.status != UserStatus.APPROVED.value:
        raise ForbiddenError("Admin access required")


def _guard_banned_or_deleted(admin: User, target: User) -> None:
    """Group admins must not lift a ban by rejecting or deleting a banned/deleted user (see `approve_user`)."""
    if target.status in (UserStatus.BANNED.value, UserStatus.DELETED.value) and not is_global_admin(admin):
        raise ForbiddenError("Only a main admin can change a banned or deleted user")


def _guard_moderation_target(admin: User, target: User) -> None:
    if target.telegram_id == admin.telegram_id:
        raise ForbiddenError("You cannot ban, reject or delete yourself")
    if target.telegram_id in settings.initial_admin_ids:
        raise ForbiddenError("Initial admin cannot be banned, rejected or deleted")


async def _require_moderation_rights(session: AsyncSession, admin: User, target: User) -> None:
    """Group admins moderate regular members of their own group; staff (admin/DSVR) only main admins."""
    if target.role in (UserRole.ADMIN.value, UserRole.DSVR.value):
        require_admin(admin)
        return
    await require_group_admin(session, admin, target.group_id)


def _notify(session: AsyncSession, target: User, text: str) -> None:
    if target.notifications_enabled:
        outbox.queue_notification(session, target.telegram_id, text)


def require_approved(user: User) -> None:
    if user.role == UserRole.ADMIN.value:
        return
    if user.status != UserStatus.APPROVED.value:
        raise ForbiddenError("User is not approved")


async def _scoped_users(session: AsyncSession, admin_telegram_id: int) -> Select:
    """Users the admin may moderate: everyone for a main admin, members of their own groups for a group admin."""
    admin = await get_user_by_telegram_id(session, admin_telegram_id)
    if admin is None:
        raise NotFoundError("Admin not found")
    stmt = select(User).order_by(User.created_at.desc())
    if not is_global_admin(admin):
        group_ids = await administered_group_ids(session, admin)
        if not group_ids or admin.status != UserStatus.APPROVED.value:
            raise ForbiddenError("Admin access required")
        stmt = stmt.where(User.group_id.in_(group_ids))
    return stmt


async def list_users_by_status(session: AsyncSession, admin_telegram_id: int, status: str | None = None) -> list[User]:
    stmt = await _scoped_users(session, admin_telegram_id)
    if status is not None:
        stmt = stmt.where(User.status == status)
    result = await session.execute(stmt)
    return list(result.scalars())


async def search_users(
    session: AsyncSession, admin_telegram_id: int, status: str | None, query: str | None
) -> tuple[list[User], dict[str, int]]:
    """Scoped users filtered by status and a literal, case-insensitive text match; counts ignore both filters."""
    result = await session.execute(await _scoped_users(session, admin_telegram_id))
    scoped = list(result.scalars())
    counts = {item.value: 0 for item in UserStatus}
    for user in scoped:
        counts[user.status] = counts.get(user.status, 0) + 1
    users = [user for user in scoped if status is None or user.status == status]
    needle = (query or "").strip().lstrip("@").lower()
    if needle:
        users = [
            user
            for user in users
            if any(
                needle in str(value).lower()
                for value in (user.first_name, user.last_name, user.telegram_username, user.telegram_id)
                if value is not None
            )
        ]
    return users, counts


async def approve_user(session: AsyncSession, admin_telegram_id: int, target_identifier: str) -> User:
    from app.services.audit import log_action

    admin = await get_user_by_telegram_id(session, admin_telegram_id)
    target = await get_user_by_identifier(session, target_identifier)
    if admin is None or target is None:
        raise NotFoundError("User not found")
    await _require_moderation_rights(session, admin, target)
    if target.status in (UserStatus.BANNED.value, UserStatus.DELETED.value) and not is_global_admin(admin):
        raise ForbiddenError("Only a main admin can restore a banned or deleted user")
    already_approved = target.status == UserStatus.APPROVED.value
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
    if not already_approved:
        _notify(session, target, "Ваша регистрация одобрена. Теперь доступны все функции AITU Music Club Bot.")
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
    _notify(session, target, "Вам выдали права админа в AITU Music Club Bot.")
    await session.commit()
    await session.refresh(target)
    return target


async def demote_admin(session: AsyncSession, admin_telegram_id: int, target_identifier: str) -> User:
    from app.services.audit import log_action

    admin = await get_user_by_telegram_id(session, admin_telegram_id)
    target = await get_user_by_identifier(session, target_identifier)
    if admin is None or target is None:
        raise NotFoundError("User not found")
    require_admin(admin)
    if target.telegram_id in settings.initial_admin_ids:
        raise ForbiddenError("Initial admin cannot be demoted")
    if target.telegram_id == admin.telegram_id:
        raise ForbiddenError("You cannot remove your own admin role")
    target.role = UserRole.USER.value
    if target.status != UserStatus.APPROVED.value:
        target.status = UserStatus.APPROVED.value
    await log_action(
        session,
        admin.id,
        "admin.remove",
        "user",
        target.telegram_id,
        f"Removed admin: @{target.telegram_username or target.telegram_id}",
    )
    _notify(session, target, "С вас сняли права админа в AITU Music Club Bot.")
    await session.commit()
    await session.refresh(target)
    return target


async def set_dsvr_role(session: AsyncSession, admin_telegram_id: int, target_identifier: str, enabled: bool) -> User:
    from app.services.audit import log_action

    admin = await get_user_by_telegram_id(session, admin_telegram_id)
    target = await get_user_by_identifier(session, target_identifier)
    if admin is None or target is None:
        raise NotFoundError("User not found")
    require_admin(admin)
    if target.role == UserRole.ADMIN.value:
        raise ConflictError("Admins already have full access; remove admin role first")
    if enabled:
        if target.status != UserStatus.APPROVED.value:
            raise ConflictError("User must be approved before becoming DSVR")
        target.role = UserRole.DSVR.value
    elif target.role == UserRole.DSVR.value:
        target.role = UserRole.USER.value
    await log_action(
        session,
        admin.id,
        "dsvr.add" if enabled else "dsvr.remove",
        "user",
        target.telegram_id,
        f"{'Назначил' if enabled else 'Снял'} ДСВР: @{target.telegram_username or target.telegram_id}",
    )
    _notify(session, target, "Вам выдана роль ДСВР в AITU Music Club Bot." if enabled else "С вас сняли роль ДСВР в AITU Music Club Bot.")
    await session.commit()
    await session.refresh(target)
    return target


async def list_admins(session: AsyncSession, admin_telegram_id: int) -> list[User]:
    admin = await get_user_by_telegram_id(session, admin_telegram_id)
    if admin is None:
        raise NotFoundError("Admin not found")
    require_admin(admin)
    result = await session.execute(
        select(User)
        .where(User.role == UserRole.ADMIN.value, User.status == UserStatus.APPROVED.value)
        .order_by(User.created_at.desc())
    )
    return list(result.scalars())


async def reject_user(session: AsyncSession, admin_telegram_id: int, target_identifier: str, reason: str | None) -> User:
    from app.services.audit import log_action

    admin = await get_user_by_telegram_id(session, admin_telegram_id)
    target = await get_user_by_identifier(session, target_identifier)
    if admin is None or target is None:
        raise NotFoundError("User not found")
    await _require_moderation_rights(session, admin, target)
    _guard_moderation_target(admin, target)
    _guard_banned_or_deleted(admin, target)
    target.status = UserStatus.REJECTED.value
    target.moderation_reason = reason
    target.registration_retry_after = now_local() + timedelta(hours=settings.registration_retry_hours)
    await log_action(
        session,
        admin.id,
        "user.reject",
        "user",
        target.telegram_id,
        f"Отклонил заявку: @{target.telegram_username or target.telegram_id}. Причина: {reason or '-'}",
    )
    retry_after = format_moment(target.registration_retry_after)
    _notify(
        session,
        target,
        "Ваша регистрация отклонена, но это не бан.\n"
        f"Повторно подать заявку можно после: {retry_after}.\n"
        "Когда срок пройдет, отправьте /start.",
    )
    await session.commit()
    await session.refresh(target)
    return target


async def _release_holdings(session: AsyncSession, target: User, actor: User, reason: str) -> None:
    """A banned or deleted member stops holding the room: cancel their future personal rehearsals, drop their waiting
    entries and close their open move requests. Band bookings and band membership stay with the band."""
    from app.db.models import Booking, BookingStatus, MoveRequest, MoveRequestStatus, WaitlistEntry
    from app.services.bookings import _lock_bookings, _offer_freed
    from app.services.schedule_history import record_change, snapshot

    await _lock_bookings(session)
    now = now_local()
    rows = await session.execute(
        select(Booking).where(
            Booking.created_by_id == target.id,
            Booking.band_id.is_(None),
            Booking.purpose == "personal",
            Booking.status == BookingStatus.CONFIRMED.value,
        )
        .options(selectinload(Booking.band), selectinload(Booking.created_by))
    )
    freed = []
    for booking in rows.scalars():
        if datetime.combine(booking.booking_date, booking.end_time) <= now:
            continue
        before = snapshot(booking)
        booking.status = BookingStatus.CANCELLED.value
        booking.cancelled_by_id = actor.id
        booking.cancel_reason = reason
        await record_change(session, booking, "cancelled", actor, before=before, reason=reason, notify=False)
        freed.append((booking.booking_date, booking.start_time, booking.end_time))

    await session.execute(delete(WaitlistEntry).where(WaitlistEntry.user_id == target.id))
    await session.execute(
        update(MoveRequest)
        .where(MoveRequest.requester_id == target.id, MoveRequest.status == MoveRequestStatus.PENDING.value)
        .values(status=MoveRequestStatus.CANCELLED.value, resolved_at=now, resolved_by_id=actor.id)
    )
    await session.flush()
    await _offer_freed(session, freed)


async def ban_user(session: AsyncSession, admin_telegram_id: int, target_identifier: str, reason: str | None) -> User:
    from app.services.audit import log_action

    admin = await get_user_by_telegram_id(session, admin_telegram_id)
    target = await get_user_by_identifier(session, target_identifier)
    if admin is None or target is None:
        raise NotFoundError("User not found")
    await _require_moderation_rights(session, admin, target)
    _guard_moderation_target(admin, target)
    await _release_holdings(session, target, admin, "Пользователь заблокирован")
    target.status = UserStatus.BANNED.value
    target.role = UserRole.USER.value
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
    await outbox.commit_and_publish(session)
    await session.refresh(target)
    return target


async def soft_delete_user(session: AsyncSession, admin_telegram_id: int, target_identifier: str, reason: str | None) -> User:
    from app.services.audit import log_action

    admin = await get_user_by_telegram_id(session, admin_telegram_id)
    target = await get_user_by_identifier(session, target_identifier)
    if admin is None or target is None:
        raise NotFoundError("User not found")
    await _require_moderation_rights(session, admin, target)
    _guard_moderation_target(admin, target)
    _guard_banned_or_deleted(admin, target)
    await _release_holdings(session, target, admin, "Пользователь удалён")
    target.status = UserStatus.DELETED.value
    target.role = UserRole.USER.value
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
    await outbox.commit_and_publish(session)
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
    if user.status == UserStatus.REJECTED.value and user.registration_retry_after and user.registration_retry_after > now_local():
        raise ConflictError(f"You can submit again after {format_moment(user.registration_retry_after)}")
    user.status = UserStatus.PENDING.value
    user.moderation_reason = None
    user.registration_retry_after = None
    await session.commit()
    await session.refresh(user)
    return user
