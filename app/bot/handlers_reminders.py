from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.handlers import answer_callback, api

router = Router()

TURNED_OFF = "🔕 Напоминания о репетициях выключены. Включить снова: Профиль → «Напоминания вкл/выкл»."


@router.callback_query(F.data == "reminders:off")
async def turn_reminders_off(callback: CallbackQuery) -> None:
    """The button under a rehearsal reminder (services/reminders.py): switches the person's reminders off."""
    try:
        await api.update_settings(callback.from_user.id, {"rehearsal_reminders_enabled": False})
    except RuntimeError as exc:
        await answer_callback(callback, f"Не получилось: {exc}", show_alert=True)
        return
    try:
        await callback.message.edit_text(f"{callback.message.text}\n\n{TURNED_OFF}", reply_markup=None)
    except Exception:
        await callback.message.answer(TURNED_OFF)
    await answer_callback(callback)
