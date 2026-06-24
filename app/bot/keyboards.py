from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


def language_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="ru"), KeyboardButton(text="kz"), KeyboardButton(text="en")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🎵 Мои коллективы"), KeyboardButton(text="➕ Создать коллектив")],
        [KeyboardButton(text="📋 Расписание"), KeyboardButton(text="📅 Забронировать")],
        [KeyboardButton(text="🔑 Вступить по коду")],
        [KeyboardButton(text="🎤 Концерты"), KeyboardButton(text="👤 Личный кабинет")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="⚙️ Админ")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def main_menu_inline(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="📅 Репетиции", callback_data="menu_section:rehearsals"),
            InlineKeyboardButton(text="🎵 Коллективы", callback_data="menu_section:bands"),
        ],
        [
            InlineKeyboardButton(text="🎤 Концерты", callback_data="menu_section:concerts"),
            InlineKeyboardButton(text="👤 Профиль", callback_data="menu_section:profile"),
        ],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="⚙️ Админ", callback_data="menu:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rehearsals_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Забронировать репетицию", callback_data="menu:booking")],
            [InlineKeyboardButton(text="📋 Открыть расписание", callback_data="menu:schedule")],
            [InlineKeyboardButton(text="Назад", callback_data="menu:back")],
        ]
    )


def bands_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎵 Мои коллективы", callback_data="menu:bands")],
            [
                InlineKeyboardButton(text="➕ Создать коллектив", callback_data="menu:create_band"),
                InlineKeyboardButton(text="🔑 Вступить по коду", callback_data="menu:join_band"),
            ],
            [InlineKeyboardButton(text="Назад", callback_data="menu:back")],
        ]
    )


def concerts_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎤 Подать номер на концерт", callback_data="menu:events")],
            [InlineKeyboardButton(text="Назад", callback_data="menu:back")],
        ]
    )


def flow_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Назад в меню", callback_data="flow:cancel")],
        ]
    )


def booking_bands_keyboard(bands: list[dict], page: int = 0, page_size: int = 5) -> InlineKeyboardMarkup:
    pages = max(1, (len(bands) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    rows = [
        [InlineKeyboardButton(text=band["name"], callback_data=f"booking:band:{band['id']}")]
        for band in bands[start : start + page_size]
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="Назад", callback_data=f"booking:bands:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="Дальше", callback_data=f"booking:bands:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="Назад в меню", callback_data="flow:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def booking_days_keyboard(days: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=day["label"], callback_data=f"booking:date:{day['date']}")]
        for day in days
    ]
    rows.append(
        [
            InlineKeyboardButton(text="Назад", callback_data="booking:back:bands"),
            InlineKeyboardButton(text="В меню", callback_data="flow:cancel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def booking_times_keyboard(slots: list[dict], page: int = 0, page_size: int = 12) -> InlineKeyboardMarkup:
    pages = max(1, (len(slots) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    buttons = [
        InlineKeyboardButton(text=slot["start_time"][:5], callback_data=f"booking:slot:{start + idx}")
        for idx, slot in enumerate(slots[start : start + page_size])
    ]
    rows = [buttons[idx : idx + 3] for idx in range(0, len(buttons), 3)]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="Назад", callback_data=f"booking:times:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="Дальше", callback_data=f"booking:times:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(text="Назад", callback_data="booking:back:days"),
            InlineKeyboardButton(text="В меню", callback_data="flow:cancel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def booking_end_times_keyboard(options: list[dict], page: int = 0, page_size: int = 12) -> InlineKeyboardMarkup:
    pages = max(1, (len(options) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    buttons = [
        InlineKeyboardButton(text=option["label"], callback_data=f"booking:end:{start + idx}")
        for idx, option in enumerate(options[start : start + page_size])
    ]
    rows = [buttons[idx : idx + 3] for idx in range(0, len(buttons), 3)]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="Назад", callback_data=f"booking:ends:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="Дальше", callback_data=f"booking:ends:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(text="Назад", callback_data="booking:back:times"),
            InlineKeyboardButton(text="В меню", callback_data="flow:cancel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def booking_purpose_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="К концерту", callback_data="booking:purpose:concert"),
                InlineKeyboardButton(text="Для себя", callback_data="booking:purpose:self"),
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="booking:back:ends"),
                InlineKeyboardButton(text="В меню", callback_data="flow:cancel"),
            ],
        ]
    )


def booking_song_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Назад", callback_data="booking:back:purpose"),
                InlineKeyboardButton(text="В меню", callback_data="flow:cancel"),
            ],
        ]
    )


def profile_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Изменить имя"), KeyboardButton(text="Изменить фамилию")],
            [KeyboardButton(text="Изменить группу"), KeyboardButton(text="Изменить язык")],
            [KeyboardButton(text="Уведомления вкл/выкл")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
    )


def profile_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Изменить имя", callback_data="profile:first_name"),
                InlineKeyboardButton(text="Изменить фамилию", callback_data="profile:last_name"),
            ],
            [
                InlineKeyboardButton(text="Изменить группу", callback_data="profile:study_group"),
                InlineKeyboardButton(text="Изменить язык", callback_data="profile:language"),
            ],
            [InlineKeyboardButton(text="Уведомления вкл/выкл", callback_data="profile:notifications")],
            [InlineKeyboardButton(text="Назад", callback_data="menu:back")],
        ]
    )


def admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Заявки пользователей"), KeyboardButton(text="История действий")],
            [KeyboardButton(text="Одобрить пользователя"), KeyboardButton(text="Отклонить пользователя")],
            [KeyboardButton(text="Забанить пользователя"), KeyboardButton(text="Удалить пользователя")],
            [KeyboardButton(text="Добавить админа")],
            [KeyboardButton(text="Включить день"), KeyboardButton(text="Выключить день")],
            [KeyboardButton(text="Занять время"), KeyboardButton(text="Удалить бронь")],
            [KeyboardButton(text="Часы кабинета")],
            [KeyboardButton(text="Лимит в неделю"), KeyboardButton(text="Лимит в день")],
            [KeyboardButton(text="Создать концерт")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
    )


def admin_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Заявки", callback_data="admin_section:users"),
                InlineKeyboardButton(text="История", callback_data="admin_section:actions"),
            ],
            [
                InlineKeyboardButton(text="Расписание", callback_data="admin_section:schedule"),
                InlineKeyboardButton(text="Настройки", callback_data="admin_section:settings"),
            ],
            [
                InlineKeyboardButton(text="Концерты", callback_data="admin_section:events"),
            ],
            [InlineKeyboardButton(text="Назад", callback_data="menu:back")],
        ]
    )


