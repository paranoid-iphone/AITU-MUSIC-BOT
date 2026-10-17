from urllib.parse import urlencode

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo

from app.core.config import settings


def language_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="ru"), KeyboardButton(text="kz"), KeyboardButton(text="en")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def group_keyboard(groups: list[dict]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=group["title"])] for group in groups],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🎵 Мои коллективы"), KeyboardButton(text="➕ Создать коллектив")],
        [KeyboardButton(text="📅 Забронировать")],
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


def mini_app_url(base_url: str, screen: str = "schedule", join_code: str | None = None, tab: str | None = None) -> str:
    """The Mini App entry. web_app buttons cannot carry a start_param, so the target screen goes in the query."""
    url = f"{base_url.rstrip('/')}/webapp/schedule"
    params = {}
    if screen != "schedule":
        params["screen"] = screen
    if join_code:
        params["join"] = join_code
    if tab:
        params["tab"] = tab
    return f"{url}?{urlencode(params)}" if params else url


def mini_app_button(
    text: str, base_url: str, screen: str = "schedule", join_code: str | None = None, tab: str | None = None
) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=mini_app_url(base_url, screen, join_code, tab)))


def mini_app_keyboard(text: str, base_url: str | None, screen: str = "schedule", join_code: str | None = None) -> InlineKeyboardMarkup:
    rows = [[mini_app_button(text, base_url, screen, join_code)]] if base_url else []
    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rehearsals_menu_keyboard(webapp_base_url: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    if webapp_base_url:
        rows.append([mini_app_button("📅 Забронировать репетицию", webapp_base_url, "schedule")])
        rows.append([mini_app_button("🎧 Мои репетиции", webapp_base_url, "mine")])
    if settings.weekly_planning_enabled:
        rows.append([InlineKeyboardButton(text="🗓 Заявка на неделю", callback_data="plan:start")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bands_menu_keyboard(webapp_base_url: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    if webapp_base_url:
        rows.append([mini_app_button("🎵 Мои коллективы", webapp_base_url, "bands")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def concerts_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎤 Открытые концерты", callback_data="menu:events")],
            [InlineKeyboardButton(text="📋 Мои заявки", callback_data="event_applications:0")],
            [InlineKeyboardButton(text="🕘 История мероприятий", callback_data="profile:event_history")],
            [InlineKeyboardButton(text="Назад", callback_data="menu:back")],
        ]
    )


def flow_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Назад в меню", callback_data="flow:cancel")],
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


def profile_event_history_keyboard(rows: list[dict] | None = None) -> InlineKeyboardMarkup:
    keyboard = [[InlineKeyboardButton(text="Добавить событие", callback_data="profile_event_history:add")]]
    personal_rows = [row for row in rows or [] if row.get("source") == "personal"]
    for item in personal_rows[:10]:
        text = f"{item['event_date']} | {item['event_title']}"
        keyboard.append([InlineKeyboardButton(text=text[:64], callback_data=f"profile_personal_event:{item['id']}")])
    keyboard.append([InlineKeyboardButton(text="Назад к профилю", callback_data="menu:profile")])
    keyboard.append([InlineKeyboardButton(text="В меню", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def profile_personal_event_detail_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Название", callback_data=f"profile_personal_event_edit:{item_id}:title"),
                InlineKeyboardButton(text="Дата", callback_data=f"profile_personal_event_edit:{item_id}:event_date"),
            ],
            [
                InlineKeyboardButton(text="Место", callback_data=f"profile_personal_event_edit:{item_id}:location"),
                InlineKeyboardButton(text="Роль", callback_data=f"profile_personal_event_edit:{item_id}:role_at_event"),
            ],
            [InlineKeyboardButton(text="Удалить", callback_data=f"profile_personal_event_delete:{item_id}")],
            [InlineKeyboardButton(text="Назад к истории", callback_data="profile:event_history")],
        ]
    )


def profile_personal_event_delete_confirm_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Удалить событие", callback_data=f"profile_personal_event_delete_confirm:{item_id}")],
            [InlineKeyboardButton(text="Назад", callback_data=f"profile_personal_event:{item_id}")],
        ]
    )


def admin_inline_keyboard(webapp_base_url: str | None = None) -> InlineKeyboardMarkup:
    """Main admin menu. Requests and admins live in the Mini App, so those buttons exist only with a public URL."""
    first_row = [InlineKeyboardButton(text="История", callback_data="admin_section:actions")]
    last_but_one = [InlineKeyboardButton(text="Группы", callback_data="groups:list")]
    if webapp_base_url:
        first_row.insert(0, mini_app_button("Заявки", webapp_base_url, "admin", tab="requests"))
        last_but_one.insert(0, mini_app_button("Админы", webapp_base_url, "admin", tab="roles"))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            first_row,
            [
                InlineKeyboardButton(text="Репетиции", callback_data="admin_section:schedule"),
                InlineKeyboardButton(text="Настройки", callback_data="admin_section:settings"),
            ],
            [
                InlineKeyboardButton(text="Коллективы", callback_data="admin_section:bands"),
                InlineKeyboardButton(text="Концерты", callback_data="admin_section:events"),
            ],
            last_but_one,
            [InlineKeyboardButton(text="Инструкция", callback_data="admin_section:guide")],
            [InlineKeyboardButton(text="Назад", callback_data="menu:back")],
        ]
    )


