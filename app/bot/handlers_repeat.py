from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.handlers import answer_callback, api

router = Router()


@router.callback_query(F.data.startswith("repeat:"))
async def repeat_booking(callback: CallbackQuery) -> None:
    """The one-tap button from a repeat offer: books the same slot one week later."""
    booking_id = int(callback.data.split(":", maxsplit=1)[1])
    try:
        booking = await api.repeat_booking(callback.from_user.id, booking_id)
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
