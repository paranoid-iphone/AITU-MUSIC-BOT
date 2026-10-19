from typing import Any

import httpx

from app.bot.i18n import t
from app.core.config import settings


class BackendUnavailable(RuntimeError):
    """The backend did not answer or failed on its own (5xx); the message is safe to show to a user."""


class BackendClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = settings.backend_base_url.rstrip("/")
        self.headers = {"X-Internal-Token": settings.internal_api_token}
        self._transport = transport  # tests only

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=self.headers, timeout=20, transport=self._transport
        ) as client:
            try:
                response = await client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                raise BackendUnavailable(t("ru", "error_backend_unavailable")) from exc
            if response.status_code >= 500:
                raise BackendUnavailable(t("ru", "error_backend_unavailable"))
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

    async def get_user(self, telegram_id: int, requester_telegram_id: int | None = None) -> dict[str, Any]:
        """`requester_telegram_id` is whoever asks; it defaults to the user themselves."""
        params = {"requester_telegram_id": requester_telegram_id if requester_telegram_id is not None else telegram_id}
        return await self.request("GET", f"/users/{telegram_id}", params=params)

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

    async def list_groups(self) -> list[dict[str, Any]]:
        return await self.request("GET", "/groups")

    async def group_reviewer_ids(self, group_id: int) -> list[int]:
        return await self.request("GET", f"/groups/{group_id}/reviewers")

    async def update_band_responsible(self, requester_telegram_id: int, band_id: int, member_id: int) -> dict[str, Any]:
        return await self.request(
            "PATCH",
            f"/bands/{band_id}/responsible",
            json={"requester_telegram_id": requester_telegram_id, "member_id": member_id},
        )

    async def list_bands(self, telegram_id: int) -> list[dict[str, Any]]:
        return await self.request("GET", "/bands", params={"telegram_id": telegram_id})

    async def list_all_bands(self, admin_telegram_id: int) -> list[dict[str, Any]]:
        return await self.request("GET", "/bands/admin", params={"admin_telegram_id": admin_telegram_id})

    async def get_band(self, band_id: int, requester_telegram_id: int) -> dict[str, Any]:
        return await self.request("GET", f"/bands/{band_id}", params={"requester_telegram_id": requester_telegram_id})

    async def transfer_band_owner(self, requester_telegram_id: int, band_id: int, member_id: int) -> dict[str, Any]:
        return await self.request(
            "PATCH",
            f"/bands/{band_id}/owner",
            json={"requester_telegram_id": requester_telegram_id, "member_id": member_id},
        )

    async def remove_band_member(self, requester_telegram_id: int, band_id: int, member_id: int) -> dict[str, Any]:
        return await self.request("DELETE", f"/bands/{band_id}/members/{member_id}", params={"requester_telegram_id": requester_telegram_id})

    async def planning_week(self) -> dict[str, Any]:
        return await self.request("GET", "/planning/week")

    async def planning_requests(self, telegram_id: int) -> list[dict[str, Any]]:
        return await self.request("GET", "/planning/requests", params={"telegram_id": telegram_id})

    async def create_planning_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", "/planning/requests", json=payload)

    async def delete_planning_request(self, telegram_id: int, request_id: int) -> dict[str, Any]:
        return await self.request("DELETE", f"/planning/requests/{request_id}", params={"telegram_id": telegram_id})

    async def planning_allocate(self, telegram_id: int, dry_run: bool = False) -> dict[str, Any]:
        return await self.request("POST", "/planning/allocate", json={"telegram_id": telegram_id, "dry_run": dry_run})

    async def schedule(self) -> list[dict[str, Any]]:
        return await self.request("GET", "/bookings/schedule")

    async def my_bookings(self, telegram_id: int) -> list[dict[str, Any]]:
        return await self.request("GET", "/bookings/mine", params={"telegram_id": telegram_id})

    async def repeat_booking(self, telegram_id: int, booking_id: int) -> dict[str, Any]:
        return await self.request("POST", f"/bookings/{booking_id}/repeat", json={"telegram_id": telegram_id})

    async def take_waitlist(self, telegram_id: int, entry_id: int) -> dict[str, Any]:
        return await self.request("POST", f"/waitlist/{entry_id}/take", json={"telegram_id": telegram_id})

    async def answer_move_request(self, telegram_id: int, request_id: int, action: str) -> dict[str, Any]:
        return await self.request("POST", f"/move-requests/{request_id}/{action}", json={"telegram_id": telegram_id})

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

    async def my_event_applications(self, telegram_id: int) -> list[dict[str, Any]]:
        return await self.request("GET", "/events/applications/mine", params={"telegram_id": telegram_id})

    async def get_event_application(self, telegram_id: int, application_id: int) -> dict[str, Any]:
        return await self.request("GET", f"/events/applications/{application_id}", params={"telegram_id": telegram_id})

    async def update_event_application(self, telegram_id: int, application_id: int, data: dict[str, Any]) -> dict[str, Any]:
        return await self.request("PATCH", f"/events/applications/{application_id}", json={"telegram_id": telegram_id, **data})

    async def delete_event_application(self, telegram_id: int, application_id: int) -> dict[str, Any]:
        return await self.request("DELETE", f"/events/applications/{application_id}", params={"telegram_id": telegram_id})

    async def list_event_applications(self, telegram_id: int, event_id: int) -> list[dict[str, Any]]:
        return await self.request("GET", f"/events/{event_id}/applications", params={"telegram_id": telegram_id})

    async def event_history(self, telegram_id: int) -> list[dict[str, Any]]:
        return await self.request("GET", "/events/history", params={"telegram_id": telegram_id})

    async def create_personal_event_history(self, telegram_id: int, data: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", "/events/history/personal", json={"telegram_id": telegram_id, **data})

    async def get_personal_event_history(self, telegram_id: int, item_id: int) -> dict[str, Any]:
        return await self.request("GET", f"/events/history/personal/{item_id}", params={"telegram_id": telegram_id})

    async def update_personal_event_history(self, telegram_id: int, item_id: int, data: dict[str, Any]) -> dict[str, Any]:
        return await self.request("PATCH", f"/events/history/personal/{item_id}", json={"telegram_id": telegram_id, **data})

    async def delete_personal_event_history(self, telegram_id: int, item_id: int) -> dict[str, Any]:
        return await self.request("DELETE", f"/events/history/personal/{item_id}", params={"telegram_id": telegram_id})

    async def schedule_day_closures(self) -> list[dict[str, Any]]:
        return await self.request("GET", "/bookings/day-closures")

    async def booking_history(self, telegram_id: int) -> list[dict[str, Any]]:
        return await self.request("GET", "/admin/bookings/history", params={"telegram_id": telegram_id})

    async def action_history(self, telegram_id: int, limit: int = 30) -> list[dict[str, Any]]:
        return await self.request("GET", "/admin/actions/history", params={"telegram_id": telegram_id, "limit": limit})