def admin_users_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Заявки", callback_data="admin_users:pending:0"),
                InlineKeyboardButton(text="Одобренные", callback_data="admin_users:approved:0"),
            ],
            [
                InlineKeyboardButton(text="Отклоненные", callback_data="admin_users:rejected:0"),
                InlineKeyboardButton(text="Забаненные", callback_data="admin_users:banned:0"),
            ],
            [InlineKeyboardButton(text="Ручное действие", callback_data="admin_users:manual")],
            [InlineKeyboardButton(text="Назад", callback_data="admin:back")],
        ]
    )


def admin_users_manual_keyboard(can_add_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="Одобрить", callback_data="admin:approve_user"),
            InlineKeyboardButton(text="Отклонить", callback_data="admin:reject_user"),
        ],
        [
            InlineKeyboardButton(text="Забанить", callback_data="admin:ban_user"),
            InlineKeyboardButton(text="Удалить", callback_data="admin:delete_user"),
        ],
    ]
    if can_add_admin:
        rows.append([InlineKeyboardButton(text="Добавить админа", callback_data="admin:add_admin")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="admin_section:users")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_user_list_keyboard(users: list[dict], status: str, page: int = 0, page_size: int = 6) -> InlineKeyboardMarkup:
    pages = max(1, (len(users) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    rows = []
    for user in users[start : start + page_size]:
        name = f"{user.get('last_name') or ''} {user.get('first_name') or ''}".strip() or user.get("telegram_full_name") or "Без имени"
        username = f"@{user['telegram_username']}" if user.get("telegram_username") else str(user["telegram_id"])
        rows.append([InlineKeyboardButton(text=f"{name} | {username}", callback_data=f"admin_user:{user['telegram_id']}:{status}:{page}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="Назад", callback_data=f"admin_users:{status}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="Дальше", callback_data=f"admin_users:{status}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="К пользователям", callback_data="admin_section:users")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_user_detail_keyboard(user: dict, origin_status: str = "pending", page: int = 0) -> InlineKeyboardMarkup:
    telegram_id = user["telegram_id"]
    rows = []
    if user["status"] in {"pending", "rejected", "banned"}:
        rows.append([InlineKeyboardButton(text="Одобрить", callback_data=f"admin_user_action:approve:{telegram_id}")])
    if user["status"] in {"pending", "approved"}:
        rows.append([InlineKeyboardButton(text="Отклонить", callback_data=f"admin_user_action:reject:{telegram_id}")])
    if user["status"] != "banned":
        rows.append([InlineKeyboardButton(text="Забанить", callback_data=f"admin_user_action:ban:{telegram_id}")])
    rows.append([InlineKeyboardButton(text="Удалить", callback_data=f"admin_user_action:delete:{telegram_id}")])
    rows.append([InlineKeyboardButton(text="Назад к списку", callback_data=f"admin_users:{origin_status}:{page}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_actions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Последние действия", callback_data="admin:action_history")],
            [InlineKeyboardButton(text="Назад", callback_data="admin:back")],
        ]
    )


def admin_schedule_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Включить день", callback_data="admin:enable_day"),
                InlineKeyboardButton(text="Выключить день", callback_data="admin:disable_day"),
            ],
            [
                InlineKeyboardButton(text="Занять время", callback_data="admin:staff_booking"),
                InlineKeyboardButton(text="Удалить бронь", callback_data="admin_bookings_delete:0"),
            ],
            [InlineKeyboardButton(text="Часы кабинета", callback_data="admin:booking_window")],
            [InlineKeyboardButton(text="Назад", callback_data="admin:back")],
        ]
    )


def admin_booking_delete_list_keyboard(bookings: list[dict], page: int = 0, page_size: int = 6) -> InlineKeyboardMarkup:
    pages = max(1, (len(bookings) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    rows = []
    for booking in bookings[start : start + page_size]:
        title = booking.get("song_title") or booking["band_name"]
        text = f"{booking['booking_date']} {booking['start_time'][:5]}-{booking['end_time'][:5]} | {title}"
        rows.append([InlineKeyboardButton(text=text[:64], callback_data=f"admin_booking_delete:{booking['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="Назад", callback_data=f"admin_bookings_delete:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="Дальше", callback_data=f"admin_bookings_delete:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="К расписанию", callback_data="admin_section:schedule")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_booking_delete_confirm_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Удалить", callback_data=f"admin_booking_delete_confirm:{booking_id}"),
                InlineKeyboardButton(text="Назад", callback_data="admin_bookings_delete:0"),
            ],
        ]
    )


def admin_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Лимит в неделю", callback_data="admin:weekly_booking_limit"),
                InlineKeyboardButton(text="Лимит в день", callback_data="admin:daily_booking_limit"),
            ],
            [InlineKeyboardButton(text="Назад", callback_data="admin:back")],
        ]
    )


