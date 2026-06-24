from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_internal_token
from app.db.session import get_session
from app.schemas.bands import BandCreate, BandDetail, BandJoin, BandRead, MemberRoleUpdate
from app.services import bands

router = APIRouter(dependencies=[Depends(verify_internal_token)])


@router.post("", response_model=BandRead)
async def create_band(payload: BandCreate, session: AsyncSession = Depends(get_session)):
    return await bands.create_band(session, payload.creator_telegram_id, payload.name, payload.creator_role)


@router.get("", response_model=list[BandDetail])
async def list_my_bands(telegram_id: int, session: AsyncSession = Depends(get_session)):
    return await bands.list_user_bands(session, telegram_id)


@router.post("/join", response_model=BandDetail)
async def join_band(payload: BandJoin, session: AsyncSession = Depends(get_session)):
    return await bands.join_band(session, payload.telegram_id, payload.invite_code, payload.instrument_role)


@router.get("/{band_id}", response_model=BandDetail)
async def get_band(band_id: int, session: AsyncSession = Depends(get_session)):
    return await bands.get_band_detail(session, band_id)


@router.patch("/{band_id}/members/{member_id}/role")
async def update_member_role(
    band_id: int,
    member_id: int,
    payload: MemberRoleUpdate,
    session: AsyncSession = Depends(get_session),
):
    return await bands.update_member_role(session, band_id, member_id, payload.requester_telegram_id, payload.instrument_role)
