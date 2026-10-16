from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.handlers import answer_callback, api, edit_screen, parse_booking_window
from app.bot.keyboards import flow_cancel_keyboard
from app.bot.states import PlanFlow

router = Router()

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
DURATIONS = [60, 90, 120, 150, 180, 240]
MAX_PRIORITIES = 5
PURPOSES = {"concert": "к концерту", "self": "для себя"}
STATUS_LABELS = {"pending": "ожидает распределения", "assigned": "назначено", "unassigned": "места не нашлось"}


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def _duration_label(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    return f"{hours} ч" + (f" {rest} мин" if rest else "")


def _dm(iso_date: str) -> str:
    return datetime.fromisoformat(iso_date).strftime("%d.%m")


def _windows_text(windows: list[dict]) -> str:
    return "\n".join(
        f"  {w['priority'] if 'priority' in w else index}) {WEEKDAYS[w['day_of_week']]} {w['start_time'][:5]}–{w['end_time'][:5]}"
        for index, w in enumerate(windows, start=1)
    )


def _week_title(week: dict) -> str:
    return f"{_dm(week['week_start'])}–{_dm(week['week_end'])}"


def _deadline_text(week: dict) -> str:
    return datetime.fromisoformat(week["deadline"]).strftime("%d.%m в %H:%M")


# ---------- member side ----------


@router.callback_query(F.data == "plan:start")
async def plan_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    week = await api.planning_week()
    if not week["is_open"]:
        reason = "расписание уже составлено" if week["published"] else "срок приёма вышел"
        await edit_screen(
            callback,
            f"Заявки на неделю {_week_title(week)} закрыты: {reason}.\n"
            "Заявки на следующую неделю откроются в понедельник. Свободные слоты текущего расписания можно бронировать вручную.",
            _kb([_button("К репетициям", "menu_section:rehearsals")]),
        )
        await answer_callback(callback)
        return
    await edit_screen(
        callback,
        f"Заявка на неделю {_week_title(week)}\n"
        f"Приём заявок до {_deadline_text(week)}, затем система сама распределит кабинет между всеми номерами.\n\n"
        "Укажите, когда вам удобно, и до 5 вариантов по приоритету: система постарается дать самый высокий из возможных.",
        _kb(
            [_button("Новая заявка", "plan:new")],
            [_button("Мои заявки", "plan:mine")],
            [_button("К репетициям", "menu_section:rehearsals")],
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data == "plan:new")
async def plan_new(callback: CallbackQuery, state: FSMContext) -> None:
    me = await api.get_user(callback.from_user.id)
    bands = [
        band
        for band in await api.list_bands(callback.from_user.id)
        if band.get("responsible_user_id") == me["id"]
        or any(member.get("is_owner") and member["user"]["telegram_id"] == callback.from_user.id for member in band["members"])
    ]
    if not bands:
        await answer_callback(callback, "Подать заявку может лидер или ответственный номера.", show_alert=True)
        return
    await state.clear()
    await state.set_state(PlanFlow.band)
    rows = [[_button(band["name"], f"plan:band:{band['id']}")] for band in bands]
    rows.append([_button("Отмена", "flow:cancel")])
    await edit_screen(callback, "Для какого номера заявка?", InlineKeyboardMarkup(inline_keyboard=rows))
    await answer_callback(callback)


@router.callback_query(PlanFlow.band, F.data.startswith("plan:band:"))
async def plan_pick_band(callback: CallbackQuery, state: FSMContext) -> None:
    band_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    band = await api.get_band(band_id)
    await state.update_data(plan_band_id=band_id, plan_band_name=band["name"], plan_windows=[])
    await state.set_state(PlanFlow.duration)
    rows = [[_button(_duration_label(m), f"plan:dur:{m}") for m in DURATIONS[i : i + 3]] for i in range(0, len(DURATIONS), 3)]
    rows.append([_button("Отмена", "flow:cancel")])
    await edit_screen(callback, f"Номер: {band['name']}\nСколько длится репетиция?", InlineKeyboardMarkup(inline_keyboard=rows))
    await answer_callback(callback)


def _days_keyboard(priority: int) -> InlineKeyboardMarkup:
    days = [_button(name, f"plan:day:{index}") for index, name in enumerate(WEEKDAYS)]
    return _kb(days[:4], days[4:], [_button("Отмена", "flow:cancel")])


@router.callback_query(PlanFlow.duration, F.data.startswith("plan:dur:"))
async def plan_pick_duration(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(plan_duration=int(callback.data.rsplit(":", maxsplit=1)[-1]))
    await state.set_state(PlanFlow.day)
    await edit_screen(callback, "Приоритет 1 — самый желанный вариант.\nВыберите день недели:", _days_keyboard(1))
    await answer_callback(callback)


@router.callback_query(PlanFlow.day, F.data.startswith("plan:day:"))
async def plan_pick_day(callback: CallbackQuery, state: FSMContext) -> None:
    day = int(callback.data.rsplit(":", maxsplit=1)[-1])
    data = await state.get_data()
    priority = len(data.get("plan_windows", [])) + 1
    await state.update_data(plan_day=day)
    await state.set_state(PlanFlow.time)
    await callback.message.answer(
        f"Приоритет {priority}: {WEEKDAYS[day]}.\nВ какое время вам удобно? Напишите промежуток, например 18:00-21:00.",
        reply_markup=flow_cancel_keyboard(),
    )
    await answer_callback(callback)


@router.message(PlanFlow.time)
async def plan_enter_time(message: Message, state: FSMContext) -> None:
    parsed = parse_booking_window(message.text or "")
    data = await state.get_data()
    if parsed is None:
        await message.answer("Введите время в формате 18:00-21:00.", reply_markup=flow_cancel_keyboard())
        return
    start, end = parsed
    if start.time() >= end.time() or start.minute not in {0, 30} or end.minute not in {0, 30}:
        await message.answer("Начало должно быть раньше конца, время — по полчаса: 14:00, 14:30.", reply_markup=flow_cancel_keyboard())
        return
    if (end - start).seconds // 60 < data["plan_duration"]:
        await message.answer(
            f"Этот промежуток короче репетиции ({_duration_label(data['plan_duration'])}). Укажите более широкий.",
            reply_markup=flow_cancel_keyboard(),
        )
        return
    windows = list(data.get("plan_windows", []))
    windows.append({"day_of_week": data["plan_day"], "start_time": start.time().isoformat(), "end_time": end.time().isoformat()})
    await state.update_data(plan_windows=windows)
    summary = f"Ваши варианты:\n{_windows_text(windows)}"
    if len(windows) < MAX_PRIORITIES:
        await state.set_state(PlanFlow.more)
        await message.answer(
            summary + "\n\nДобавить запасной вариант? Он пригодится, если основное время займут.",
            reply_markup=_kb([_button(f"Добавить вариант {len(windows) + 1}", "plan:more")], [_button("Достаточно", "plan:done")]),
        )
    else:
        await _ask_purpose(message, state, summary)


async def _ask_purpose(message: Message, state: FSMContext, summary: str) -> None:
    await state.set_state(PlanFlow.purpose)
    await message.answer(
        summary + "\n\nДля чего репетиция?",
        reply_markup=_kb([_button(label, f"plan:purpose:{key}") for key, label in PURPOSES.items()], [_button("Отмена", "flow:cancel")]),
    )


@router.callback_query(PlanFlow.more, F.data == "plan:more")
async def plan_more(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    priority = len(data.get("plan_windows", [])) + 1
    await state.set_state(PlanFlow.day)
    await callback.message.answer(f"Приоритет {priority}. Выберите день недели:", reply_markup=_days_keyboard(priority))
    await answer_callback(callback)


@router.callback_query(PlanFlow.more, F.data == "plan:done")
async def plan_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await _ask_purpose(callback.message, state, f"Ваши варианты:\n{_windows_text(data['plan_windows'])}")
    await answer_callback(callback)


@router.callback_query(PlanFlow.purpose, F.data.startswith("plan:purpose:"))
async def plan_purpose(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(plan_purpose=callback.data.rsplit(":", maxsplit=1)[-1])
    await state.set_state(PlanFlow.song)
    await callback.message.answer("Введите название песни или номера. Чтобы пропустить, отправьте '-'.", reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.message(PlanFlow.song)
async def plan_song(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if len(raw) > 150:
        await message.answer("Название не длиннее 150 символов.", reply_markup=flow_cancel_keyboard())
        return
    data = await state.get_data()
    try:
        created = await api.create_planning_request(
            {
                "telegram_id": message.from_user.id,
                "band_id": data["plan_band_id"],
                "duration_minutes": data["plan_duration"],
                "purpose": data["plan_purpose"],
                "song_title": None if raw in {"", "-"} else raw,
                "windows": data["plan_windows"],
            }
        )
    except RuntimeError as exc:
        await state.clear()
        await message.answer(f"Заявка не принята: {exc}", reply_markup=_kb([_button("Попробовать снова", "plan:start")]))
        return
    await state.clear()
    await message.answer(
        f"Заявка принята: {created['band_name']}, {_duration_label(created['duration_minutes'])}\n{_windows_text(created['windows'])}\n\n"
        "В воскресенье система распределит кабинет, и вы получите уведомление с итоговым временем.",
        reply_markup=_kb([_button("Ещё одна заявка", "plan:new")], [_button("Мои заявки", "plan:mine")]),
    )


@router.callback_query(F.data == "plan:mine")
async def plan_mine(callback: CallbackQuery) -> None:
    requests = await api.planning_requests(callback.from_user.id)
    if not requests:
        await edit_screen(callback, "На эту неделю заявок пока нет.", _kb([_button("Новая заявка", "plan:new")], [_button("Назад", "plan:start")]))
        await answer_callback(callback)
        return
    lines = ["Мои заявки на неделю:", ""]
    rows = []
    for item in requests:
        lines.append(f"#{item['id']} {item['band_name']} · {_duration_label(item['duration_minutes'])} · {STATUS_LABELS.get(item['status'], item['status'])}")
        lines.append(_windows_text(item["windows"]))
        if item["status"] == "pending":
            rows.append([_button(f"Удалить #{item['id']}", f"plan:del:{item['id']}")])
    rows.append([_button("Новая заявка", "plan:new")])
    rows.append([_button("Назад", "plan:start")])
    await edit_screen(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("plan:del:"))
async def plan_delete(callback: CallbackQuery) -> None:
    request_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    try:
        await api.delete_planning_request(callback.from_user.id, request_id)
    except RuntimeError as exc:
        await answer_callback(callback, str(exc), show_alert=True)
        return
    await answer_callback(callback, "Заявка удалена")
    await plan_mine(callback)


# ---------- admin side ----------


def _result_text(summary: dict, title: str) -> str:
    lines = [title, ""]
    if summary["assigned"]:
        lines.append("Назначено:")
        lines += [f"- {row['label']} · {row['band_name']} (приоритет {row['priority']})" for row in summary["assigned"]]
    if summary["unassigned"]:
        lines.append("")
        lines.append("Не нашлось места:")
        lines += [f"- {row['band_name']} ({_duration_label(row['duration_minutes'])})" for row in summary["unassigned"]]
    if not summary["assigned"] and not summary["unassigned"]:
        lines.append("Заявок нет.")
    return "\n".join(lines)


async def _require_admin(callback: CallbackQuery) -> bool:
    user = await api.get_user(callback.from_user.id)
    if user.get("role") != "admin":
        await answer_callback(callback, "Нет доступа", show_alert=True)
        return False
    return True


@router.callback_query(F.data == "plan:admin")
async def plan_admin(callback: CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    week = await api.planning_week()
    counts = week["requests"]
    status = "расписание опубликовано" if week["published"] else ("приём заявок открыт" if week["is_open"] else "приём закрыт, ждёт распределения")
    await edit_screen(
        callback,
        f"Планирование недели {_week_title(week)}\nСтатус: {status}\nАвто-распределение: {_deadline_text(week)}\n\n"
        f"Заявок: ждут {counts.get('pending', 0)}, назначено {counts.get('assigned', 0)}, без места {counts.get('unassigned', 0)}",
        _kb(
            [_button("Предпросмотр распределения", "plan:preview")],
            [_button("Запустить сейчас", "plan:run")],
            [_button("Назад", "admin:back")],
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data == "plan:preview")
async def plan_preview(callback: CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    try:
        summary = await api.planning_allocate(callback.from_user.id, dry_run=True)
    except RuntimeError as exc:
        await edit_screen(callback, f"Предпросмотр недоступен: {exc}", _kb([_button("Назад", "plan:admin")]))
        await answer_callback(callback)
        return
    await edit_screen(
        callback,
        _result_text(summary, "Предпросмотр (ничего не сохранено)"),
        _kb([_button("Запустить сейчас", "plan:run")], [_button("Назад", "plan:admin")]),
    )
    await answer_callback(callback)


@router.callback_query(F.data == "plan:run")
async def plan_run(callback: CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    await edit_screen(
        callback,
        "Запустить распределение сейчас? Расписание недели будет опубликовано, а ответственным уйдут уведомления. Повторно запустить нельзя.",
        _kb([_button("Да, запустить", "plan:run_confirm")], [_button("Отмена", "plan:admin")]),
    )
    await answer_callback(callback)


@router.callback_query(F.data == "plan:run_confirm")
async def plan_run_confirm(callback: CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    try:
        summary = await api.planning_allocate(callback.from_user.id, dry_run=False)
    except RuntimeError as exc:
        await edit_screen(callback, f"Не получилось: {exc}", _kb([_button("Назад", "plan:admin")]))
        await answer_callback(callback)
        return
    await edit_screen(callback, _result_text(summary, "Расписание опубликовано"), _kb([_button("Назад", "plan:admin")]))
    await answer_callback(callback, "Готово")
