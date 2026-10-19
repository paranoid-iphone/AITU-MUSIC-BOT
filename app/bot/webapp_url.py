from __future__ import annotations

from datetime import datetime, timedelta

import httpx

from app.core.config import settings

_cached_url: str | None = None
_cached_until: datetime | None = None


async def get_webapp_base_url() -> str | None:
    global _cached_url, _cached_until

    if settings.webapp_base_url:
        return settings.webapp_base_url.rstrip("/")

    domain = _ngrok_domain()
    if domain:
        return f"https://{domain}"

    now = datetime.utcnow()
    if _cached_until and _cached_until > now:
        return _cached_url

    _cached_url = await _fetch_ngrok_url()
    _cached_until = now + timedelta(seconds=60)
    return _cached_url


def _ngrok_domain() -> str:
    """The reserved ngrok domain without scheme and slashes ("" when not set)."""
    value = settings.ngrok_domain.strip()
    for scheme in ("https://", "http://"):
        if value.lower().startswith(scheme):
            value = value[len(scheme):]
    return value.strip("/")


async def _fetch_ngrok_url() -> str | None:
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(settings.ngrok_api_url)
            response.raise_for_status()
    except httpx.HTTPError:
        return None

    tunnels = response.json().get("tunnels", [])
    for tunnel in tunnels:
        public_url = tunnel.get("public_url", "")
        if public_url.startswith("https://"):
            return public_url.rstrip("/")
    return None
