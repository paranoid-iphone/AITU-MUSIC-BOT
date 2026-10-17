from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_internal_token
from app.db.session import get_session
from app.schemas.bands import GroupRead
from app.schemas.users import AdminRoleUpdate, UserRead
from app.services import groups, outbox

router = APIRouter(dependencies=[Depends(verify_internal_token)])


@router.get("", response_model=list[GroupRead])
async def list_groups(session: AsyncSession = Depends(get_session)):
    return await groups.list_groups(session)


@router.get("/{group_id}/admins", response_model=list[UserRead])
async def list_group_admins(group_id: int, telegram_id: int, session: AsyncSession = Depends(get_session)):
    return await groups.list_group_admins(session, telegram_id, group_id)


@router.post("/{group_id}/admins", response_model=UserRead)
async def add_group_admin(group_id: int, payload: AdminRoleUpdate, session: AsyncSession = Depends(get_session)):
    user = await groups.add_group_admin(session, payload.admin_telegram_id, group_id, payload.target)
    await outbox.commit_and_publish(session)
    return user


@router.delete("/{group_id}/admins", response_model=UserRead)
async def remove_group_admin(
    group_id: int, telegram_id: int, target: str, session: AsyncSession = Depends(get_session)
):
    return await groups.remove_group_admin(session, telegram_id, group_id, target)


@router.get("/{group_id}/reviewers")
async def registration_reviewers(group_id: int, session: AsyncSession = Depends(get_session)):
    return await groups.registration_reviewer_ids(session, group_id)
