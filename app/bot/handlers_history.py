from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.handlers import answer_callback, api, edit_screen
from app.core.config import settings

router = Router()

ACTION_TITLES = {
    "created": "создал(а) бронь",
    "updated": "изменил(а) бронь",
    "cancelled": "отменил(а) бронь",
    "preempted": "вытеснил(а) бронь (ДСВР)",
    "auto_assigned": "авто-распределение",
}


def _local(iso_utc: str) -> datetime:
    return datetime.fromisoformat(iso_utc).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone))


def _slot(snap: dict | None) -> str:
    if not snap:
        return "-"
    day = date.fromisoformat(snap["date"]).strftime("%d.%m")
    title = f" · {snap['song_title']}" if snap.get("song_title") else ""
    return f"{day} {snap['start']}–{snap['end']}{title}"


def format_history(rows: list[dict], title: str) -> str:
    if not rows:
        return f"{title}\n\nИзменений пока нет."
    lines = [title, ""]
    for row in rows:
        stamp = _local(row["created_at"]).strftime("%d.%m %H:%M")
        action = ACTION_TITLES.get(row["action"], row["action"])
        lines.append(f"{stamp} · {row['actor_name']} {action} · {row.get('band_name') or '-'}")
        if row["action"] == "updated":
            lines.append(f"   {_slot(row.get('before'))} → {_slot(row.get('after'))}")
        elif row["action"] in {"cancelled", "preempted"}:
            lines.append(f"   {_slot(row.get('before') or row.get('after'))}")
        else:
            lines.append(f"   {_slot(row.get('after'))}")
        if row.get("reason"):
            lines.append(f"   Причина: {row['reason']}")
    text = "\n".join(lines)
    return text if len(text) <= 3900 else text[:3880].rstrip() + "\n…"


def _back_keyboard(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data=callback_data)]])


@router.callback_query(F.data == "sched_history:all")
async def schedule_history_all(callback: CallbackQuery) -> None:
    try:
        rows = await api.schedule_history(callback.from_user.id, limit=15)
    except RuntimeError:
        await answer_callback(callback, "Нет доступа", show_alert=True)
        return
    await edit_screen(callback, format_history(rows, "История расписания"), _back_keyboard("admin:back"))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("band_history:"))
async def band_history(callback: CallbackQuery) -> None:
    _, band_id_raw, origin = callback.data.split(":")
    band_id = int(band_id_raw)
    back = f"admin_band:{band_id}:0" if origin == "a" else f"user_band:{band_id}:0"
    try:
        rows = await api.schedule_history(callback.from_user.id, band_id=band_id, limit=15)
    except RuntimeError:
        await answer_callback(callback, "Нет доступа", show_alert=True)
        return
    await edit_screen(callback, format_history(rows, "История номера"), _back_keyboard(back))
    await answer_callback(callback)
