from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, StringConstraints

from app.schemas.users import UserRead


class GroupRead(BaseModel):
    id: int
    slug: str
    title: str

    model_config = {"from_attributes": True}


class BandResponsibleUpdate(BaseModel):
    requester_telegram_id: int
    member_id: int


class BandOwnerUpdate(BaseModel):
    requester_telegram_id: int
    member_id: int


class BandMemberRead(BaseModel):
    id: int
    instrument_role: str
    is_owner: bool
    notifications_enabled: bool
    user: UserRead

    model_config = {"from_attributes": True}


class BandRead(BaseModel):
    id: int
    name: str
    invite_code: str | None = None
    creator_id: int
    group_id: int | None = None
    responsible_user_id: int | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class BandDetail(BandRead):
    members: list[BandMemberRead]
    group: GroupRead | None = None
    responsible: UserRead | None = None


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]
Role = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class WebAppBandCreate(BaseModel):
    """Mini App payloads: who acts comes from the signed initData, never from the body."""

    name: Name
    instrument_role: Role = "owner"


class WebAppBandJoin(BaseModel):
    invite_code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30)]
    instrument_role: Role = "other"


class WebAppBandRename(BaseModel):
    name: Name


class WebAppMemberPick(BaseModel):
    member_id: int


class WebAppMemberRole(BaseModel):
    instrument_role: Role
