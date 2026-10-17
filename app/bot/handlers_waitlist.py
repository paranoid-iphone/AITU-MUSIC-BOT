from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.handlers import answer_callback, api

router = Router()


@router.callback_query(F.data.regexp(r"^waitlist:take:\d+$"))
async def take_waitlist(callback: CallbackQuery) -> None:
    """The button of a "time is free" notification: books the range the user was waiting for."""
    entry_id = int(callback.data.rsplit(":", maxsplit=1)[1])
    try:
        booking = await api.take_waitlist(callback.from_user.id, entry_id)
    except RuntimeError as exc:
        await answer_callback(callback, f"Не получилось: {exc}", show_alert=True)
        return
    day = booking["booking_date"][8:10] + "." + booking["booking_date"][5:7]
    done = f"✅ Забронировано: {day} {booking['start_time'][:5]}–{booking['end_time'][:5]}"
    try:
        await callback.message.edit_text(f"{callback.message.text}\n\n{done}", reply_markup=None)
    except Exception:
        await callback.message.answer(done)
    await answer_callback(callback)
