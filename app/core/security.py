import hmac

from fastapi import Header, HTTPException, status

from app.core.config import settings


async def verify_internal_token(x_internal_token: str | None = Header(default=None)) -> None:
    expected = settings.internal_api_token
    if not expected or not x_internal_token or not hmac.compare_digest(
        x_internal_token.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid internal token")