def staff_inline_keyboard(is_group_admin: bool, is_dsvr: bool, webapp_base_url: str | None = None) -> InlineKeyboardMarkup:
    """Admin menu for people who are not main admins: group admins and/or DSVR."""
    rows = []
    if is_group_admin:
        row = [InlineKeyboardButton(text="Коллективы", callback_data="admin_section:bands")]
        if webapp_base_url:
            row.insert(0, mini_app_button("Заявки", webapp_base_url, "admin", tab="requests"))
        rows.append(row)
    if is_dsvr:
        rows.append([InlineKeyboardButton(text="Занять время (приоритет ДСВР)", callback_data="dsvr:start")])
    rows.append([InlineKeyboardButton(text="История расписания", callback_data="sched_history:all")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def groups_keyboard(groups: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=group["title"], callback_data=f"groups:open:{group['id']}")] for group in groups]
    rows.append([InlineKeyboardButton(text="Назад", callback_data="admin:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_detail_keyboard(group_id: int, admins: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for admin in admins:
        name = f"{admin.get('last_name') or ''} {admin.get('first_name') or ''}".strip() or admin.get("telegram_username") or str(admin["telegram_id"])
        rows.append([InlineKeyboardButton(text=f"Снять: {name}"[:64], callback_data=f"groups:remove:{group_id}:{admin['telegram_id']}")])
    rows.append([InlineKeyboardButton(text="Добавить админа группы", callback_data=f"groups:add:{group_id}")])
    rows.append([InlineKeyboardButton(text="К группам", callback_data="groups:list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_bands_keyboard(bands: list[dict], page: int = 0, page_size: int = 6) -> InlineKeyboardMarkup:
    pages = max(1, (len(bands) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    rows = [
        [InlineKeyboardButton(text=band["name"][:64], callback_data=f"admin_band:{band['id']}:{page}")]
        for band in bands[start : start + page_size]
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="Назад", callback_data=f"admin_bands:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="Дальше", callback_data=f"admin_bands:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="К админке", callback_data="admin:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_band_detail_keyboard(band_id: int, page: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Лидер", callback_data=f"admin_band_members:{band_id}:owner:0"),
                InlineKeyboardButton(text="Участники", callback_data=f"admin_band_members:{band_id}:remove:0"),
            ],
            [InlineKeyboardButton(text="Ответственный", callback_data=f"admin_band_members:{band_id}:responsible:0")],
            [InlineKeyboardButton(text="История изменений", callback_data=f"band_history:{band_id}:a")],
            [InlineKeyboardButton(text="Назад к коллективам", callback_data=f"admin_bands:{page}")],
        ]
    )


def admin_band_members_keyboard(band: dict, action: str, page: int = 0, page_size: int = 6) -> InlineKeyboardMarkup:
    members = band.get("members", [])
    pages = max(1, (len(members) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    rows = []
    for member in members[start : start + page_size]:
        user = member["user"]
        name = f"{user.get('last_name') or ''} {user.get('first_name') or ''}".strip() or user.get("telegram_username") or str(user["telegram_id"])
        marker = "👑 " if member.get("is_owner") else ""
        rows.append([InlineKeyboardButton(text=f"{marker}{name} | {member['instrument_role']}"[:64], callback_data=f"admin_band_member:{band['id']}:{member['id']}:{action}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="Назад", callback_data=f"admin_band_members:{band['id']}:{action}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="Дальше", callback_data=f"admin_band_members:{band['id']}:{action}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="К коллективу", callback_data=f"admin_band:{band['id']}:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_member_remove_confirm_keyboard(band_id: int, member_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Удалить участника", callback_data=f"admin_band_member_remove_confirm:{band_id}:{member_id}")],
            [InlineKeyboardButton(text="Назад", callback_data=f"admin_band_members:{band_id}:remove:0")],
        ]
    )


def admin_actions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="История расписания", callback_data="sched_history:all")],
            [InlineKeyboardButton(text="Последние действия", callback_data="admin:action_history")],
            [InlineKeyboardButton(text="Назад", callback_data="admin:back")],
        ]
    )


def admin_schedule_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="Включить день", callback_data="admin:enable_day"),
            InlineKeyboardButton(text="Выключить день", callback_data="admin:disable_day"),
        ],
        [
            InlineKeyboardButton(text="Занять время", callback_data="admin:staff_booking"),
            InlineKeyboardButton(text="Отменить бронь", callback_data="admin_bookings_manage:0"),
        ],
        [InlineKeyboardButton(text="Занять с вытеснением (ДСВР)", callback_data="dsvr:start")],
    ]
    if settings.weekly_planning_enabled:
        rows.append([InlineKeyboardButton(text="Планирование недели", callback_data="plan:admin")])
    rows += [
        [InlineKeyboardButton(text="Часы кабинета", callback_data="admin:booking_window")],
        [InlineKeyboardButton(text="Назад", callback_data="admin:back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_booking_delete_list_keyboard(
    bookings: list[dict],
    page: int = 0,
    page_size: int = 6,
    action_prefix: str = "admin_booking_delete",
    page_prefix: str = "admin_bookings_delete",
) -> InlineKeyboardMarkup:
    pages = max(1, (len(bookings) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    rows = []
    for booking in bookings[start : start + page_size]:
        title = booking.get("song_title") or booking["band_name"]
        text = f"{booking['booking_date']} {booking['start_time'][:5]}-{booking['end_time'][:5]} | {title}"
        rows.append([InlineKeyboardButton(text=text[:64], callback_data=f"{action_prefix}:{booking['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="Назад", callback_data=f"{page_prefix}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="Дальше", callback_data=f"{page_prefix}:{page + 1}"))
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


def admin_booking_detail_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Удалить", callback_data=f"admin_booking_delete:{booking_id}")],
            [InlineKeyboardButton(text="Назад к репетициям", callback_data="admin_bookings_manage:0")],
        ]
    )


def admin_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Лимит в неделю", callback_data="admin:weekly_booking_limit"),
                InlineKeyboardButton(text="Лимит в день", callback_data="admin:daily_booking_limit"),
            ],
            [InlineKeyboardButton(text="Макс. броней на номер", callback_data="admin:max_future_bookings")],
            [InlineKeyboardButton(text="Личные репетиции вкл/выкл", callback_data="admin:personal_bookings_toggle")],
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


def event_applications_keyboard(applications: list[dict], page: int = 0, page_size: int = 6) -> InlineKeyboardMarkup:
    pages = max(1, (len(applications) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    rows = []
    for application in applications[start : start + page_size]:
        title = application.get("song_title") or "песня не указана"
        text = f"{application['event_date']} | {title} | {application['band_name']}"
        rows.append([InlineKeyboardButton(text=text[:64], callback_data=f"event_application:{application['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="Назад", callback_data=f"event_applications:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="Дальше", callback_data=f"event_applications:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="К концертам", callback_data="menu_section:concerts")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def event_application_detail_keyboard(application_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Песня", callback_data=f"event_application_edit:{application_id}:song"),
                InlineKeyboardButton(text="Участники", callback_data=f"event_application_edit:{application_id}:members"),
            ],
            [InlineKeyboardButton(text="Отозвать заявку", callback_data=f"event_application_delete:{application_id}")],
            [InlineKeyboardButton(text="Назад к заявкам", callback_data="event_applications:0")],
        ]
    )


def event_application_delete_confirm_keyboard(application_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отозвать заявку", callback_data=f"event_application_delete_confirm:{application_id}")],
            [InlineKeyboardButton(text="Назад", callback_data=f"event_application:{application_id}")],
        ]
    )


def event_application_members_keyboard(members: list[dict], selected_ids: list[int], application_id: int) -> InlineKeyboardMarkup:
    selected = set(selected_ids)
    rows = []
    for member in members:
        user = member["user"]
        name = f"{user.get('last_name') or ''} {user.get('first_name') or ''}".strip() or user.get("telegram_username") or str(user["telegram_id"])
        mark = "✓" if member["id"] in selected else "○"
        rows.append([InlineKeyboardButton(text=f"{mark} {name} | {member['instrument_role']}", callback_data=f"event_application_member:{member['id']}")])
    rows.append([InlineKeyboardButton(text="Сохранить участников", callback_data=f"event_application_members_save:{application_id}")])
    rows.append([InlineKeyboardButton(text="Назад к заявке", callback_data=f"event_application:{application_id}")])
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
