from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_internal_token
from app.db.session import get_session
from app.schemas.events import EventApplicationCreate, EventApplicationRead, EventCreate, EventRead, EventUpdate
from app.services import events

router = APIRouter(dependencies=[Depends(verify_internal_token)])


@router.post("", response_model=EventRead)
async def create_event(payload: EventCreate, session: AsyncSession = Depends(get_session)):
    data = payload.model_dump(exclude={"telegram_id"})
    return await events.create_event(session, payload.telegram_id, **data)


@router.get("", response_model=list[EventRead])
async def list_events(session: AsyncSession = Depends(get_session)):
    return await events.list_events(session)


@router.patch("/{event_id}", response_model=EventRead)
async def update_event(event_id: int, payload: EventUpdate, session: AsyncSession = Depends(get_session)):
    data = payload.model_dump(exclude={"telegram_id"}, exclude_unset=True)
    return await events.update_event(session, event_id, payload.telegram_id, **data)


@router.post("/{event_id}/applications", response_model=EventApplicationRead)
async def submit_application(event_id: int, payload: EventApplicationCreate, session: AsyncSession = Depends(get_session)):
    return await events.submit_application(session, event_id, payload.telegram_id, payload.band_id, payload.song_title, payload.member_ids)


@router.get("/{event_id}/applications")
async def list_applications(event_id: int, telegram_id: int, session: AsyncSession = Depends(get_session)):
    return await events.list_event_applications(session, telegram_id, event_id)


@router.get("/{event_id}/export.xlsx")
async def export_event(event_id: int, session: AsyncSession = Depends(get_session)):
    content = await events.export_event_applications(session, event_id)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="event-{event_id}-participants.xlsx"'},
    )
