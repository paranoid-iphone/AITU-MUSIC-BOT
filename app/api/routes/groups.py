from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_internal_token
from app.db.session import get_session
from app.schemas.bands import GroupRead
from app.services import groups

router = APIRouter(dependencies=[Depends(verify_internal_token)])


@router.get("", response_model=list[GroupRead])
async def list_groups(session: AsyncSession = Depends(get_session)):
    return await groups.list_groups(session)


@router.get("/{group_id}/reviewers")
async def registration_reviewers(group_id: int, session: AsyncSession = Depends(get_session)):
    return await groups.registration_reviewer_ids(session, group_id)
