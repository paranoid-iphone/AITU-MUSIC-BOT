from datetime import datetime

from pydantic import BaseModel, Field


class UserTelegramUpsert(BaseModel):
    telegram_id: int
    telegram_username: str | None = None
    telegram_full_name: str | None = None


class UserProfileUpdate(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    study_group: str = Field(min_length=1, max_length=50)
    language: str = Field(pattern="^(ru|kz|en)$")


class UserSettingsUpdate(BaseModel):
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    study_group: str | None = Field(default=None, min_length=1, max_length=50)
    language: str | None = Field(default=None, pattern="^(ru|kz|en)$")
    notifications_enabled: bool | None = None


class UserRead(BaseModel):
    id: int
    telegram_id: int
    telegram_username: str | None
    telegram_full_name: str | None
    first_name: str | None
    last_name: str | None
    study_group: str | None
    language: str
    role: str
    status: str
    notifications_enabled: bool
    registration_retry_after: datetime | None = None
    profile_completed: bool

    model_config = {"from_attributes": True}


class UserModerationUpdate(BaseModel):
    admin_telegram_id: int
    target: str
    reason: str | None = Field(default=None, max_length=255)
