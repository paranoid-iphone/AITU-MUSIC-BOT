from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.users import UserRead


class BandCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    creator_telegram_id: int
    creator_role: str = Field(default="owner", max_length=80)


class BandJoin(BaseModel):
    telegram_id: int
    invite_code: str
    instrument_role: str = Field(default="other", max_length=80)


class MemberRoleUpdate(BaseModel):
    requester_telegram_id: int
    instrument_role: str = Field(min_length=1, max_length=80)


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
    invite_code: str
    creator_id: int
    created_at: datetime

    model_config = {"from_attributes": True}


class BandDetail(BandRead):
    members: list[BandMemberRead]
