from datetime import date, datetime, timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.handlers import answer_callback, api, edit_screen, parse_booking_window
from app.bot.keyboards import flow_cancel_keyboard
from app.bot.states import DsvrFlow
from app.core.timezone import now_local

router = Router()

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _dates_keyboard(days: int = 14) -> InlineKeyboardMarkup:
    today = now_local().date()
    buttons = []
    for offset in range(days):
        day = today + timedelta(days=offset)
        buttons.append(
            InlineKeyboardButton(text=f"{WEEKDAYS[day.weekday()]} {day.strftime('%d.%m')}", callback_data=f"dsvr:date:{day.isoformat()}")
        )
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton(text="Назад в меню", callback_data="flow:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _confirm_keyboard(count: int) -> InlineKeyboardMarkup:
    label = f"Занять и отменить броней: {count}" if count else "Занять время"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data="dsvr:confirm")],
            [InlineKeyboardButton(text="Отмена", callback_data="flow:cancel")],
        ]
    )


def _format_affected(rows: list[dict]) -> str:
    lines = []
    for row in rows:
        who = row["band_name"]
        if row.get("responsible_name"):
            who += f" (отв.: {row['responsible_name']})"
        lines.append(f"- {row['start_time'][:5]}–{row['end_time'][:5]} · {who} · {row.get('song_title') or 'без названия'}")
    return "\n".join(lines)


@router.callback_query(F.data == "dsvr:start")
async def dsvr_start(callback: CallbackQuery, state: FSMContext) -> None:
    user = await api.get_user(callback.from_user.id)
    if user.get("role") not in {"admin", "dsvr"}:
        await answer_callback(callback, "Нет доступа", show_alert=True)
        return
    await state.clear()
    await state.set_state(DsvrFlow.date)
    await edit_screen(
        callback,
        "Занять кабинет с приоритетом.\nПересекающиеся брони будут отменены, их ответственным придет уведомление.\n\nВыберите дату:",
        _dates_keyboard(),
    )
    await answer_callback(callback)


@router.callback_query(DsvrFlow.date, F.data.startswith("dsvr:date:"))
async def dsvr_pick_date(callback: CallbackQuery, state: FSMContext) -> None:
    day = callback.data.rsplit(":", maxsplit=1)[-1]
    await state.update_data(dsvr_date=day)
    await state.set_state(DsvrFlow.time)
    await callback.message.answer(
        f"Дата: {date.fromisoformat(day).strftime('%d.%m.%Y')}\nВведите время в формате 14:00-15:30 (шаг 30 минут):",
        reply_markup=flow_cancel_keyboard(),
    )
    await answer_callback(callback)


@router.message(DsvrFlow.time)
async def dsvr_pick_time(message: Message, state: FSMContext) -> None:
    parsed = parse_booking_window(message.text or "")
    if parsed is None:
        await message.answer("Введите время в формате 14:00-15:30.", reply_markup=flow_cancel_keyboard())
        return
    start, end = parsed
    if start.time() >= end.time():
        await message.answer("Начало должно быть раньше конца.", reply_markup=flow_cancel_keyboard())
        return
    if start.minute not in {0, 30} or end.minute not in {0, 30}:
        await message.answer("Время должно идти по полчаса: 14:00, 14:30, 15:00.", reply_markup=flow_cancel_keyboard())
        return
    await state.update_data(dsvr_start=start.time().isoformat(), dsvr_end=end.time().isoformat())
    await state.set_state(DsvrFlow.title)
    await message.answer("Введите название мероприятия / причину:", reply_markup=flow_cancel_keyboard())


@router.message(DsvrFlow.title)
async def dsvr_pick_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title or len(title) > 150:
        await message.answer("Название должно быть от 1 до 150 символов.", reply_markup=flow_cancel_keyboard())
        return
    data = await state.get_data()
    try:
        affected = await api.priority_preview(message.from_user.id, data["dsvr_date"], data["dsvr_start"], data["dsvr_end"])
    except RuntimeError as exc:
        await state.clear()
        await message.answer(f"Не получилось занять время: {exc}")
        return
    await state.update_data(dsvr_title=title)
    await state.set_state(DsvrFlow.confirm)
    text = (
        f"{date.fromisoformat(data['dsvr_date']).strftime('%d.%m.%Y')} {data['dsvr_start'][:5]}–{data['dsvr_end'][:5]}\n"
        f"Название: {title}\n\n"
    )
    text += ("Будут отменены брони:\n" + _format_affected(affected)) if affected else "Время свободно, броней отменять не нужно."
    await message.answer(text, reply_markup=_confirm_keyboard(len(affected)))


@router.callback_query(DsvrFlow.confirm, F.data == "dsvr:confirm")
async def dsvr_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    try:
        result = await api.create_priority_booking(
            callback.from_user.id, data["dsvr_date"], data["dsvr_start"], data["dsvr_end"], data["dsvr_title"]
        )
    except RuntimeError as exc:
        await edit_screen(callback, f"Не получилось занять время: {exc}")
        await answer_callback(callback)
        return
    cancelled = result["cancelled"]
    text = f"Кабинет занят: {data['dsvr_date']} {data['dsvr_start'][:5]}–{data['dsvr_end'][:5]} · {data['dsvr_title']}"
    if cancelled:
        text += f"\n\nОтменено броней: {len(cancelled)}\n" + _format_affected(cancelled) + "\n\nОтветственным отправлены уведомления."
    await edit_screen(callback, text)
    await answer_callback(callback, "Готово")


@router.callback_query(F.data.in_({"dsvr:grant", "dsvr:revoke"}))
async def dsvr_role_start(callback: CallbackQuery, state: FSMContext) -> None:
    user = await api.get_user(callback.from_user.id)
    if user.get("role") != "admin":
        await answer_callback(callback, "Нет доступа", show_alert=True)
        return
    grant = callback.data == "dsvr:grant"
    await state.set_state(DsvrFlow.grant_target if grant else DsvrFlow.revoke_target)
    await callback.message.answer(
        "Отправьте @username или Telegram ID, кого " + ("назначить ДСВР:" if grant else "снять с роли ДСВР:"),
        reply_markup=flow_cancel_keyboard(),
    )
    await answer_callback(callback)


async def _set_role(message: Message, state: FSMContext, enabled: bool) -> None:
    try:
        user = await api.set_dsvr(message.from_user.id, (message.text or "").strip(), enabled)
    except RuntimeError as exc:
        await message.answer(f"Не получилось: {exc}", reply_markup=flow_cancel_keyboard())
        return
    await state.clear()
    name = f"@{user['telegram_username']}" if user.get("telegram_username") else str(user["telegram_id"])
    await message.answer(f"ДСВР назначен: {name}" if enabled else f"Роль ДСВР снята: {name}")
    try:
        await message.bot.send_message(
            user["telegram_id"],
            "Вам выдана роль ДСВР: откройте /start и кнопку «Админ»." if enabled else "Роль ДСВР снята.",
        )
    except Exception:
        pass


@router.message(DsvrFlow.grant_target)
async def dsvr_grant(message: Message, state: FSMContext) -> None:
    await _set_role(message, state, True)


@router.message(DsvrFlow.revoke_target)
async def dsvr_revoke(message: Message, state: FSMContext) -> None:
    await _set_role(message, state, False)
