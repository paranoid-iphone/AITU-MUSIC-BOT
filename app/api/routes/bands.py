from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_internal_token
from app.db.session import get_session
from app.schemas.bands import BandDetail, BandOwnerUpdate, BandResponsibleUpdate
from app.services import bands

router = APIRouter(dependencies=[Depends(verify_internal_token)])


async def _with_visible_invite_code(session: AsyncSession, viewer_telegram_id: int, items: list) -> list[BandDetail]:
    visible = await bands.invite_code_visible_ids(session, viewer_telegram_id, items)
    result = []
    for band in items:
        detail = BandDetail.model_validate(band)
        if band.id not in visible:
            detail.invite_code = None
        result.append(detail)
    return result


@router.get("", response_model=list[BandDetail])
async def list_my_bands(telegram_id: int, session: AsyncSession = Depends(get_session)):
    return await _with_visible_invite_code(session, telegram_id, await bands.list_user_bands(session, telegram_id))


@router.get("/admin", response_model=list[BandDetail])
async def list_all_bands(admin_telegram_id: int, session: AsyncSession = Depends(get_session)):
    return await bands.list_all_bands(session, admin_telegram_id)


@router.get("/{band_id}", response_model=BandDetail)
async def get_band(band_id: int, requester_telegram_id: int, session: AsyncSession = Depends(get_session)):
    band = await bands.get_band_for_requester(session, band_id, requester_telegram_id)
    return (await _with_visible_invite_code(session, requester_telegram_id, [band]))[0]


@router.patch("/{band_id}/owner", response_model=BandDetail)
async def transfer_owner(band_id: int, payload: BandOwnerUpdate, session: AsyncSession = Depends(get_session)):
    return await bands.transfer_band_owner(session, band_id, payload.requester_telegram_id, payload.member_id)


@router.patch("/{band_id}/responsible", response_model=BandDetail)
async def update_responsible(band_id: int, payload: BandResponsibleUpdate, session: AsyncSession = Depends(get_session)):
    return await bands.update_band_responsible(session, band_id, payload.requester_telegram_id, payload.member_id)


@router.delete("/{band_id}/members/{member_id}")
async def remove_member(
    band_id: int,
    member_id: int,
    requester_telegram_id: int,
    session: AsyncSession = Depends(get_session),
):
    return await bands.remove_band_member(session, band_id, member_id, requester_telegram_id)
