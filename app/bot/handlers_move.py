from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.handlers import answer_callback, api

router = Router()

ANSWERS = {"accept": "✅ Вы согласились, бронь перенесена", "decline": "❌ Вы отказали"}


@router.callback_query(F.data.regexp(r"^move:(accept|decline):\d+$"))
async def answer_move_request(callback: CallbackQuery) -> None:
    """The buttons of a "please move" notification."""
    _, action, request_id = callback.data.split(":")
    try:
        await api.answer_move_request(callback.from_user.id, int(request_id), action)
    except RuntimeError as exc:
        await answer_callback(callback, f"Не получилось: {exc}", show_alert=True)
        return
    try:
        await callback.message.edit_text(f"{callback.message.text}\n\n{ANSWERS[action]}", reply_markup=None)
    except Exception:
        await callback.message.answer(ANSWERS[action])
    await answer_callback(callback)
