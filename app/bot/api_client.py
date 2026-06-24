from datetime import date, time
from typing import Any

import httpx

from app.core.config import settings


class BackendClient:
    def __init__(self) -> None:
        self.base_url = settings.backend_base_url.rstrip("/")
        self.headers = {"X-Internal-Token": settings.internal_api_token}

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        async with httpx.AsyncClient(base_url=self.base_url, headers=self.headers, timeout=20) as client:
            response = await client.request(method, path, **kwargs)
            if response.status_code >= 400:
                try:
                    detail = response.json().get("detail", response.text)
                except ValueError:
                    detail = response.text
                raise RuntimeError(str(detail))
            if not response.content:
                return None
            return response.json()

    async def upsert_user(self, message_user) -> dict[str, Any]:
        full_name = " ".join(part for part in [message_user.first_name, message_user.last_name] if part)
        return await self.request(
            "POST",
            "/users/telegram",
            json={
                "telegram_id": message_user.id,
                "telegram_username": message_user.username,
                "telegram_full_name": full_name or None,
            },
        )

    async def get_user(self, telegram_id: int) -> dict[str, Any]:
        return await self.request("GET", f"/users/{telegram_id}")

    async def update_profile(self, telegram_id: int, data: dict[str, Any]) -> dict[str, Any]:
        return await self.request("PATCH", f"/users/{telegram_id}/profile", json=data)

    async def update_settings(self, telegram_id: int, data: dict[str, Any]) -> dict[str, Any]:
        return await self.request("PATCH", f"/users/{telegram_id}/settings", json=data)

    async def resubmit_registration(self, telegram_id: int) -> dict[str, Any]:
        return await self.request("POST", f"/users/{telegram_id}/resubmit")

    async def list_users(self, admin_telegram_id: int, status: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"admin_telegram_id": admin_telegram_id}
        if status:
            params["status"] = status
        return await self.request("GET", "/users", params=params)

    async def moderate_user(self, action: str, admin_telegram_id: int, target: str, reason: str | None = None) -> dict[str, Any]:
        return await self.request(
            "POST",
            f"/users/{action}",
            json={"admin_telegram_id": admin_telegram_id, "target": target, "reason": reason},
        )

    async def promote_admin(self, admin_telegram_id: int, target: str) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/users/promote-admin",
            json={"admin_telegram_id": admin_telegram_id, "target": target},
        )

    async def create_band(self, telegram_id: int, name: str, creator_role: str) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/bands",
            json={"creator_telegram_id": telegram_id, "name": name, "creator_role": creator_role},
        )

    async def list_bands(self, telegram_id: int) -> list[dict[str, Any]]:
        return await self.request("GET", "/bands", params={"telegram_id": telegram_id})

    async def join_band(self, telegram_id: int, invite_code: str, instrument_role: str) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/bands/join",
            json={"telegram_id": telegram_id, "invite_code": invite_code, "instrument_role": instrument_role},
        )

    async def available_slots(self, target_date: date, duration_minutes: int = 60, band_id: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"target_date": target_date.isoformat(), "duration_minutes": duration_minutes}
        if band_id is not None:
            params["band_id"] = band_id
        return await self.request(
            "GET",
            "/bookings/available-slots",
            params=params,
        )

    async def booking_limit_status(self, telegram_id: int, band_id: int, target_date: date | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"telegram_id": telegram_id, "band_id": band_id}
        if target_date is not None:
            params["target_date"] = target_date.isoformat()
        return await self.request("GET", "/bookings/limit-status", params=params)

    async def schedule(self) -> list[dict[str, Any]]:
        return await self.request("GET", "/bookings/schedule")

    async def create_booking(
        self,
        telegram_id: int,
        band_id: int,
        target_date: date,
        start: time,
        end: time,
        purpose: str,
        song_title: str | None,
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/bookings",
            json={
                "telegram_id": telegram_id,
                "band_id": band_id,
                "booking_date": target_date.isoformat(),
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "purpose": purpose,
                "song_title": song_title,
            },
        )

    async def create_staff_booking(
        self,
        telegram_id: int,
        target_date: date,
        start: time,
        end: time,
        title: str,
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/bookings/staff",
            json={
                "telegram_id": telegram_id,
                "booking_date": target_date.isoformat(),
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "title": title,
            },
        )

    async def delete_booking(self, telegram_id: int, booking_id: int) -> dict[str, Any]:
        return await self.request("DELETE", f"/bookings/{booking_id}", params={"telegram_id": telegram_id})

    async def list_events(self) -> list[dict[str, Any]]:
        return await self.request("GET", "/events")

    async def create_event(self, telegram_id: int, data: dict[str, Any]) -> dict[str, Any]:
        payload = {"telegram_id": telegram_id, **data}
        return await self.request("POST", "/events", json=payload)

    async def update_event(self, telegram_id: int, event_id: int, data: dict[str, Any]) -> dict[str, Any]:
        payload = {"telegram_id": telegram_id, **data}
        return await self.request("PATCH", f"/events/{event_id}", json=payload)

    async def submit_application(self, telegram_id: int, event_id: int, band_id: int, song_title: str, member_ids: list[int]) -> dict[str, Any]:
        return await self.request(
            "POST",
            f"/events/{event_id}/applications",
            json={"telegram_id": telegram_id, "band_id": band_id, "song_title": song_title, "member_ids": member_ids},
        )

    async def list_event_applications(self, telegram_id: int, event_id: int) -> list[dict[str, Any]]:
        return await self.request("GET", f"/events/{event_id}/applications", params={"telegram_id": telegram_id})

    async def update_day(self, telegram_id: int, day_of_week: int, is_enabled: bool) -> dict[str, Any]:
        return await self.request(
            "PUT",
            "/admin/days",
            json={"telegram_id": telegram_id, "day_of_week": day_of_week, "is_enabled": is_enabled},
        )

    async def get_weekly_booking_limit(self, telegram_id: int) -> dict[str, Any]:
        return await self.request("GET", "/admin/settings/weekly-booking-limit", params={"telegram_id": telegram_id})

    async def update_weekly_booking_limit(self, telegram_id: int, hours: float) -> dict[str, Any]:
        return await self.request(
            "PUT",
            "/admin/settings/weekly-booking-limit",
            json={"telegram_id": telegram_id, "hours": hours},
        )

    async def get_daily_booking_limit(self, telegram_id: int) -> dict[str, Any]:
        return await self.request("GET", "/admin/settings/daily-booking-limit", params={"telegram_id": telegram_id})

    async def update_daily_booking_limit(self, telegram_id: int, hours: float) -> dict[str, Any]:
        return await self.request(
            "PUT",
            "/admin/settings/daily-booking-limit",
            json={"telegram_id": telegram_id, "hours": hours},
        )

    async def get_booking_window(self, telegram_id: int) -> dict[str, Any]:
        return await self.request("GET", "/admin/settings/booking-window", params={"telegram_id": telegram_id})

    async def update_booking_window(self, telegram_id: int, start: time, end: time) -> dict[str, Any]:
        return await self.request(
            "PUT",
            "/admin/settings/booking-window",
            json={"telegram_id": telegram_id, "start_time": start.isoformat(), "end_time": end.isoformat()},
        )

    async def booking_history(self, telegram_id: int) -> list[dict[str, Any]]:
        return await self.request("GET", "/admin/bookings/history", params={"telegram_id": telegram_id})

    async def action_history(self, telegram_id: int, limit: int = 30) -> list[dict[str, Any]]:
        return await self.request("GET", "/admin/actions/history", params={"telegram_id": telegram_id, "limit": limit})