def admin_events_keyboard(events: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Создать концерт", callback_data="admin:create_event")]]
    for event in events[:10]:
        rows.append([InlineKeyboardButton(text=f"{event['event_date']} | {event['title']}", callback_data=f"admin_event:{event['id']}")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="admin:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_event_detail_keyboard(event_id: int, status: str) -> InlineKeyboardMarkup:
    status_text = "Закрыть" if status == "open" else "Открыть"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Название", callback_data=f"admin_event_edit:{event_id}:title"),
                InlineKeyboardButton(text="Дата", callback_data=f"admin_event_edit:{event_id}:event_date"),
            ],
            [
                InlineKeyboardButton(text="Место", callback_data=f"admin_event_edit:{event_id}:location"),
                InlineKeyboardButton(text="Дедлайн", callback_data=f"admin_event_edit:{event_id}:submission_deadline"),
            ],
            [InlineKeyboardButton(text=status_text, callback_data=f"admin_event_toggle:{event_id}")],
            [InlineKeyboardButton(text="Заявки номеров", callback_data=f"admin_event_applications:{event_id}")],
            [InlineKeyboardButton(text="Назад к концертам", callback_data="admin_section:events")],
        ]
    )


def user_events_keyboard(events: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for event in events[:10]:
        rows.append([InlineKeyboardButton(text=f"{event['event_date']} | {event['title']}", callback_data=f"event_apply:{event['id']}")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def event_bands_keyboard(bands: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=band["name"], callback_data=f"event_band:{band['id']}")] for band in bands]
    rows.append([InlineKeyboardButton(text="Назад к концертам", callback_data="menu:events")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def event_members_keyboard(members: list[dict], selected_ids: list[int]) -> InlineKeyboardMarkup:
    selected = set(selected_ids)
    rows = []
    for member in members:
        user = member["user"]
        name = f"{user.get('last_name') or ''} {user.get('first_name') or ''}".strip() or user.get("telegram_username") or str(user["telegram_id"])
        mark = "✓" if member["id"] in selected else "○"
        rows.append([InlineKeyboardButton(text=f"{mark} {name} | {member['instrument_role']}", callback_data=f"event_member:{member['id']}")])
    rows.append([InlineKeyboardButton(text="Отправить заявку", callback_data="event_submit")])
    rows.append([InlineKeyboardButton(text="Назад к бэндам", callback_data="event_back:bands")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_event_applications_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Назад к концерту", callback_data=f"admin_event:{event_id}")],
        ]
    )


def registration_review_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Одобрить", callback_data=f"registration:approve:{telegram_id}"),
                InlineKeyboardButton(text="Отклонить", callback_data=f"registration:reject:{telegram_id}"),
            ]
        ]
    )
