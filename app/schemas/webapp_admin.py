from pydantic import BaseModel, Field


class WebAppModerate(BaseModel):
    reason: str | None = Field(default=None, max_length=255)


class WebAppRoleChange(BaseModel):
    target: str = Field(min_length=1, max_length=100)
    enabled: bool = True
