from io import BytesIO

from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import BandMember, Event, EventApplication, EventApplicationMember
from app.services.errors import ConflictError, ForbiddenError, NotFoundError
from app.services.users import get_user_by_telegram_id, require_admin, require_approved


async def create_event(session: AsyncSession, telegram_id: int, **data) -> Event:
    from app.services.audit import log_action

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_approved(user)
    require_admin(user)
    event = Event(created_by_id=user.id, **data)
    session.add(event)
    await session.flush()
    await log_action(
        session,
        user.id,
        "event.create",
        "event",
        event.id,
        f"Создал концерт: {event.title} ({event.event_date.isoformat()})",
    )
    await session.commit()
    await session.refresh(event)
    return event


async def update_event(session: AsyncSession, event_id: int, telegram_id: int, **data) -> Event:
    from app.services.audit import log_action

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    result = await session.execute(select(Event).where(Event.id == event_id))
    event = result.scalar_one_or_none()
    if event is None:
        raise NotFoundError("Event not found")
    for key, value in data.items():
        setattr(event, key, value)
    changed = ", ".join(sorted(data.keys())) or "без изменений"
    await log_action(
        session,
        user.id,
        "event.update",
        "event",
        event.id,
        f"Обновил концерт #{event.id}: {changed}",
    )
    await session.commit()
    await session.refresh(event)
    return event


async def list_events(session: AsyncSession) -> list[Event]:
    result = await session.execute(select(Event).order_by(Event.event_date.desc()))
    return list(result.scalars())


async def submit_application(
    session: AsyncSession,
    event_id: int,
    telegram_id: int,
    band_id: int,
    song_title: str,
    member_ids: list[int],
) -> EventApplication:
    from app.services.audit import log_action

    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_approved(user)
    owner_result = await session.execute(
        select(BandMember).where(BandMember.band_id == band_id, BandMember.user_id == user.id, BandMember.is_owner.is_(True))
    )
    if owner_result.scalar_one_or_none() is None:
        raise ForbiddenError("Only band owner can submit lists")
    members_result = await session.execute(
        select(BandMember).where(BandMember.band_id == band_id, BandMember.id.in_(member_ids)).options(selectinload(BandMember.user))
    )
    members = list(members_result.scalars())
    if not members:
        raise ConflictError("Choose at least one member")
    incomplete = [m.user for m in members if not m.user.profile_completed]
    if incomplete:
        names = ", ".join(user.telegram_username or str(user.telegram_id) for user in incomplete)
        raise ConflictError(f"Some members have incomplete profiles: {names}")
    existing = await session.execute(select(EventApplication).where(EventApplication.event_id == event_id, EventApplication.band_id == band_id))
    application = existing.scalar_one_or_none()
    if application is None:
        application = EventApplication(event_id=event_id, band_id=band_id, submitted_by_id=user.id, song_title=song_title)
        session.add(application)
        await session.flush()
    else:
        application.song_title = song_title
        application.submitted_by_id = user.id
        old_members = await session.execute(select(EventApplicationMember).where(EventApplicationMember.application_id == application.id))
        for old_member in old_members.scalars():
            await session.delete(old_member)
    for member in members:
        session.add(EventApplicationMember(application_id=application.id, user_id=member.user_id, instrument_role=member.instrument_role))
    await log_action(
        session,
        user.id,
        "event.application.submit",
        "event_application",
        application.id,
        f"Подал номер на концерт #{event_id}: {song_title}",
    )
    await session.commit()
    await session.refresh(application)
    return application


async def list_event_applications(session: AsyncSession, telegram_id: int, event_id: int) -> list[dict]:
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise NotFoundError("User not found")
    require_admin(user)
    result = await session.execute(
        select(EventApplication)
        .where(EventApplication.event_id == event_id)
        .options(
            selectinload(EventApplication.band),
            selectinload(EventApplication.members).selectinload(EventApplicationMember.user),
        )
        .order_by(EventApplication.submitted_at.desc())
    )
    rows = []
    for application in result.scalars().unique():
        rows.append(
            {
                "id": application.id,
                "event_id": application.event_id,
                "band_id": application.band_id,
                "band_name": application.band.name,
                "song_title": application.song_title,
                "submitted_at": application.submitted_at.isoformat(),
                "members": [
                    {
                        "id": member.id,
                        "instrument_role": member.instrument_role,
                        "user": {
                            "telegram_id": member.user.telegram_id,
                            "telegram_username": member.user.telegram_username,
                            "first_name": member.user.first_name,
                            "last_name": member.user.last_name,
                            "study_group": member.user.study_group,
                        },
                    }
                    for member in application.members
                ],
            }
        )
    return rows


async def export_event_applications(session: AsyncSession, event_id: int) -> bytes:
    result = await session.execute(
        select(EventApplication)
        .where(EventApplication.event_id == event_id)
        .options(
            selectinload(EventApplication.event),
            selectinload(EventApplication.band),
            selectinload(EventApplication.members).selectinload(EventApplicationMember.user),
        )
    )
    applications = list(result.scalars())
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Participants"
    sheet.append(["Concert", "Band", "Song", "Last name", "First name", "Study group", "Role", "Telegram"])
    for application in applications:
        for member in application.members:
            user = member.user
            sheet.append(
                [
                    application.event.title,
                    application.band.name,
                    application.song_title,
                    user.last_name,
                    user.first_name,
                    user.study_group,
                    member.instrument_role,
                    f"@{user.telegram_username}" if user.telegram_username else "",
                ]
            )
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
