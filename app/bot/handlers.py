import logging
import hashlib
import json
import random
from datetime import date, datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InputMediaPhoto, Message, ReplyKeyboardRemove
from redis.asyncio import Redis

from app.bot.api_client import BackendClient
from app.bot.i18n import t
from app.bot.keyboards import (
    admin_actions_keyboard,
    admin_admins_keyboard,
    admin_band_detail_keyboard,
    admin_band_members_keyboard,
    admin_bands_keyboard,
    admin_booking_delete_confirm_keyboard,
    admin_booking_delete_list_keyboard,
    admin_booking_detail_keyboard,
    admin_booking_purpose_keyboard,
    admin_event_detail_keyboard,
    admin_events_keyboard,
    admin_event_applications_keyboard,
    admin_inline_keyboard,
    admin_member_remove_confirm_keyboard,
    admin_user_detail_keyboard,
    admin_user_list_keyboard,
    admin_keyboard,
    admin_schedule_keyboard,
    admin_settings_keyboard,
    admin_users_manual_keyboard,
    admin_users_keyboard,
    bands_menu_keyboard,
    booking_bands_keyboard,
    booking_days_keyboard,
    booking_end_times_keyboard,
    booking_purpose_keyboard,
    booking_song_keyboard,
    booking_times_keyboard,
    concerts_menu_keyboard,
    event_bands_keyboard,
    event_application_delete_confirm_keyboard,
    event_application_detail_keyboard,
    event_application_members_keyboard,
    event_applications_keyboard,
    event_members_keyboard,
    flow_cancel_keyboard,
    language_keyboard,
    main_menu,
    main_menu_inline,
    profile_inline_keyboard,
    profile_event_history_keyboard,
    profile_personal_event_delete_confirm_keyboard,
    profile_personal_event_detail_keyboard,
    profile_keyboard,
    rehearsals_menu_keyboard,
    registration_review_keyboard,
    user_events_keyboard,
    user_band_detail_keyboard,
    user_band_members_keyboard,
    user_bands_list_keyboard,
    user_booking_delete_confirm_keyboard,
    user_booking_detail_keyboard,
    user_bookings_list_keyboard,
    user_member_remove_confirm_keyboard,
)
from app.bot.schedule_image import render_schedule_image
from app.bot.states import AdminFlow, BandFlow, BookingFlow, EventFlow, Onboarding, ProfileFlow
from app.bot.webapp_url import get_webapp_base_url
from app.core.config import settings

router = Router()
api = BackendClient()
redis = Redis.from_url(settings.redis_url, decode_responses=True)
logger = logging.getLogger(__name__)
registration_notice_sent: set[int] = set()
SCHEDULE_MENU_MESSAGES_KEY = "bot:schedule_menu_messages"
SCHEDULE_IMAGE_FILE_ID_PREFIX = "bot:schedule_image_file_id:"
bot_username_cache: str | None = None

LEHA_PRAISES = [
    (
        "Алексей Захаров - великий создатель этого бота, человек, который посмотрел на хаос расписаний "
        "и сказал: теперь тут будет порядок, стиль и кнопочки."
    ),
    (
        "Легенды AITU Music Club гласят: когда Алексей Захаров пишет код, репетиции сами становятся в расписание, "
        "а админка начинает вести себя прилично."
    ),
    (
        "Сегодняшняя минутка уважения посвящается Алексею Захарову: архитектору порядка, покровителю бэндов "
        "и человеку, без которого этот бот был бы просто грустной идеей в заметках."
    ),
    (
        "Алексей Захаров - тот самый великий создатель, который дал этому боту смысл, кнопки и характер. "
        "Просим любить, ценить и не забывать вовремя бронировать кабинет."
    ),
]


async def show_menu(message: Message, user: dict) -> None:
    language = user.get("language", "ru")
    await send_schedule_menu(message, user, t(language, "main_menu"))


def extract_join_code_from_start(message: Message) -> str | None:
    parts = (message.text or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        return None
    payload = parts[1].strip()
    if payload.startswith("join_"):
        code = payload.removeprefix("join_").strip()
        return code or None
    return None


async def get_bot_username(bot) -> str:
    global bot_username_cache
    if bot_username_cache is None:
        me = await bot.get_me()
        bot_username_cache = me.username
    return bot_username_cache


async def build_band_invite_link(bot, invite_code: str) -> str:
    username = await get_bot_username(bot)
    return f"https://t.me/{username}?start=join_{invite_code}"


async def format_bands_list(bot, bands: list[dict]) -> str:
    lines = []
    for band in bands:
        invite_link = await build_band_invite_link(bot, band["invite_code"])
        lines.append(f"{band['id']}. {band['name']}")
        lines.append(f"Код: {band['invite_code']}")
        lines.append(f"Ссылка: {invite_link}")
        for member in band["members"]:
            u = member["user"]
            lines.append(f"  - {u['last_name'] or ''} {u['first_name'] or ''} | {u['study_group'] or '-'} | {member['instrument_role']}")
        lines.append("")
    return "\n".join(lines).strip()


async def start_join_by_code(message: Message, state: FSMContext, invite_code: str) -> None:
    await state.update_data(invite_code=invite_code)
    await state.set_state(BandFlow.join_role)
    await message.answer(
        "Ссылка на вступление в коллектив открыта.\n"
        "Напишите вашу роль в коллективе: гитарист, вокалист, барабанщик или другое.",
        reply_markup=flow_cancel_keyboard(),
    )


async def edit_screen(callback: CallbackQuery, text: str, reply_markup=None) -> None:
    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=text, reply_markup=reply_markup)
        else:
            await callback.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await callback.message.answer(text, reply_markup=reply_markup)


async def answer_callback(callback: CallbackQuery, text: str | None = None, show_alert: bool = False) -> None:
    try:
        await callback.answer(text, show_alert=show_alert)
    except TelegramBadRequest as exc:
        if "query is too old" not in str(exc) and "query ID is invalid" not in str(exc):
            raise


def format_purpose(value: str) -> str:
    return {"concert": "к концерту", "self": "для себя", "staff": "ивент / стафф", "personal": "личная репетиция"}.get(value, value)


def format_hours(minutes: int) -> str:
    hours = minutes / 60
    return f"{hours:g} ч"


def format_limit_status(status: dict) -> str:
    text = (
        f"Лимит: {format_hours(status['used_minutes'])} использовано из "
        f"{format_hours(status['weekly_limit_minutes'])}; осталось {format_hours(status['remaining_minutes'])}."
    )
    if status.get("daily_limit_minutes", 0) > 0:
        text += (
            f"\nНа этот день: {format_hours(status['daily_used_minutes'])} использовано из "
            f"{format_hours(status['daily_limit_minutes'])}; осталось {format_hours(status['daily_remaining_minutes'])}."
        )
    return text


def format_booking_date(value: str) -> str:
    weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    parsed = date.fromisoformat(value)
    return f"{parsed.isoformat()} ({weekdays[parsed.weekday()]})"


def format_booking_day_button(value: date, slots_count: int) -> str:
    weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    return f"{weekdays[value.weekday()]} {value.strftime('%d.%m')} | свободно: {slots_count}"


def format_event_card(event: dict) -> str:
    status = "открыт" if event["status"] == "open" else "закрыт"
    return (
        f"Концерт #{event['id']}\n"
        f"Название: {event['title']}\n"
        f"Дата: {event['event_date']}\n"
        f"Место: {event['location'] or '-'}\n"
        f"Дедлайн списков: {event['submission_deadline'] or '-'}\n"
        f"Статус: {status}"
    )


def format_application_card(application: dict) -> str:
    lines = [
        f"Заявка #{application['id']}",
        f"Концерт: {application['event_title']} | {application['event_date']}",
        f"Место: {application.get('event_location') or '-'}",
        f"Коллектив: {application['band_name']}",
        f"Песня/номер: {application.get('song_title') or 'песня не указана'}",
        "",
        "Участники:",
    ]
    for member in application.get("members", []):
        user = member["user"]
        name = f"{user.get('last_name') or ''} {user.get('first_name') or ''}".strip() or f"@{user.get('telegram_username') or '-'}"
        lines.append(f"- {name} | {user.get('study_group') or '-'} | {member['instrument_role']}")
    lines.append("")
    lines.append("Выберите действие:")
    return "\n".join(lines)


def format_user_card(user: dict) -> str:
    name = f"{user.get('last_name') or '-'} {user.get('first_name') or '-'}".strip()
    return (
        f"Пользователь\n"
        f"Имя: {name}\n"
        f"Группа: {user.get('study_group') or '-'}\n"
        f"Telegram: @{user.get('telegram_username') or '-'}\n"
        f"Telegram ID: {user['telegram_id']}\n"
        f"Роль: {user['role']}\n"
        f"Статус: {user['status']}\n"
        f"Причина: {user.get('moderation_reason') or '-'}\n"
        f"Повторная подача: {user.get('registration_retry_after') or '-'}"
    )


def format_admins_list(users: list[dict]) -> str:
    admins = [user for user in users if user.get("role") == "admin" and user.get("status") == "approved"]
    if not admins:
        return "Управление админами\n\nАдминов пока не найдено."
    lines = ["Управление админами", "", "Текущие админы:"]
    for index, user in enumerate(admins, start=1):
        name = f"{user.get('last_name') or ''} {user.get('first_name') or ''}".strip()
        username = f"@{user['telegram_username']}" if user.get("telegram_username") else str(user["telegram_id"])
        lines.append(f"{index}. {name or username} | {username}")
    return "\n".join(lines)


def format_band_card(band: dict) -> str:
    lines = [
        f"Коллектив #{band['id']}",
        f"Название: {band['name']}",
        f"Код: {band['invite_code']}",
        "",
        "Участники:",
    ]
    for member in band.get("members", []):
        user = member["user"]
        name = f"{user.get('last_name') or ''} {user.get('first_name') or ''}".strip() or user.get("telegram_username") or str(user["telegram_id"])
        owner = " | лидер" if member.get("is_owner") else ""
        lines.append(f"- #{member['id']} {name} | {user.get('study_group') or '-'} | {member['instrument_role']}{owner}")
    return "\n".join(lines)


def user_is_band_owner(band: dict, telegram_id: int) -> bool:
    return any(
        member.get("is_owner") and member.get("user", {}).get("telegram_id") == telegram_id
        for member in band.get("members", [])
    )


def admin_guide_text() -> str:
    return (
        "Инструкция админа\n\n"
        "Заявки: проверяй имя, фамилию и группу. Отклонение не банит пользователя, он сможет подать заявку позже.\n\n"
        "Репетиции: можно занять кабинет под стафф/ивент, изменить дату, время, песню или цель любой брони, а также удалить бронь.\n\n"
        "Личные репетиции: в настройках можно включить или выключить бронь без коллектива.\n\n"
        "Коллективы: можно переименовать коллектив, поменять лидера, отредактировать роли участников или удалить участника из коллектива.\n\n"
        "Концерты: создай концерт, открой прием номеров, затем смотри заявки номеров со списками участников.\n\n"
        "Админы: добавляй админа по @username или Telegram ID. Initial admins защищены от снятия роли.\n\n"
        "История: раздел показывает последние действия: кто кого одобрил, удалил, изменил бронь, коллектив или концерт."
    )


def user_help_text(is_approved: bool = True) -> str:
    if not is_approved:
        return (
            "AITU Music Club Bot\n\n"
            "Пока ваша регистрация ожидает одобрения админа, доступен просмотр расписания.\n"
            "После одобрения откроются коллективы, бронирование, концерты и личная история мероприятий.\n\n"
            "Если данные профиля введены неправильно, после одобрения их можно будет поменять в профиле."
        )
    return (
        "AITU Music Club Bot: инструкция\n\n"
        "Главное меню всегда показывает актуальную картинку расписания на ближайшие 7 дней.\n\n"
        "Репетиции: можно посмотреть свои брони, забронировать время для коллектива или личную репетицию, "
        "а также изменить или удалить свою бронь.\n\n"
        "Коллективы: можно создать коллектив, вступить по коду или ссылке, посмотреть участников. "
        "Лидер коллектива может менять название, роли, состав и передавать лидерство.\n\n"
        "Концерты: открытые концерты показывают, куда можно подать номер. Лидер выбирает коллектив, песню "
        "и участников. В Моих заявках можно изменить песню/состав или отозвать заявку.\n\n"
        "Профиль: можно изменить имя, фамилию, учебную группу, язык и уведомления.\n\n"
        "История мероприятий: здесь видны события для social GPA. Клубные события добавляются из заявок на концерты, "
        "а личные события можно добавить вручную и потом редактировать: название, дату, место и роль."
    )


def format_event_history(rows: list[dict]) -> str:
    if not rows:
        return (
            "История мероприятий\n\n"
            "Пока нет мероприятий. Можно добавить личное событие вручную."
        )
    lines = ["История мероприятий"]
    for index, item in enumerate(rows[:30], start=1):
        source = item.get("source_label") or ("личный" if item.get("source") == "personal" else "клубный")
        location = item.get("event_location") or "-"
        song = item.get("song_title") or "песня не указана"
        band = item.get("band_name") or "личное событие"
        lines.append(
            f"\n{index}. {item['event_date']} | {item['event_title']}\n"
            f"Источник: {source}\n"
            f"Место: {location}\n"
            f"Песня: {song}\n"
            f"Коллектив: {band}\n"
            f"Роль: {item['instrument_role']}"
        )
    if len(rows) > 30:
        lines.append(f"\nПоказаны последние 30 из {len(rows)} мероприятий.")
    return "\n".join(lines)


def format_personal_event_card(item: dict) -> str:
    title = item.get("event_title") or item.get("title") or "-"
    event_date_value = item.get("event_date") or "-"
    location = item.get("event_location") if "event_location" in item else item.get("location")
    role = item.get("instrument_role") or item.get("role_at_event") or "-"
    return (
        f"Личное событие #{item['id']}\n"
        f"Название: {title}\n"
        f"Дата: {event_date_value}\n"
        f"Место: {location or '-'}\n"
        f"Роль: {role}\n\n"
        "Выберите действие:"
    )


def format_booking_card(booking: dict) -> str:
    title = booking.get("song_title") or "-"
    owner = booking.get("band_name") or ("Личная репетиция" if booking.get("purpose") == "personal" else "-")
    return (
        f"Бронь #{booking['id']}\n"
        f"Дата: {booking['booking_date']}\n"
        f"Время: {booking['start_time'][:5]}-{booking['end_time'][:5]}\n"
        f"Кто занял: {owner}\n"
        f"Создал: {booking.get('created_by') or '-'}\n"
        f"Цель: {format_purpose(booking['purpose'])}\n"
        f"Песня/название: {title}\n\n"
        "Выберите действие:"
    )


def parse_booking_window(value: str) -> tuple[datetime, datetime] | None:
    normalized = value.strip().replace(" ", "")
    if "-" not in normalized:
        return None
    start_raw, end_raw = normalized.split("-", maxsplit=1)
    parsed = []
    for raw in (start_raw, end_raw):
        value_datetime = None
        for fmt in ("%H:%M", "%H"):
            try:
                value_datetime = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if value_datetime is None:
            return None
        parsed.append(value_datetime)
    return parsed[0], parsed[1]


async def get_end_time_options(target_date: str, start_time: str, band_id: int | None) -> list[dict]:
    options = []
    parsed_date = date.fromisoformat(target_date)
    for duration in range(30, 24 * 60 + 1, 30):
        slots = await api.available_slots(parsed_date, duration, band_id)
        slot = next((item for item in slots if item["start_time"][:5] == start_time[:5]), None)
        if slot is None:
            break
        label = f"{slot['end_time'][:5]} ({format_hours(duration)})"
        options.append({"date": target_date, "start_time": slot["start_time"], "end_time": slot["end_time"], "label": label})
    return options


def build_end_time_text(target_date: str, start_time: str, limit_status: dict | None, end_options: list[dict]) -> str:
    text = (
        f"{format_booking_date(target_date)}\n"
        f"Начало: {start_time[:5]}\n"
        "Выберите время окончания:"
    )
    if limit_status is not None:
        text = (
            f"{format_booking_date(target_date)}\n"
            f"Начало: {start_time[:5]}\n"
            f"{format_limit_status(limit_status)}\n"
            "Выберите время окончания:"
        )
    if end_options:
        max_duration = int(
            (
                datetime.strptime(end_options[-1]["end_time"], "%H:%M:%S")
                - datetime.strptime(end_options[-1]["start_time"], "%H:%M:%S")
            ).total_seconds()
            // 60
        )
        if limit_status is not None and max_duration < limit_status["remaining_minutes"]:
            text += "\nДальше уже занято или выходит за часы кабинета."
    return text


async def get_schedule_text() -> str:
    rows = await api.schedule()
    if not rows:
        return "Расписание на ближайшие 7 дней свободно."
    lines = ["Расписание на ближайшие 7 дней:"]
    for item in rows:
        song = item["song_title"] or "песня не указана"
        lines.append(
            f"{item['booking_date']} {item['start_time'][:5]}-{item['end_time'][:5]} | "
            f"{item['band_name']} | {song} | {format_purpose(item['purpose'])}"
        )
    return "\n".join(lines)


def schedule_fingerprint(rows: list[dict]) -> str:
    payload = [
        {
            "id": item.get("id"),
            "band_name": item.get("band_name"),
            "booking_date": item.get("booking_date"),
            "start_time": item.get("start_time"),
            "end_time": item.get("end_time"),
            "purpose": item.get("purpose"),
            "song_title": item.get("song_title"),
            "status": item.get("status"),
        }
        for item in rows
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def get_schedule_image_media() -> tuple[str | BufferedInputFile, str, bool]:
    rows = await api.schedule()
    fingerprint = schedule_fingerprint(rows)
    cached_file_id = await redis.get(f"{SCHEDULE_IMAGE_FILE_ID_PREFIX}{fingerprint}")
    if cached_file_id:
        return cached_file_id, fingerprint, True
    image = render_schedule_image(rows)
    return BufferedInputFile(image, filename="schedule.jpg"), fingerprint, False


async def remember_schedule_file_id(fingerprint: str, message: Message | None) -> None:
    if message is None or not message.photo:
        return
    await redis.set(f"{SCHEDULE_IMAGE_FILE_ID_PREFIX}{fingerprint}", message.photo[-1].file_id, ex=60 * 60 * 24 * 14)


async def remember_schedule_menu(chat_id: int, message_id: int, is_admin: bool) -> None:
    await redis.hset(SCHEDULE_MENU_MESSAGES_KEY, str(chat_id), f"{message_id}:{int(is_admin)}")


async def forget_schedule_menu(chat_id: int) -> None:
    await redis.hdel(SCHEDULE_MENU_MESSAGES_KEY, str(chat_id))


async def refresh_all_schedule_menus(bot, caption: str = "Главное меню") -> None:
    stored_messages = await redis.hgetall(SCHEDULE_MENU_MESSAGES_KEY)
    if not stored_messages:
        return
    image_media, fingerprint, is_cached = await get_schedule_image_media()
    for chat_id_raw, payload in stored_messages.items():
        try:
            message_id_raw, is_admin_raw = payload.split(":", maxsplit=1)
            chat_id = int(chat_id_raw)
            message_id = int(message_id_raw)
            is_admin = is_admin_raw == "1"
        except (ValueError, AttributeError):
            await redis.hdel(SCHEDULE_MENU_MESSAGES_KEY, chat_id_raw)
            continue
        media = InputMediaPhoto(media=image_media, caption=caption)
        try:
            updated = await bot.edit_message_media(
                chat_id=chat_id,
                message_id=message_id,
                media=media,
                reply_markup=main_menu_inline(is_admin),
            )
            if not is_cached and isinstance(updated, Message):
                await remember_schedule_file_id(fingerprint, updated)
                image_media = updated.photo[-1].file_id
                is_cached = True
        except Exception as exc:
            text = str(exc).lower()
            if "message is not modified" not in text:
                logger.info("Could not refresh schedule menu for chat %s: %s", chat_id, exc)
            if "message to edit not found" in text or "bot was blocked" in text or "chat not found" in text:
                await forget_schedule_menu(chat_id)


async def send_schedule(message: Message) -> None:
    image_media, fingerprint, is_cached = await get_schedule_image_media()
    sent = await message.answer_photo(image_media, caption="Расписание на ближайшие 7 дней")
    if not is_cached:
        await remember_schedule_file_id(fingerprint, sent)


async def send_schedule_menu(message: Message, user: dict, caption: str | None = None) -> None:
    image_media, fingerprint, is_cached = await get_schedule_image_media()
    sent = await message.answer_photo(
        image_media,
        caption=caption or "Главное меню",
        reply_markup=main_menu_inline(user.get("role") == "admin"),
    )
    if not is_cached:
        await remember_schedule_file_id(fingerprint, sent)
    await remember_schedule_menu(sent.chat.id, sent.message_id, user.get("role") == "admin")


async def edit_schedule_menu(callback: CallbackQuery, user: dict, caption: str | None = None) -> None:
    image_media, fingerprint, is_cached = await get_schedule_image_media()
    media = InputMediaPhoto(media=image_media, caption=caption or "Главное меню")
    try:
        updated = await callback.message.edit_media(media=media, reply_markup=main_menu_inline(user.get("role") == "admin"))
        if not is_cached and isinstance(updated, Message):
            await remember_schedule_file_id(fingerprint, updated)
        await remember_schedule_menu(callback.message.chat.id, callback.message.message_id, user.get("role") == "admin")
    except Exception:
        try:
            await callback.message.edit_caption(caption=caption or "Главное меню", reply_markup=main_menu_inline(user.get("role") == "admin"))
            await remember_schedule_menu(callback.message.chat.id, callback.message.message_id, user.get("role") == "admin")
        except Exception:
            await send_schedule_menu(callback.message, user, caption)


async def start_booking_flow(message: Message, state: FSMContext, telegram_id: int) -> None:
    bands = await api.list_bands(telegram_id)
    personal_settings = await api.personal_booking_settings()
    allow_personal = bool(personal_settings.get("personal_bookings_enabled"))
    if not bands and not allow_personal:
        await message.answer("Сначала создайте коллектив или вступите в него.")
        return
    bands = sorted(bands, key=lambda item: item.get("created_at", ""), reverse=True)
    await state.update_data(booking_bands=bands, allow_personal_booking=allow_personal)
    await state.set_state(BookingFlow.band_id)
    await message.answer("Выберите коллектив или личную репетицию:", reply_markup=booking_bands_keyboard(bands, allow_personal=allow_personal))


async def send_pending_notice(message: Message) -> None:
    await send_schedule(message)
    await message.answer(
        "Ваша регистрация отправлена админам. Пока одобрения нет, доступен только просмотр расписания.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def get_registration_admin_ids() -> set[int]:
    admin_ids = set(settings.initial_admin_ids)
    for seed_admin_id in settings.initial_admin_ids:
        try:
            users = await api.list_users(seed_admin_id, "approved")
        except Exception as exc:
            logger.warning("Could not load admins for registration notification: %s", exc)
            continue
        for item in users:
            if item.get("role") == "admin" and item.get("status") == "approved":
                admin_ids.add(item["telegram_id"])
    return admin_ids


async def notify_admins_about_registration(message: Message, user: dict, force: bool = False) -> bool:
    telegram_id = user["telegram_id"]
    if not force and telegram_id in registration_notice_sent:
        return False
    sent = 0
    admin_ids = await get_registration_admin_ids()
    for admin_id in admin_ids:
        try:
            await message.bot.send_message(
                admin_id,
                "Новая заявка на регистрацию:\n"
                f"{user['last_name']} {user['first_name']} | {user['study_group']}\n"
                f"Telegram: @{user['telegram_username'] or '-'}\n"
                f"Telegram ID: {user['telegram_id']}\n"
                "Одобрить регистрацию?",
                reply_markup=registration_review_keyboard(user["telegram_id"]),
            )
            sent += 1
        except Exception as exc:
            logger.warning("Could not send registration notification to admin %s: %s", admin_id, exc)
    if sent:
        registration_notice_sent.add(telegram_id)
    else:
        logger.warning("Registration notification for user %s was not delivered to any admin", telegram_id)
    return sent > 0


async def prompt_next_profile_step(message: Message, state: FSMContext, user: dict, show_welcome: bool = False) -> None:
    language = user.get("language", "ru")
    if show_welcome:
        await message.answer(t(language, "welcome"))
    if not user.get("first_name"):
        await state.set_state(Onboarding.first_name)
        await message.answer(t(language, "ask_first_name"), reply_markup=ReplyKeyboardRemove())
        return
    if not user.get("last_name"):
        await state.set_state(Onboarding.last_name)
        await message.answer("Введите фамилию:", reply_markup=ReplyKeyboardRemove())
        return
    if not user.get("study_group"):
        await state.set_state(Onboarding.study_group)
        await message.answer("Введите учебную группу:", reply_markup=ReplyKeyboardRemove())
        return
    await state.set_state(Onboarding.language)
    await message.answer("Выберите язык:", reply_markup=language_keyboard())


async def has_full_access(message: Message) -> bool:
    user = await api.get_user(message.from_user.id)
    if user["role"] == "admin" or user["status"] == "approved":
        return True
    await send_pending_notice(message)
    return False


async def has_full_access_for_callback(callback: CallbackQuery) -> bool:
    user = await api.get_user(callback.from_user.id)
    if user["role"] == "admin" or user["status"] == "approved":
        return True
    await send_pending_notice(callback.message)
    await answer_callback(callback)
    return False


@router.callback_query(F.data == "menu:back")
async def inline_back(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user = await api.get_user(callback.from_user.id)
    await edit_schedule_menu(callback, user, t(user.get("language", "ru"), "main_menu"))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("menu_section:"))
async def inline_menu_section(callback: CallbackQuery) -> None:
    if not await has_full_access_for_callback(callback):
        return
    section = callback.data.split(":", maxsplit=1)[1]
    if section == "rehearsals":
        rows = await api.my_bookings(callback.from_user.id)
        preview = "\n".join(
            f"- {item['booking_date']} {item['start_time'][:5]}-{item['end_time'][:5]} | {item.get('song_title') or item['band_name']}"
            for item in rows[:3]
        )
        text = "Репетиции"
        if preview:
            text += "\n\nБлижайшие мои:\n" + preview
        else:
            text += "\n\nУ вас пока нет будущих репетиций."
        await edit_screen(callback, text, rehearsals_menu_keyboard(await get_webapp_base_url()))
    elif section == "bands":
        bands = sorted(await api.list_bands(callback.from_user.id), key=lambda item: item.get("created_at", ""), reverse=True)
        preview = "\n".join(f"- {band['name']}" for band in bands[:5])
        text = "Коллективы"
        if preview:
            text += "\n\nМои коллективы:\n" + preview
        else:
            text += "\n\nУ вас пока нет коллективов."
        await edit_screen(callback, text, bands_menu_keyboard())
    elif section == "concerts":
        items = await api.list_events()
        open_events = [item for item in items if item.get("status") == "open"]
        preview = "\n".join(f"- {item['event_date']} | {item['title']}" for item in open_events[:3])
        text = "Концерты"
        if preview:
            text += "\n\nОткрытые концерты:\n" + preview
        else:
            text += "\n\nСейчас нет открытых концертов."
        await edit_screen(callback, text, concerts_menu_keyboard())
    elif section == "profile":
        await inline_profile(callback)
        return
    else:
        await answer_callback(callback, "Раздел не найден.", show_alert=True)
        return
    await answer_callback(callback)


@router.callback_query(F.data == "flow:cancel")
async def inline_cancel_flow(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user = await api.get_user(callback.from_user.id)
    await edit_schedule_menu(callback, user, t(user.get("language", "ru"), "main_menu"))
    await answer_callback(callback)


@router.callback_query(F.data == "menu:schedule")
async def inline_schedule(callback: CallbackQuery) -> None:
    user = await api.get_user(callback.from_user.id)
    await edit_schedule_menu(callback, user, "Расписание на ближайшие 7 дней")
    await answer_callback(callback)


@router.callback_query(F.data.startswith("user_bookings:"))
async def inline_user_bookings(callback: CallbackQuery, state: FSMContext) -> None:
    if not await has_full_access_for_callback(callback):
        return
    page = int(callback.data.rsplit(":", maxsplit=1)[-1])
    rows = await api.my_bookings(callback.from_user.id)
    await state.update_data(user_bookings=rows)
    if not rows:
        await edit_screen(callback, "У вас пока нет будущих репетиций.", rehearsals_menu_keyboard(await get_webapp_base_url()))
        await answer_callback(callback)
        return
    await edit_screen(callback, "Мои репетиции", user_bookings_list_keyboard(rows, page))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("user_booking:"))
async def inline_user_booking_detail(callback: CallbackQuery, state: FSMContext) -> None:
    booking_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    data = await state.get_data()
    rows = data.get("user_bookings") or await api.my_bookings(callback.from_user.id)
    await state.update_data(user_bookings=rows)
    booking = next((item for item in rows if item["id"] == booking_id), None)
    if booking is None:
        await answer_callback(callback, "Бронь не найдена. Обновите список.", show_alert=True)
        return
    await edit_screen(callback, format_booking_card(booking), user_booking_detail_keyboard(booking_id))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("user_booking_edit:"))
async def inline_user_booking_edit(callback: CallbackQuery, state: FSMContext) -> None:
    _, booking_id_raw, field = callback.data.split(":")
    labels = {
        "date": "Введите новую дату в формате YYYY-MM-DD:",
        "time": "Введите новое время в формате 14:00-15:30:",
        "song": "Введите новую песню/название. Для пустого значения отправьте '-'.",
    }
    if field not in labels:
        await answer_callback(callback, "Поле не поддерживается.", show_alert=True)
        return
    await state.update_data(user_booking_edit_id=int(booking_id_raw), user_booking_edit_field=field)
    await state.set_state(BookingFlow.edit_value)
    await callback.message.answer(labels[field], reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data.startswith("user_booking_delete:"))
async def inline_user_booking_delete(callback: CallbackQuery) -> None:
    booking_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    await edit_screen(callback, "Удалить эту бронь и освободить время?", user_booking_delete_confirm_keyboard(booking_id))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("user_booking_delete_confirm:"))
async def inline_user_booking_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    booking_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    try:
        deleted = await api.delete_booking(callback.from_user.id, booking_id)
    except RuntimeError as exc:
        await edit_screen(callback, f"Не получилось удалить бронь: {exc}", user_booking_detail_keyboard(booking_id))
        await answer_callback(callback)
        return
    await state.update_data(user_bookings=await api.my_bookings(callback.from_user.id))
    await refresh_all_schedule_menus(callback.message.bot)
    await edit_screen(
        callback,
        f"Бронь удалена: {deleted['booking_date']} {deleted['start_time'][:5]}-{deleted['end_time'][:5]} | {deleted['band_name']}",
        rehearsals_menu_keyboard(await get_webapp_base_url()),
    )
    await answer_callback(callback, "Готово")


@router.callback_query(F.data == "menu:profile")
async def inline_profile(callback: CallbackQuery) -> None:
    if not await has_full_access_for_callback(callback):
        return
    user = await api.get_user(callback.from_user.id)
    text = (
        f"Имя: {user['first_name']}\n"
        f"Фамилия: {user['last_name']}\n"
        f"Группа: {user['study_group']}\n"
        f"Язык: {user['language']}\n"
        f"Telegram: @{user['telegram_username'] or '-'}\n"
        f"Уведомления: {'включены' if user['notifications_enabled'] else 'выключены'}"
    )
    await edit_screen(callback, text, profile_inline_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data == "menu:bands")
async def inline_bands(callback: CallbackQuery) -> None:
    if not await has_full_access_for_callback(callback):
        return
    bands = await api.list_bands(callback.from_user.id)
    if not bands:
        await edit_screen(callback, "У вас пока нет коллективов.", bands_menu_keyboard())
        await answer_callback(callback)
        return
    bands = sorted(bands, key=lambda item: item.get("created_at", ""), reverse=True)
    await edit_screen(callback, "Мои коллективы", user_bands_list_keyboard(bands))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("user_bands:"))
async def inline_user_bands_page(callback: CallbackQuery) -> None:
    if not await has_full_access_for_callback(callback):
        return
    page = int(callback.data.rsplit(":", maxsplit=1)[-1])
    bands = sorted(await api.list_bands(callback.from_user.id), key=lambda item: item.get("created_at", ""), reverse=True)
    text = "Мои коллективы" if bands else "У вас пока нет коллективов."
    await edit_screen(callback, text, user_bands_list_keyboard(bands, page))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("user_band:"))
async def inline_user_band_detail(callback: CallbackQuery) -> None:
    if not await has_full_access_for_callback(callback):
        return
    _, band_id_raw, page_raw = callback.data.split(":")
    band = await api.get_band(int(band_id_raw))
    is_owner = user_is_band_owner(band, callback.from_user.id)
    await edit_screen(callback, format_band_card(band), user_band_detail_keyboard(band["id"], is_owner, int(page_raw)))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("user_band_edit:"))
async def inline_user_band_edit(callback: CallbackQuery, state: FSMContext) -> None:
    _, band_id_raw, field = callback.data.split(":")
    if field != "name":
        await answer_callback(callback, "Поле не поддерживается.", show_alert=True)
        return
    await state.update_data(user_band_edit_id=int(band_id_raw), user_band_edit_field=field)
    await state.set_state(BandFlow.edit_value)
    await callback.message.answer("Введите новое название коллектива:", reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data.startswith("user_band_members:"))
async def inline_user_band_members(callback: CallbackQuery) -> None:
    _, band_id_raw, action, page_raw = callback.data.split(":")
    band = await api.get_band(int(band_id_raw))
    if not user_is_band_owner(band, callback.from_user.id):
        await answer_callback(callback, "Редактировать коллектив может только лидер.", show_alert=True)
        return
    titles = {
        "owner": "Выберите нового лидера коллектива:",
        "role": "Выберите участника, которому нужно изменить роль:",
        "remove": "Выберите участника, которого нужно убрать из коллектива:",
    }
    await edit_screen(callback, titles.get(action, "Участники:"), user_band_members_keyboard(band, action, int(page_raw)))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("user_band_member:"))
async def inline_user_band_member_action(callback: CallbackQuery, state: FSMContext) -> None:
    _, band_id_raw, member_id_raw, action = callback.data.split(":")
    band_id = int(band_id_raw)
    member_id = int(member_id_raw)
    band = await api.get_band(band_id)
    if not user_is_band_owner(band, callback.from_user.id):
        await answer_callback(callback, "Редактировать коллектив может только лидер.", show_alert=True)
        return
    if action == "owner":
        band = await api.transfer_band_owner(callback.from_user.id, band_id, member_id)
        await edit_screen(callback, "Лидер обновлен.\n\n" + format_band_card(band), user_band_detail_keyboard(band_id, user_is_band_owner(band, callback.from_user.id)))
    elif action == "role":
        await state.update_data(user_band_edit_id=band_id, user_band_member_id=member_id, user_band_edit_field="member_role")
        await state.set_state(BandFlow.edit_value)
        await callback.message.answer("Введите новую роль участника:", reply_markup=flow_cancel_keyboard())
    elif action == "remove":
        await edit_screen(callback, "Удалить участника из коллектива?", user_member_remove_confirm_keyboard(band_id, member_id))
    else:
        await answer_callback(callback, "Действие не поддерживается.", show_alert=True)
        return
    await answer_callback(callback)


@router.callback_query(F.data.startswith("user_band_member_remove_confirm:"))
async def inline_user_band_member_remove_confirm(callback: CallbackQuery) -> None:
    _, band_id_raw, member_id_raw = callback.data.split(":")
    band_id = int(band_id_raw)
    member_id = int(member_id_raw)
    try:
        deleted = await api.remove_band_member(callback.from_user.id, band_id, member_id)
    except RuntimeError as exc:
        await edit_screen(callback, f"Не получилось удалить участника: {exc}", user_band_detail_keyboard(band_id, True))
        await answer_callback(callback)
        return
    band = await api.get_band(band_id)
    await edit_screen(callback, f"Участник удален: {deleted['user_name']}\n\n" + format_band_card(band), user_band_detail_keyboard(band_id, True))
    await answer_callback(callback, "Готово")


@router.callback_query(F.data == "menu:create_band")
async def inline_create_band(callback: CallbackQuery, state: FSMContext) -> None:
    if not await has_full_access_for_callback(callback):
        return
    await state.set_state(BandFlow.create_name)
    await callback.message.answer("Введите название коллектива:", reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data == "menu:join_band")
async def inline_join_band(callback: CallbackQuery, state: FSMContext) -> None:
    if not await has_full_access_for_callback(callback):
        return
    await state.set_state(BandFlow.join_code)
    await callback.message.answer("Введите код приглашения:", reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data == "menu:booking")
async def inline_booking(callback: CallbackQuery, state: FSMContext) -> None:
    if not await has_full_access_for_callback(callback):
        return
    bands = await api.list_bands(callback.from_user.id)
    personal_settings = await api.personal_booking_settings()
    allow_personal = bool(personal_settings.get("personal_bookings_enabled"))
    if not bands and not allow_personal:
        await edit_screen(callback, "Сначала создайте коллектив или вступите в него.", main_menu_inline(False))
        await answer_callback(callback)
        return
    bands = sorted(bands, key=lambda item: item.get("created_at", ""), reverse=True)
    await state.update_data(booking_bands=bands, allow_personal_booking=allow_personal)
    await state.set_state(BookingFlow.band_id)
    await edit_screen(callback, "Выберите коллектив или личную репетицию:", booking_bands_keyboard(bands, allow_personal=allow_personal))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("booking:bands:"))
async def inline_booking_bands_page(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    bands = data.get("booking_bands", [])
    if not bands:
        await inline_booking(callback, state)
        return
    page = int(callback.data.rsplit(":", maxsplit=1)[-1])
    await edit_screen(callback, "Выберите коллектив или личную репетицию:", booking_bands_keyboard(bands, page, allow_personal=data.get("allow_personal_booking", False)))
    await answer_callback(callback)


@router.callback_query(F.data == "booking:personal")
async def inline_booking_personal(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("allow_personal_booking", False):
        settings = await api.personal_booking_settings()
        if not settings.get("personal_bookings_enabled"):
            await answer_callback(callback, "Личные репетиции сейчас выключены админами.", show_alert=True)
            return
    await state.update_data(band_id=None, band_name="Личная репетиция", is_personal_booking=True, purpose="personal")
    days = []
    today = datetime.now().date()
    for offset in range(8):
        target_date = today + timedelta(days=offset)
        day_slots = await api.available_slots(target_date, 30)
        if day_slots:
            days.append(
                {
                    "date": target_date.isoformat(),
                    "label": format_booking_day_button(target_date, len(day_slots)),
                }
            )
    if not days:
        await state.clear()
        user = await api.get_user(callback.from_user.id)
        await edit_screen(callback, "На ближайшие 7 дней нет свободного времени.", main_menu_inline(user.get("role") == "admin"))
        await answer_callback(callback)
        return
    await state.update_data(booking_dates=days)
    await state.set_state(BookingFlow.booking_date)
    await edit_screen(callback, "Личная репетиция\nВыберите день:", booking_days_keyboard(days))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("booking:band:"))
async def inline_booking_band(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    bands = data.get("booking_bands", [])
    band_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    band = next((item for item in bands if item["id"] == band_id), None)
    if band is None:
        await answer_callback(callback, "Коллектив не найден, начните заново.", show_alert=True)
        return
    await state.update_data(band_id=band_id, band_name=band["name"], is_personal_booking=False)
    days = []
    today = datetime.now().date()
    for offset in range(8):
        target_date = today + timedelta(days=offset)
        day_slots = await api.available_slots(target_date, 30, band_id)
        if day_slots:
            days.append(
                {
                    "date": target_date.isoformat(),
                    "label": format_booking_day_button(target_date, len(day_slots)),
                }
            )
    if not days:
        await state.clear()
        user = await api.get_user(callback.from_user.id)
        await edit_screen(
            callback,
            "На ближайшие 7 дней нет свободного времени.",
            main_menu_inline(user.get("role") == "admin"),
        )
        await answer_callback(callback)
        return
    await state.update_data(booking_dates=days)
    await state.set_state(BookingFlow.booking_date)
    await edit_screen(callback, f"Коллектив: {band['name']}\nВыберите день:", booking_days_keyboard(days))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("booking:date:"))
async def inline_booking_date(callback: CallbackQuery, state: FSMContext) -> None:
    selected_date = callback.data.rsplit(":", maxsplit=1)[-1]
    data = await state.get_data()
    band_id = data.get("band_id")
    slots = [
        {"date": selected_date, **slot}
        for slot in await api.available_slots(date.fromisoformat(selected_date), 30, band_id)
    ]
    if not slots:
        await answer_callback(callback, "На этот день уже нет свободных слотов.", show_alert=True)
        return
    await state.update_data(selected_date=selected_date, slots=slots)
    await state.set_state(BookingFlow.slot)
    await edit_screen(callback, f"{format_booking_date(selected_date)}\nВыберите время начала:", booking_times_keyboard(slots))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("booking:times:"))
async def inline_booking_times_page(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    slots = data.get("slots", [])
    selected_date = data.get("selected_date")
    if not slots or not selected_date:
        await answer_callback(callback, "Слоты устарели, начните заново.", show_alert=True)
        return
    page = int(callback.data.rsplit(":", maxsplit=1)[-1])
    await edit_screen(callback, f"{format_booking_date(selected_date)}\nВыберите время начала:", booking_times_keyboard(slots, page))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("booking:slot:"))
async def inline_booking_slot(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    slots = data.get("slots", [])
    index = int(callback.data.rsplit(":", maxsplit=1)[-1])
    if index < 0 or index >= len(slots):
        await answer_callback(callback, "Слот не найден.", show_alert=True)
        return
    slot = slots[index]
    band_id = data.get("band_id")
    end_options = await get_end_time_options(slot["date"], slot["start_time"], band_id)
    if not end_options:
        await answer_callback(callback, "Для этого старта нет доступного окончания.", show_alert=True)
        return
    limit_status = None if band_id is None else await api.booking_limit_status(callback.from_user.id, band_id, date.fromisoformat(slot["date"]))
    await state.update_data(selected_start_slot=slot, end_options=end_options)
    await state.set_state(BookingFlow.end_time)
    await edit_screen(
        callback,
        build_end_time_text(slot["date"], slot["start_time"], limit_status, end_options),
        booking_end_times_keyboard(end_options),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("booking:ends:"))
async def inline_booking_end_times_page(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    start_slot = data.get("selected_start_slot")
    end_options = data.get("end_options", [])
    if not start_slot or not end_options:
        await answer_callback(callback, "Слоты устарели, начните заново.", show_alert=True)
        return
    band_id = data.get("band_id")
    limit_status = None if band_id is None else await api.booking_limit_status(callback.from_user.id, band_id, date.fromisoformat(start_slot["date"]))
    page = int(callback.data.rsplit(":", maxsplit=1)[-1])
    await edit_screen(
        callback,
        build_end_time_text(start_slot["date"], start_slot["start_time"], limit_status, end_options),
        booking_end_times_keyboard(end_options, page),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("booking:end:"))
async def inline_booking_end_time(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    end_options = data.get("end_options", [])
    index = int(callback.data.rsplit(":", maxsplit=1)[-1])
    if index < 0 or index >= len(end_options):
        await answer_callback(callback, "Время окончания не найдено.", show_alert=True)
        return
    slot = end_options[index]
    await state.update_data(selected_slot=slot)
    if data.get("is_personal_booking"):
        await state.update_data(purpose="personal")
        await state.set_state(BookingFlow.song_title)
        await edit_screen(callback, "Что будете репетировать? Если пока не ясно, отправьте '-'.", booking_song_keyboard())
        await answer_callback(callback)
        return
    await state.set_state(BookingFlow.purpose)
    await edit_screen(
        callback,
        f"Выбрано: {format_booking_date(slot['date'])} {slot['start_time'][:5]}-{slot['end_time'][:5]}\n"
        "Выберите цель репетиции:",
        booking_purpose_keyboard(),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("booking:purpose:"))
async def inline_booking_purpose(callback: CallbackQuery, state: FSMContext) -> None:
    purpose = callback.data.rsplit(":", maxsplit=1)[-1]
    if purpose not in {"concert", "self"}:
        await answer_callback(callback, "Неверная цель.", show_alert=True)
        return
    await state.update_data(purpose=purpose)
    await state.set_state(BookingFlow.song_title)
    await edit_screen(callback, "Какую песню репетируете? Если пока не ясно, отправьте '-'.", booking_song_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data.startswith("booking:back:"))
async def inline_booking_back(callback: CallbackQuery, state: FSMContext) -> None:
    step = callback.data.rsplit(":", maxsplit=1)[-1]
    data = await state.get_data()
    if step == "bands":
        bands = data.get("booking_bands", [])
        if not bands:
            await inline_booking(callback, state)
            return
        await state.set_state(BookingFlow.band_id)
        await edit_screen(callback, "Выберите коллектив или личную репетицию:", booking_bands_keyboard(bands, allow_personal=data.get("allow_personal_booking", False)))
    elif step == "days":
        days = data.get("booking_dates", [])
        band_name = data.get("band_name", "коллектив")
        if not days:
            await answer_callback(callback, "Дни устарели, начните заново.", show_alert=True)
            return
        await state.set_state(BookingFlow.booking_date)
        await edit_screen(callback, f"Коллектив: {band_name}\nВыберите день:", booking_days_keyboard(days))
    elif step == "times":
        slots = data.get("slots", [])
        selected_date = data.get("selected_date")
        if not slots or not selected_date:
            await answer_callback(callback, "Слоты устарели, начните заново.", show_alert=True)
            return
        await state.set_state(BookingFlow.slot)
        await edit_screen(callback, f"{format_booking_date(selected_date)}\nВыберите время начала:", booking_times_keyboard(slots))
    elif step == "ends":
        start_slot = data.get("selected_start_slot")
        end_options = data.get("end_options", [])
        if not start_slot or not end_options:
            await answer_callback(callback, "Время устарело, начните заново.", show_alert=True)
            return
        band_id = data.get("band_id")
        limit_status = None if band_id is None else await api.booking_limit_status(callback.from_user.id, band_id, date.fromisoformat(start_slot["date"]))
        await state.set_state(BookingFlow.end_time)
        await edit_screen(
            callback,
            build_end_time_text(start_slot["date"], start_slot["start_time"], limit_status, end_options),
            booking_end_times_keyboard(end_options),
        )
    elif step == "purpose":
        slot = data.get("selected_slot")
        if not slot:
            await answer_callback(callback, "Выбор устарел, начните заново.", show_alert=True)
            return
        if data.get("is_personal_booking"):
            start_slot = data.get("selected_start_slot")
            end_options = data.get("end_options", [])
            if not start_slot or not end_options:
                await answer_callback(callback, "Время устарело, начните заново.", show_alert=True)
                return
            await state.set_state(BookingFlow.end_time)
            await edit_screen(
                callback,
                build_end_time_text(start_slot["date"], start_slot["start_time"], None, end_options),
                booking_end_times_keyboard(end_options),
            )
            await answer_callback(callback)
            return
        await state.set_state(BookingFlow.purpose)
        await edit_screen(
            callback,
            f"Выбрано: {format_booking_date(slot['date'])} {slot['start_time'][:5]}-{slot['end_time'][:5]}\n"
            "Выберите цель репетиции:",
            booking_purpose_keyboard(),
        )
    else:
        await answer_callback(callback, "Не знаю, куда вернуться.", show_alert=True)
        return
    await answer_callback(callback)


@router.callback_query(F.data == "menu:events")
async def inline_events(callback: CallbackQuery) -> None:
    if not await has_full_access_for_callback(callback):
        return
    items = await api.list_events()
    items = [item for item in items if item.get("status") == "open"]
    if not items:
        await edit_screen(callback, "Сейчас нет открытых концертов.", concerts_menu_keyboard())
        await answer_callback(callback)
        return
    user = await api.get_user(callback.from_user.id)
    await edit_screen(
        callback,
        "Выберите концерт, на который хотите подать номер:",
        user_events_keyboard(items),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("event_applications:"))
async def inline_event_applications(callback: CallbackQuery, state: FSMContext) -> None:
    if not await has_full_access_for_callback(callback):
        return
    page = int(callback.data.rsplit(":", maxsplit=1)[-1])
    applications = await api.my_event_applications(callback.from_user.id)
    await state.update_data(event_applications=applications)
    if not applications:
        await edit_screen(callback, "У вас пока нет поданных заявок.", concerts_menu_keyboard())
        await answer_callback(callback)
        return
    await edit_screen(callback, "Мои заявки на концерты", event_applications_keyboard(applications, page))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("event_application:"))
async def inline_event_application_detail(callback: CallbackQuery, state: FSMContext) -> None:
    application_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    try:
        application = await api.get_event_application(callback.from_user.id, application_id)
    except RuntimeError as exc:
        await edit_screen(callback, f"Не получилось открыть заявку: {exc}", concerts_menu_keyboard())
        await answer_callback(callback)
        return
    await state.update_data(current_event_application=application)
    await edit_screen(callback, format_application_card(application), event_application_detail_keyboard(application_id))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("event_application_edit:"))
async def inline_event_application_edit(callback: CallbackQuery, state: FSMContext) -> None:
    _, application_id_raw, field = callback.data.split(":")
    application_id = int(application_id_raw)
    application = await api.get_event_application(callback.from_user.id, application_id)
    if field == "song":
        await state.update_data(event_application_edit_id=application_id)
        await state.set_state(EventFlow.application_song_title)
        await callback.message.answer("Введите новое название песни/номера:", reply_markup=flow_cancel_keyboard())
    elif field == "members":
        bands = await api.list_bands(callback.from_user.id)
        band = next((item for item in bands if item["id"] == application["band_id"]), None)
        if band is None:
            await answer_callback(callback, "Коллектив не найден.", show_alert=True)
            return
        selected_ids = []
        selected_telegram_ids = {member["user"]["telegram_id"] for member in application.get("members", [])}
        for member in band["members"]:
            if member["user"]["telegram_id"] in selected_telegram_ids:
                selected_ids.append(member["id"])
        await state.update_data(
            event_application_edit_id=application_id,
            event_application_members=band["members"],
            event_application_selected_member_ids=selected_ids,
        )
        await state.set_state(EventFlow.application_members)
        await edit_screen(
            callback,
            "Выберите участников номера. Нажмите на участника, чтобы убрать или вернуть его в список.",
            event_application_members_keyboard(band["members"], selected_ids, application_id),
        )
    else:
        await answer_callback(callback, "Поле не поддерживается.", show_alert=True)
        return
    await answer_callback(callback)


@router.callback_query(F.data.startswith("event_application_member:"))
async def inline_event_application_member_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    member_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    data = await state.get_data()
    application_id = int(data["event_application_edit_id"])
    selected = set(data.get("event_application_selected_member_ids", []))
    if member_id in selected:
        selected.remove(member_id)
    else:
        selected.add(member_id)
    selected_ids = sorted(selected)
    await state.update_data(event_application_selected_member_ids=selected_ids)
    await edit_screen(
        callback,
        "Выберите участников номера. Нажмите на участника, чтобы убрать или вернуть его в список.",
        event_application_members_keyboard(data.get("event_application_members", []), selected_ids, application_id),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("event_application_members_save:"))
async def inline_event_application_members_save(callback: CallbackQuery, state: FSMContext) -> None:
    application_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    data = await state.get_data()
    member_ids = data.get("event_application_selected_member_ids", [])
    if not member_ids:
        await answer_callback(callback, "Выберите хотя бы одного участника.", show_alert=True)
        return
    try:
        await api.update_event_application(callback.from_user.id, application_id, {"member_ids": member_ids})
        application = await api.get_event_application(callback.from_user.id, application_id)
    except RuntimeError as exc:
        await answer_callback(callback, f"Не получилось сохранить: {exc}", show_alert=True)
        return
    await state.clear()
    await edit_screen(callback, "Заявка обновлена.\n\n" + format_application_card(application), event_application_detail_keyboard(application_id))
    await answer_callback(callback, "Готово")


@router.callback_query(F.data.startswith("event_application_delete:"))
async def inline_event_application_delete(callback: CallbackQuery) -> None:
    application_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    await edit_screen(callback, "Отозвать заявку на концерт?", event_application_delete_confirm_keyboard(application_id))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("event_application_delete_confirm:"))
async def inline_event_application_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    application_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    try:
        deleted = await api.delete_event_application(callback.from_user.id, application_id)
    except RuntimeError as exc:
        await edit_screen(callback, f"Не получилось отозвать заявку: {exc}", event_application_detail_keyboard(application_id))
        await answer_callback(callback)
        return
    await state.clear()
    await edit_screen(
        callback,
        f"Заявка отозвана: {deleted['event_title']} | {deleted['band_name']} | {deleted.get('song_title') or '-'}",
        concerts_menu_keyboard(),
    )
    await answer_callback(callback, "Готово")


@router.callback_query(F.data == "menu:admin")
async def inline_admin(callback: CallbackQuery) -> None:
    user = await api.get_user(callback.from_user.id)
    if user["role"] != "admin":
        await edit_screen(callback, "Нет доступа.")
        await answer_callback(callback)
        return
    await edit_screen(callback, "Админ-меню", admin_inline_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data.startswith("event_apply:"))
async def inline_event_apply(callback: CallbackQuery, state: FSMContext) -> None:
    if not await has_full_access_for_callback(callback):
        return
    event_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    bands = await api.list_bands(callback.from_user.id)
    owner_bands = [band for band in bands if any(member.get("is_owner") for member in band["members"] if member["user"]["telegram_id"] == callback.from_user.id)]
    if not owner_bands:
        await answer_callback(callback, "Подать номер может только лидер коллектива.", show_alert=True)
        return
    await state.update_data(event_id=event_id, bands=owner_bands)
    await state.set_state(EventFlow.band_id)
    await edit_screen(callback, "Выберите коллектив, который подает номер:", event_bands_keyboard(owner_bands))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("event_band:"))
async def inline_event_band(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    band_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    band = next((item for item in data.get("bands", []) if item["id"] == band_id), None)
    if band is None:
        await answer_callback(callback, "Коллектив не найден.", show_alert=True)
        return
    await state.update_data(band_id=band_id, band_name=band["name"], members=band["members"], selected_member_ids=[member["id"] for member in band["members"]])
    await state.set_state(EventFlow.song_title)
    await edit_screen(callback, f"Коллектив: {band['name']}\nВведите название песни или номера:", flow_cancel_keyboard())
    await answer_callback(callback)


@router.message(EventFlow.song_title)
async def event_song_title(message: Message, state: FSMContext) -> None:
    song_title = message.text.strip()
    if not song_title:
        await message.answer("Название не должно быть пустым.", reply_markup=flow_cancel_keyboard())
        return
    if len(song_title) > 150:
        await message.answer("Название должно быть не длиннее 150 символов.", reply_markup=flow_cancel_keyboard())
        return
    data = await state.get_data()
    await state.update_data(song_title=song_title)
    await state.set_state(EventFlow.members)
    await message.answer(
        "Выберите участников номера. Нажмите на участника, чтобы убрать или вернуть его в список.",
        reply_markup=event_members_keyboard(data["members"], data.get("selected_member_ids", [])),
    )


@router.message(EventFlow.application_song_title)
async def event_application_song_title_save(message: Message, state: FSMContext) -> None:
    song_title = message.text.strip()
    if not song_title:
        await message.answer("Название не должно быть пустым.", reply_markup=flow_cancel_keyboard())
        return
    if len(song_title) > 150:
        await message.answer("Название должно быть не длиннее 150 символов.", reply_markup=flow_cancel_keyboard())
        return
    data = await state.get_data()
    application_id = int(data["event_application_edit_id"])
    try:
        await api.update_event_application(message.from_user.id, application_id, {"song_title": song_title})
        application = await api.get_event_application(message.from_user.id, application_id)
    except RuntimeError as exc:
        await state.clear()
        await message.answer(f"Не получилось обновить заявку: {exc}", reply_markup=concerts_menu_keyboard())
        return
    await state.clear()
    await message.answer(
        "Заявка обновлена.\n\n" + format_application_card(application),
        reply_markup=event_application_detail_keyboard(application_id),
    )


@router.callback_query(F.data.startswith("event_member:"))
async def inline_event_member_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    member_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    data = await state.get_data()
    selected = set(data.get("selected_member_ids", []))
    if member_id in selected:
        selected.remove(member_id)
    else:
        selected.add(member_id)
    selected_ids = sorted(selected)
    await state.update_data(selected_member_ids=selected_ids)
    await edit_screen(
        callback,
        "Выберите участников номера. Нажмите на участника, чтобы убрать или вернуть его в список.",
        event_members_keyboard(data.get("members", []), selected_ids),
    )
    await answer_callback(callback)


@router.callback_query(F.data == "event_submit")
async def inline_event_submit(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    member_ids = data.get("selected_member_ids", [])
    if not member_ids:
        await answer_callback(callback, "Выберите хотя бы одного участника.", show_alert=True)
        return
    try:
        await api.submit_application(callback.from_user.id, data["event_id"], data["band_id"], data["song_title"], member_ids)
    except RuntimeError as exc:
        await answer_callback(callback, f"Не получилось отправить: {exc}", show_alert=True)
        return
    await state.clear()
    user = await api.get_user(callback.from_user.id)
    await edit_schedule_menu(callback, user, f"Номер отправлен: {data['song_title']}")
    await answer_callback(callback)


@router.callback_query(F.data == "event_back:bands")
async def inline_event_back_bands(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    bands = data.get("bands", [])
    if not bands:
        await answer_callback(callback, "Список устарел.", show_alert=True)
        return
    await state.set_state(EventFlow.band_id)
    await edit_screen(callback, "Выберите коллектив, который подает номер:", event_bands_keyboard(bands))
    await answer_callback(callback)


@router.callback_query(F.data == "profile:first_name")
async def inline_edit_first_name(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileFlow.first_name)
    await callback.message.answer("Введите новое имя:", reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data == "profile:last_name")
async def inline_edit_last_name(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileFlow.last_name)
    await callback.message.answer("Введите новую фамилию:", reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data == "profile:study_group")
async def inline_edit_group(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileFlow.study_group)
    await callback.message.answer("Введите новую учебную группу:", reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data == "profile:language")
async def inline_edit_language(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileFlow.language)
    await callback.message.answer("Выберите язык:", reply_markup=language_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data == "profile:notifications")
async def inline_toggle_notifications(callback: CallbackQuery) -> None:
    user = await api.get_user(callback.from_user.id)
    updated = await api.update_settings(callback.from_user.id, {"notifications_enabled": not user["notifications_enabled"]})
    await edit_schedule_menu(callback, updated, "Настройки обновлены.")
    await answer_callback(callback)


@router.callback_query(F.data == "profile:event_history")
async def inline_event_history(callback: CallbackQuery) -> None:
    if not await has_full_access_for_callback(callback):
        return
    rows = await api.event_history(callback.from_user.id)
    await edit_screen(callback, format_event_history(rows), profile_event_history_keyboard(rows))
    await answer_callback(callback)


@router.callback_query(F.data == "profile_event_history:add")
async def inline_personal_event_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not await has_full_access_for_callback(callback):
        return
    await state.set_state(ProfileFlow.personal_event_title)
    await callback.message.answer("Введите название события:", reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data.startswith("profile_personal_event:"))
async def inline_personal_event_detail(callback: CallbackQuery) -> None:
    item_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    try:
        item = await api.get_personal_event_history(callback.from_user.id, item_id)
    except RuntimeError as exc:
        await edit_screen(callback, f"Не получилось открыть событие: {exc}", profile_event_history_keyboard())
        await answer_callback(callback)
        return
    await edit_screen(callback, format_personal_event_card(item), profile_personal_event_detail_keyboard(item_id))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("profile_personal_event_edit:"))
async def inline_personal_event_edit(callback: CallbackQuery, state: FSMContext) -> None:
    _, item_id_raw, field = callback.data.split(":")
    item_id = int(item_id_raw)
    labels = {
        "title": "Введите новое название события:",
        "event_date": "Введите новую дату в формате YYYY-MM-DD:",
        "location": "Введите новое место. Для пустого значения отправьте '-'.",
        "role_at_event": "Введите вашу роль на событии. Например: вокалист, гитарист, организатор.",
    }
    if field not in labels:
        await answer_callback(callback, "Поле не поддерживается.", show_alert=True)
        return
    await state.update_data(personal_event_edit_id=item_id, personal_event_edit_field=field)
    await state.set_state(ProfileFlow.personal_event_edit_value)
    await callback.message.answer(labels[field], reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data.startswith("profile_personal_event_delete:"))
async def inline_personal_event_delete(callback: CallbackQuery) -> None:
    item_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    await edit_screen(callback, "Удалить личное событие из истории?", profile_personal_event_delete_confirm_keyboard(item_id))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("profile_personal_event_delete_confirm:"))
async def inline_personal_event_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    item_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    try:
        deleted = await api.delete_personal_event_history(callback.from_user.id, item_id)
    except RuntimeError as exc:
        await edit_screen(callback, f"Не получилось удалить событие: {exc}", profile_event_history_keyboard())
        await answer_callback(callback)
        return
    await state.clear()
    rows = await api.event_history(callback.from_user.id)
    await edit_screen(
        callback,
        f"Событие удалено: {deleted['event_date']} | {deleted['event_title']}\n\n{format_event_history(rows)}",
        profile_event_history_keyboard(rows),
    )
    await answer_callback(callback, "Готово")


@router.callback_query(F.data == "admin:pending_users")
async def inline_pending_users(callback: CallbackQuery) -> None:
    users = await api.list_users(callback.from_user.id, "pending")
    text = "Заявки на регистрацию" if users else "Новых заявок нет."
    await edit_screen(callback, text, admin_user_list_keyboard(users, "pending"))
    await answer_callback(callback)


@router.callback_query(F.data == "admin:action_history")
async def inline_action_history(callback: CallbackQuery) -> None:
    rows = await api.action_history(callback.from_user.id)
    if not rows:
        await edit_screen(callback, "История действий пока пустая.", admin_actions_keyboard())
        await answer_callback(callback)
        return
    lines = ["Последние действия:"]
    for item in rows[:30]:
        created_at = item["created_at"].replace("T", " ")[:16]
        lines.append(f"{created_at} | {item['actor_name']} | {item['description'] or item['action']}")
    await edit_screen(callback, "\n".join(lines), admin_actions_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data == "admin:back")
async def inline_admin_back(callback: CallbackQuery) -> None:
    await edit_screen(callback, "Админ-меню", admin_inline_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin_section:"))
async def inline_admin_section(callback: CallbackQuery) -> None:
    section = callback.data.split(":", maxsplit=1)[1]
    if section == "users":
        await edit_screen(callback, "Пользователи и заявки", admin_users_keyboard())
    elif section == "actions":
        await edit_screen(callback, "История действий", admin_actions_keyboard())
    elif section == "schedule":
        await edit_screen(callback, "Репетиции и часы кабинета", admin_schedule_keyboard())
    elif section == "bands":
        bands = await api.list_all_bands(callback.from_user.id)
        text = "Коллективы" if bands else "Коллективов пока нет."
        await edit_screen(callback, text, admin_bands_keyboard(bands))
    elif section == "settings":
        personal = await api.get_personal_bookings_enabled(callback.from_user.id)
        status = "включены" if personal.get("personal_bookings_enabled") else "выключены"
        await edit_screen(callback, f"Настройки бронирования\n\nЛичные репетиции: {status}", admin_settings_keyboard())
    elif section == "events":
        events = await api.list_events()
        text = "Концерты" if events else "Концертов пока нет."
        await edit_screen(callback, text, admin_events_keyboard(events))
    elif section == "admins":
        users = await api.list_admins(callback.from_user.id)
        await edit_screen(callback, format_admins_list(users), admin_admins_keyboard())
    elif section == "guide":
        await edit_screen(callback, admin_guide_text(), admin_inline_keyboard())
    else:
        await edit_screen(callback, "Админ-меню", admin_inline_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data == "admin_users:manual")
async def inline_admin_users_manual(callback: CallbackQuery) -> None:
    await edit_screen(
        callback,
        "Ручное действие по @username или Telegram ID",
        admin_users_manual_keyboard(),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin_users:"))
async def inline_admin_users_list(callback: CallbackQuery) -> None:
    _, status, page_raw = callback.data.split(":")
    page = int(page_raw)
    users = await api.list_users(callback.from_user.id, status)
    titles = {
        "pending": "Заявки на регистрацию",
        "approved": "Одобренные пользователи",
        "rejected": "Отклоненные пользователи",
        "banned": "Забаненные пользователи",
    }
    text = titles.get(status, "Пользователи")
    if not users:
        text += "\n\nСписок пуст."
    await edit_screen(callback, text, admin_user_list_keyboard(users, status, page))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin_user:"))
async def inline_admin_user_detail(callback: CallbackQuery) -> None:
    _, telegram_id, origin_status, page_raw = callback.data.split(":")
    user = await api.get_user(int(telegram_id))
    await edit_screen(callback, format_user_card(user), admin_user_detail_keyboard(user, origin_status, int(page_raw)))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin_user_action:"))
async def inline_admin_user_action(callback: CallbackQuery) -> None:
    _, action, telegram_id = callback.data.split(":")
    reason = "Действие из админ-панели" if action in {"reject", "ban", "delete"} else None
    user = await api.moderate_user(action, callback.from_user.id, telegram_id, reason)
    await edit_screen(callback, format_user_card(user), admin_user_detail_keyboard(user, user["status"], 0))
    await answer_callback(callback, "Готово")


@router.callback_query(F.data.startswith("admin_bands:"))
async def inline_admin_bands_list(callback: CallbackQuery) -> None:
    page = int(callback.data.rsplit(":", maxsplit=1)[-1])
    bands = await api.list_all_bands(callback.from_user.id)
    text = "Коллективы" if bands else "Коллективов пока нет."
    await edit_screen(callback, text, admin_bands_keyboard(bands, page))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin_band:"))
async def inline_admin_band_detail(callback: CallbackQuery) -> None:
    _, band_id_raw, page_raw = callback.data.split(":")
    band = await api.get_band(int(band_id_raw))
    await edit_screen(callback, format_band_card(band), admin_band_detail_keyboard(band["id"], int(page_raw)))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin_band_edit:"))
async def inline_admin_band_edit(callback: CallbackQuery, state: FSMContext) -> None:
    _, band_id_raw, field = callback.data.split(":")
    if field != "name":
        await answer_callback(callback, "Поле не поддерживается.", show_alert=True)
        return
    await state.update_data(admin_band_edit_id=int(band_id_raw), admin_band_edit_field=field)
    await state.set_state(AdminFlow.band_edit_value)
    await callback.message.answer("Введите новое название коллектива:", reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin_band_members:"))
async def inline_admin_band_members(callback: CallbackQuery) -> None:
    _, band_id_raw, action, page_raw = callback.data.split(":")
    band = await api.get_band(int(band_id_raw))
    titles = {
        "owner": "Выберите нового лидера коллектива:",
        "role": "Выберите участника, которому нужно изменить роль:",
        "remove": "Выберите участника, которого нужно убрать из коллектива:",
    }
    await edit_screen(callback, titles.get(action, "Участники:"), admin_band_members_keyboard(band, action, int(page_raw)))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin_band_member:"))
async def inline_admin_band_member_action(callback: CallbackQuery, state: FSMContext) -> None:
    _, band_id_raw, member_id_raw, action = callback.data.split(":")
    band_id = int(band_id_raw)
    member_id = int(member_id_raw)
    if action == "owner":
        band = await api.transfer_band_owner(callback.from_user.id, band_id, member_id)
        await edit_screen(callback, "Лидер обновлен.\n\n" + format_band_card(band), admin_band_detail_keyboard(band_id))
    elif action == "role":
        await state.update_data(admin_band_edit_id=band_id, admin_band_member_id=member_id, admin_band_edit_field="member_role")
        await state.set_state(AdminFlow.band_edit_value)
        await callback.message.answer("Введите новую роль участника:", reply_markup=flow_cancel_keyboard())
    elif action == "remove":
        await edit_screen(callback, "Удалить участника из коллектива?", admin_member_remove_confirm_keyboard(band_id, member_id))
    else:
        await answer_callback(callback, "Действие не поддерживается.", show_alert=True)
        return
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin_band_member_remove_confirm:"))
async def inline_admin_band_member_remove_confirm(callback: CallbackQuery) -> None:
    _, band_id_raw, member_id_raw = callback.data.split(":")
    band_id = int(band_id_raw)
    member_id = int(member_id_raw)
    try:
        deleted = await api.remove_band_member(callback.from_user.id, band_id, member_id)
    except RuntimeError as exc:
        await edit_screen(callback, f"Не получилось удалить участника: {exc}", admin_band_detail_keyboard(band_id))
        await answer_callback(callback)
        return
    band = await api.get_band(band_id)
    await edit_screen(callback, f"Участник удален: {deleted['user_name']}\n\n" + format_band_card(band), admin_band_detail_keyboard(band_id))
    await answer_callback(callback, "Готово")


@router.callback_query(F.data.startswith("admin_event:"))
async def inline_admin_event_detail(callback: CallbackQuery) -> None:
    event_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    events = await api.list_events()
    event = next((item for item in events if item["id"] == event_id), None)
    if event is None:
        await answer_callback(callback, "Концерт не найден.", show_alert=True)
        return
    await edit_screen(callback, format_event_card(event), admin_event_detail_keyboard(event_id, event["status"]))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin_event_toggle:"))
async def inline_admin_event_toggle(callback: CallbackQuery) -> None:
    event_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    events = await api.list_events()
    event = next((item for item in events if item["id"] == event_id), None)
    if event is None:
        await answer_callback(callback, "Концерт не найден.", show_alert=True)
        return
    new_status = "closed" if event["status"] == "open" else "open"
    updated = await api.update_event(callback.from_user.id, event_id, {"status": new_status})
    await edit_screen(callback, format_event_card(updated), admin_event_detail_keyboard(event_id, updated["status"]))
    await answer_callback(callback, "Статус обновлен")


@router.callback_query(F.data.startswith("admin_event_edit:"))
async def inline_admin_event_edit(callback: CallbackQuery, state: FSMContext) -> None:
    _, event_id, field = callback.data.split(":", maxsplit=2)
    labels = {
        "title": "новое название",
        "event_date": "новую дату YYYY-MM-DD",
        "location": "новое место или '-' чтобы очистить",
        "submission_deadline": "новый дедлайн YYYY-MM-DD или '-' чтобы очистить",
    }
    if field not in labels:
        await answer_callback(callback, "Поле не поддерживается.", show_alert=True)
        return
    await state.update_data(event_edit_id=int(event_id), event_edit_field=field)
    await state.set_state(AdminFlow.event_edit_value)
    await callback.message.answer(f"Введите {labels[field]}:", reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin_event_applications:"))
async def inline_admin_event_applications(callback: CallbackQuery) -> None:
    event_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    applications = await api.list_event_applications(callback.from_user.id, event_id)
    if not applications:
        await edit_screen(callback, "На этот концерт пока нет поданных номеров.", admin_event_applications_keyboard(event_id))
        await answer_callback(callback)
        return
    lines = ["Поданные номера:"]
    for index, application in enumerate(applications, start=1):
        lines.append(f"\n{index}. {application['song_title'] or '-'}")
        lines.append(f"Коллектив: {application['band_name']}")
        lines.append("Участники:")
        for member in application["members"]:
            user = member["user"]
            name = f"{user.get('last_name') or ''} {user.get('first_name') or ''}".strip() or f"@{user.get('telegram_username') or '-'}"
            lines.append(f"- {name} | {user.get('study_group') or '-'} | {member['instrument_role']}")
    await edit_screen(callback, "\n".join(lines), admin_event_applications_keyboard(event_id))
    await answer_callback(callback)


@router.callback_query(F.data == "admin:approve_user")
async def inline_approve_user_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFlow.approve_user)
    await callback.message.answer("Введите @username или Telegram ID пользователя для одобрения:", reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data == "admin:reject_user")
async def inline_reject_user_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFlow.reject_user)
    await callback.message.answer("Введите @username или Telegram ID и причину через пробел.", reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data == "admin:ban_user")
async def inline_ban_user_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFlow.ban_user)
    await callback.message.answer("Введите @username или Telegram ID и причину через пробел:", reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data == "admin:delete_user")
async def inline_delete_user_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFlow.delete_user)
    await callback.message.answer("Введите @username или Telegram ID и причину через пробел:", reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data == "admin:add_admin")
async def inline_add_admin_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFlow.add_admin)
    await callback.message.answer("Введите @username или Telegram ID пользователя, которому нужно выдать админку:", reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data == "admin:remove_admin")
async def inline_remove_admin_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFlow.remove_admin)
    await callback.message.answer("Введите @username или Telegram ID админа, у которого нужно снять права:", reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data.in_({"admin:enable_day", "admin:disable_day"}))
async def inline_admin_day_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(is_enabled=callback.data == "admin:enable_day")
    await state.set_state(AdminFlow.day_toggle)
    await callback.message.answer("Введите номер дня недели: 0=Пн, 1=Вт, ..., 6=Вс", reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data == "admin:weekly_booking_limit")
async def inline_weekly_booking_limit_start(callback: CallbackQuery, state: FSMContext) -> None:
    current = await api.get_weekly_booking_limit(callback.from_user.id)
    await state.set_state(AdminFlow.weekly_booking_limit)
    await callback.message.answer(
        f"Текущий лимит: {format_hours(current['weekly_booking_limit_minutes'])} в неделю на коллектив.\n"
        "Введите новый лимит в часах. Например: 4 или 3.5",
        reply_markup=flow_cancel_keyboard(),
    )
    await answer_callback(callback)


@router.callback_query(F.data == "admin:daily_booking_limit")
async def inline_daily_booking_limit_start(callback: CallbackQuery, state: FSMContext) -> None:
    current = await api.get_daily_booking_limit(callback.from_user.id)
    await state.set_state(AdminFlow.daily_booking_limit)
    current_text = "выключен" if current["daily_booking_limit_minutes"] == 0 else format_hours(current["daily_booking_limit_minutes"])
    await callback.message.answer(
        f"Текущий дневной лимит: {current_text} на коллектив.\n"
        "Введите новый лимит в часах. Например: 2 или 1.5.\n"
        "Введите 0, чтобы выключить дневной лимит.",
        reply_markup=flow_cancel_keyboard(),
    )
    await answer_callback(callback)


@router.callback_query(F.data == "admin:personal_bookings_toggle")
async def inline_personal_bookings_toggle(callback: CallbackQuery) -> None:
    current = await api.get_personal_bookings_enabled(callback.from_user.id)
    updated = await api.update_personal_bookings_enabled(callback.from_user.id, not current.get("personal_bookings_enabled"))
    status = "включены" if updated.get("personal_bookings_enabled") else "выключены"
    await edit_screen(callback, f"Настройки бронирования\n\nЛичные репетиции: {status}", admin_settings_keyboard())
    await answer_callback(callback, "Готово")


@router.callback_query(F.data == "admin:booking_window")
async def inline_booking_window_start(callback: CallbackQuery, state: FSMContext) -> None:
    current = await api.get_booking_window(callback.from_user.id)
    await state.set_state(AdminFlow.booking_window)
    await callback.message.answer(
        f"Текущие часы кабинета: {current['start_time']}-{current['end_time']}.\n"
        "Введите новое окно в формате 08:00-21:00",
        reply_markup=flow_cancel_keyboard(),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin_bookings_delete:"))
async def inline_admin_bookings_delete_list(callback: CallbackQuery, state: FSMContext) -> None:
    page = int(callback.data.rsplit(":", maxsplit=1)[-1])
    rows = await api.schedule()
    await state.update_data(admin_delete_bookings=rows)
    if not rows:
        await edit_screen(callback, "На ближайшие 7 дней броней нет.", admin_schedule_keyboard())
        await answer_callback(callback)
        return
    await edit_screen(
        callback,
        "Выберите бронь, которую нужно удалить:",
        admin_booking_delete_list_keyboard(rows, page),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin_bookings_manage:"))
async def inline_admin_bookings_manage_list(callback: CallbackQuery, state: FSMContext) -> None:
    page = int(callback.data.rsplit(":", maxsplit=1)[-1])
    rows = await api.schedule()
    await state.update_data(admin_manage_bookings=rows)
    if not rows:
        await edit_screen(callback, "На ближайшие 7 дней броней нет.", admin_schedule_keyboard())
        await answer_callback(callback)
        return
    await edit_screen(
        callback,
        "Выберите бронь для редактирования:",
        admin_booking_delete_list_keyboard(rows, page, action_prefix="admin_booking_manage", page_prefix="admin_bookings_manage"),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin_booking_manage:"))
async def inline_admin_booking_manage_card(callback: CallbackQuery, state: FSMContext) -> None:
    booking_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    data = await state.get_data()
    rows = data.get("admin_manage_bookings") or await api.schedule()
    await state.update_data(admin_manage_bookings=rows)
    booking = next((item for item in rows if item["id"] == booking_id), None)
    if booking is None:
        await answer_callback(callback, "Бронь не найдена. Обновите список.", show_alert=True)
        return
    await edit_screen(callback, format_booking_card(booking), admin_booking_detail_keyboard(booking_id))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin_booking_delete:"))
async def inline_admin_booking_delete_card(callback: CallbackQuery, state: FSMContext) -> None:
    booking_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    data = await state.get_data()
    rows = data.get("admin_delete_bookings") or await api.schedule()
    booking = next((item for item in rows if item["id"] == booking_id), None)
    if booking is None:
        await answer_callback(callback, "Бронь не найдена. Обновите список.", show_alert=True)
        return
    await edit_screen(callback, format_booking_card(booking), admin_booking_delete_confirm_keyboard(booking_id))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin_booking_edit:"))
async def inline_admin_booking_edit(callback: CallbackQuery, state: FSMContext) -> None:
    _, booking_id_raw, field = callback.data.split(":")
    booking_id = int(booking_id_raw)
    if field == "purpose":
        await edit_screen(callback, "Выберите новую цель брони:", admin_booking_purpose_keyboard(booking_id))
        await answer_callback(callback)
        return
    labels = {
        "date": "Введите новую дату в формате YYYY-MM-DD:",
        "time": "Введите новое время в формате 14:00-15:30:",
        "song": "Введите новую песню/название. Для пустого значения отправьте '-'.",
    }
    if field not in labels:
        await answer_callback(callback, "Поле не поддерживается.", show_alert=True)
        return
    await state.update_data(admin_booking_edit_id=booking_id, admin_booking_edit_field=field)
    await state.set_state(AdminFlow.booking_edit_value)
    await callback.message.answer(labels[field], reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin_booking_purpose:"))
async def inline_admin_booking_purpose(callback: CallbackQuery, state: FSMContext) -> None:
    _, booking_id_raw, purpose = callback.data.split(":")
    try:
        updated = await api.update_booking(callback.from_user.id, int(booking_id_raw), {"purpose": purpose})
    except RuntimeError as exc:
        await edit_screen(callback, f"Не получилось обновить цель: {exc}", admin_schedule_keyboard())
        await answer_callback(callback)
        return
    rows = await api.schedule()
    await state.update_data(admin_manage_bookings=rows)
    booking = next((item for item in rows if item["id"] == updated["id"]), None)
    await refresh_all_schedule_menus(callback.message.bot)
    await edit_screen(callback, format_booking_card(booking or updated), admin_booking_detail_keyboard(updated["id"]))
    await answer_callback(callback, "Готово")


@router.callback_query(F.data.startswith("admin_booking_delete_confirm:"))
async def inline_admin_booking_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    booking_id = int(callback.data.rsplit(":", maxsplit=1)[-1])
    try:
        deleted = await api.delete_booking(callback.from_user.id, booking_id)
    except RuntimeError as exc:
        await edit_screen(callback, f"Не получилось удалить бронь: {exc}", admin_schedule_keyboard())
        await answer_callback(callback)
        return
    await state.update_data(admin_delete_bookings=await api.schedule())
    await refresh_all_schedule_menus(callback.message.bot)
    await edit_screen(
        callback,
        f"Бронь удалена: {deleted['booking_date']} {deleted['start_time'][:5]}-{deleted['end_time'][:5]} | {deleted['band_name']}",
        admin_schedule_keyboard(),
    )
    await answer_callback(callback)


@router.callback_query(F.data == "admin:staff_booking")
async def inline_staff_booking_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFlow.staff_booking_date)
    await callback.message.answer(
        "Введите дату, когда нужно занять кабинет, в формате YYYY-MM-DD.\n"
        "Например: 2026-06-25",
        reply_markup=flow_cancel_keyboard(),
    )
    await answer_callback(callback)


@router.callback_query(F.data == "admin:create_event")
async def inline_event_create_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFlow.event_title)
    await callback.message.answer("Введите название концерта:", reply_markup=flow_cancel_keyboard())
    await answer_callback(callback)


@router.callback_query(F.data.startswith("registration:approve:"))
async def inline_registration_approve(callback: CallbackQuery) -> None:
    admin = await api.get_user(callback.from_user.id)
    if admin["role"] != "admin":
        await answer_callback(callback, "Нет доступа", show_alert=True)
        return
    target = callback.data.rsplit(":", maxsplit=1)[-1]
    user = await api.moderate_user("approve", callback.from_user.id, target)
    text = (
        "Регистрация одобрена:\n"
        f"{user['last_name'] or '-'} {user['first_name'] or '-'} | {user['study_group'] or '-'}\n"
        f"Telegram: @{user['telegram_username'] or '-'}\n"
        f"Telegram ID: {user['telegram_id']}"
    )
    await edit_screen(callback, text)
    try:
        await callback.message.bot.send_message(
            user["telegram_id"],
            "Ваша регистрация одобрена. Теперь доступны все функции AITU Music Club Bot.",
        )
    except Exception:
        pass
    await answer_callback(callback, "Одобрено")


@router.callback_query(F.data.startswith("registration:reject:"))
async def inline_registration_reject(callback: CallbackQuery) -> None:
    admin = await api.get_user(callback.from_user.id)
    if admin["role"] != "admin":
        await answer_callback(callback, "Нет доступа", show_alert=True)
        return
    target = callback.data.rsplit(":", maxsplit=1)[-1]
    user = await api.moderate_user("reject", callback.from_user.id, target, "Отклонено админом")
    retry_after = user.get("registration_retry_after")
    text = (
        "Регистрация отклонена:\n"
        f"{user['last_name'] or '-'} {user['first_name'] or '-'} | {user['study_group'] or '-'}\n"
        f"Telegram: @{user['telegram_username'] or '-'}\n"
        f"Telegram ID: {user['telegram_id']}\n"
        f"Повторная подача после: {retry_after or 'позже'}"
    )
    await edit_screen(callback, text)
    try:
        await callback.message.bot.send_message(
            user["telegram_id"],
            "Ваша регистрация отклонена, но это не бан.\n"
            f"Повторно подать заявку можно после: {retry_after or 'позже'}.\n"
            "Когда срок пройдет, отправьте /start."
        )
    except Exception:
        pass
    await answer_callback(callback, "Отклонено")


@router.message(Command("leha"))
async def leha(message: Message) -> None:
    await message.answer(random.choice(LEHA_PRAISES))


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    user = await api.upsert_user(message.from_user)
    is_approved = user["role"] == "admin" or user["status"] == "approved"
    await message.answer(user_help_text(is_approved))


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    invite_code = extract_join_code_from_start(message)
    user = await api.upsert_user(message.from_user)
    if not user["profile_completed"]:
        if invite_code:
            await state.update_data(pending_invite_code=invite_code)
        await prompt_next_profile_step(message, state, user, show_welcome=True)
        return
    if user["status"] == "pending":
        await notify_admins_about_registration(message, user)
        await send_pending_notice(message)
        if invite_code:
            await message.answer("После одобрения нажмите ссылку вступления ещё раз.")
        return
    if user["status"] == "rejected":
        try:
            user = await api.resubmit_registration(message.from_user.id)
        except RuntimeError:
            retry_after = user.get("registration_retry_after")
            reason = user.get("moderation_reason") or "без причины"
            if retry_after:
                await message.answer(
                    "Ваша заявка была отклонена, но это не бан.\n"
                    f"Причина: {reason}\n"
                    f"Повторно подать заявку можно после: {retry_after}"
                )
            else:
                await message.answer(
                    "Ваша заявка была отклонена, но это не бан. Попробуйте подать заявку позже или обратитесь к админам."
                )
            return
        await notify_admins_about_registration(message, user, force=True)
        await message.answer("Заявка отправлена повторно.")
        await send_pending_notice(message)
        return
    if user["status"] in {"banned", "deleted"}:
        await message.answer("Ваш аккаунт не имеет доступа к боту. Обратитесь к админам AITU Music Club.")
        return
    if invite_code:
        await start_join_by_code(message, state, invite_code)
        return
    await show_menu(message, user)


@router.message(Onboarding.first_name)
async def onboarding_first_name(message: Message, state: FSMContext) -> None:
    first_name = message.text.strip()
    if not first_name:
        await message.answer("Введите имя:")
        return
    await api.update_settings(message.from_user.id, {"first_name": first_name})
    await state.set_state(Onboarding.last_name)
    await message.answer("Введите фамилию:")


@router.message(Onboarding.last_name)
async def onboarding_last_name(message: Message, state: FSMContext) -> None:
    last_name = message.text.strip()
    if not last_name:
        await message.answer("Введите фамилию:")
        return
    await api.update_settings(message.from_user.id, {"last_name": last_name})
    await state.set_state(Onboarding.study_group)
    await message.answer("Введите учебную группу:")


@router.message(Onboarding.study_group)
async def onboarding_group(message: Message, state: FSMContext) -> None:
    study_group = message.text.strip()
    if not study_group:
        await message.answer("Введите учебную группу:")
        return
    await api.update_settings(message.from_user.id, {"study_group": study_group})
    await state.set_state(Onboarding.language)
    await message.answer("Выберите язык:", reply_markup=language_keyboard())


@router.message(Onboarding.language)
async def onboarding_language(message: Message, state: FSMContext) -> None:
    language = message.text.strip().lower()
    if language not in {"ru", "kz", "en"}:
        await message.answer("Выберите ru, kz или en.", reply_markup=language_keyboard())
        return
    data = await state.get_data()
    user = await api.update_settings(message.from_user.id, {"language": language})
    await state.clear()
    if user["status"] == "pending":
        await notify_admins_about_registration(message, user, force=True)
        await message.answer(t(language, "profile_saved"))
        await send_pending_notice(message)
        if data.get("pending_invite_code"):
            await message.answer("После одобрения нажмите ссылку вступления ещё раз.")
        return
    if data.get("pending_invite_code"):
        await message.answer(t(language, "profile_saved"))
        await start_join_by_code(message, state, data["pending_invite_code"])
        return
    await send_schedule_menu(message, user, t(language, "profile_saved"))


@router.message(F.text == "👤 Личный кабинет")
async def profile(message: Message) -> None:
    if not await has_full_access(message):
        return
    user = await api.get_user(message.from_user.id)
    text = (
        f"Имя: {user['first_name']}\n"
        f"Фамилия: {user['last_name']}\n"
        f"Группа: {user['study_group']}\n"
        f"Язык: {user['language']}\n"
        f"Telegram: @{user['telegram_username'] or '-'}\n"
        f"Уведомления: {'включены' if user['notifications_enabled'] else 'выключены'}"
    )
    await message.answer(text, reply_markup=profile_inline_keyboard())


@router.message(F.text == "История мероприятий")
async def event_history(message: Message) -> None:
    if not await has_full_access(message):
        return
    rows = await api.event_history(message.from_user.id)
    await message.answer(format_event_history(rows), reply_markup=profile_event_history_keyboard(rows))


@router.message(F.text == "Изменить имя")
async def edit_first_name(message: Message, state: FSMContext) -> None:
    await state.set_state(ProfileFlow.first_name)
    await message.answer("Введите новое имя:", reply_markup=flow_cancel_keyboard())


@router.message(ProfileFlow.personal_event_title)
async def personal_event_title(message: Message, state: FSMContext) -> None:
    title = message.text.strip()
    if not title:
        await message.answer("Название не должно быть пустым.", reply_markup=flow_cancel_keyboard())
        return
    if len(title) > 150:
        await message.answer("Название должно быть не длиннее 150 символов.", reply_markup=flow_cancel_keyboard())
        return
    await state.update_data(personal_event_title=title)
    await state.set_state(ProfileFlow.personal_event_date)
    await message.answer("Введите дату события в формате YYYY-MM-DD:", reply_markup=flow_cancel_keyboard())


@router.message(ProfileFlow.personal_event_date)
async def personal_event_date(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    try:
        date.fromisoformat(raw)
    except ValueError:
        await message.answer("Введите дату в формате YYYY-MM-DD.", reply_markup=flow_cancel_keyboard())
        return
    await state.update_data(personal_event_date=raw)
    await state.set_state(ProfileFlow.personal_event_location)
    await message.answer("Введите место проведения. Если места нет, отправьте '-'.", reply_markup=flow_cancel_keyboard())


@router.message(ProfileFlow.personal_event_location)
async def personal_event_location(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    location = None if raw == "-" else raw
    if location and len(location) > 150:
        await message.answer("Место должно быть не длиннее 150 символов.", reply_markup=flow_cancel_keyboard())
        return
    await state.update_data(personal_event_location=location)
    await state.set_state(ProfileFlow.personal_event_role)
    await message.answer("В качестве кого вы участвовали? Например: вокалист, гитарист, организатор.", reply_markup=flow_cancel_keyboard())


@router.message(ProfileFlow.personal_event_role)
async def personal_event_role(message: Message, state: FSMContext) -> None:
    role = message.text.strip()
    if not role:
        await message.answer("Роль не должна быть пустой.", reply_markup=flow_cancel_keyboard())
        return
    if len(role) > 100:
        await message.answer("Роль должна быть не длиннее 100 символов.", reply_markup=flow_cancel_keyboard())
        return
    data = await state.get_data()
    try:
        await api.create_personal_event_history(
            message.from_user.id,
            {
                "title": data["personal_event_title"],
                "event_date": data["personal_event_date"],
                "location": data.get("personal_event_location"),
                "role_at_event": role,
            },
        )
    except RuntimeError as exc:
        await state.clear()
        await message.answer(f"Не получилось добавить событие: {exc}", reply_markup=profile_event_history_keyboard())
        return
    await state.clear()
    rows = await api.event_history(message.from_user.id)
    await message.answer("Событие добавлено.\n\n" + format_event_history(rows), reply_markup=profile_event_history_keyboard(rows))


@router.message(ProfileFlow.personal_event_edit_value)
async def personal_event_edit_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    item_id = int(data["personal_event_edit_id"])
    field = data["personal_event_edit_field"]
    raw = message.text.strip()
    value = raw
    if field == "event_date":
        try:
            date.fromisoformat(raw)
        except ValueError:
            await message.answer("Введите дату в формате YYYY-MM-DD.", reply_markup=flow_cancel_keyboard())
            return
    elif field == "location":
        value = None if raw == "-" else raw
        if value and len(value) > 150:
            await message.answer("Место должно быть не длиннее 150 символов.", reply_markup=flow_cancel_keyboard())
            return
    elif field == "title":
        if not raw:
            await message.answer("Название не должно быть пустым.", reply_markup=flow_cancel_keyboard())
            return
        if len(raw) > 150:
            await message.answer("Название должно быть не длиннее 150 символов.", reply_markup=flow_cancel_keyboard())
            return
    elif field == "role_at_event":
        if not raw:
            await message.answer("Роль не должна быть пустой.", reply_markup=flow_cancel_keyboard())
            return
        if len(raw) > 100:
            await message.answer("Роль должна быть не длиннее 100 символов.", reply_markup=flow_cancel_keyboard())
            return
    try:
        item = await api.update_personal_event_history(message.from_user.id, item_id, {field: value})
    except RuntimeError as exc:
        await state.clear()
        await message.answer(f"Не получилось обновить событие: {exc}", reply_markup=profile_event_history_keyboard())
        return
    await state.clear()
    await message.answer(
        "Событие обновлено.\n\n" + format_personal_event_card(item),
        reply_markup=profile_personal_event_detail_keyboard(item_id),
    )


@router.message(ProfileFlow.first_name)
async def save_first_name(message: Message, state: FSMContext) -> None:
    user = await api.update_settings(message.from_user.id, {"first_name": message.text.strip()})
    await state.clear()
    await send_schedule_menu(message, user, "Готово.")


@router.message(F.text == "Изменить фамилию")
async def edit_last_name(message: Message, state: FSMContext) -> None:
    await state.set_state(ProfileFlow.last_name)
    await message.answer("Введите новую фамилию:", reply_markup=flow_cancel_keyboard())


@router.message(ProfileFlow.last_name)
async def save_last_name(message: Message, state: FSMContext) -> None:
    user = await api.update_settings(message.from_user.id, {"last_name": message.text.strip()})
    await state.clear()
    await send_schedule_menu(message, user, "Готово.")


@router.message(F.text == "Изменить группу")
async def edit_group(message: Message, state: FSMContext) -> None:
    await state.set_state(ProfileFlow.study_group)
    await message.answer("Введите новую учебную группу:", reply_markup=flow_cancel_keyboard())


@router.message(ProfileFlow.study_group)
async def save_group(message: Message, state: FSMContext) -> None:
    user = await api.update_settings(message.from_user.id, {"study_group": message.text.strip()})
    await state.clear()
    await send_schedule_menu(message, user, "Готово.")


@router.message(F.text == "Изменить язык")
async def edit_language(message: Message, state: FSMContext) -> None:
    await state.set_state(ProfileFlow.language)
    await message.answer("Выберите язык:", reply_markup=language_keyboard())


@router.message(ProfileFlow.language)
async def save_language(message: Message, state: FSMContext) -> None:
    language = message.text.strip().lower()
    if language not in {"ru", "kz", "en"}:
        await message.answer("Выберите ru, kz или en.", reply_markup=language_keyboard())
        return
    user = await api.update_settings(message.from_user.id, {"language": language})
    await state.clear()
    await send_schedule_menu(message, user, "Готово.")


@router.message(F.text == "Уведомления вкл/выкл")
async def toggle_notifications(message: Message) -> None:
    user = await api.get_user(message.from_user.id)
    updated = await api.update_settings(message.from_user.id, {"notifications_enabled": not user["notifications_enabled"]})
    await send_schedule_menu(message, updated, "Настройки обновлены.")


@router.message(F.text == "➕ Создать коллектив")
async def create_band_start(message: Message, state: FSMContext) -> None:
    if not await has_full_access(message):
        return
    await state.set_state(BandFlow.create_name)
    await message.answer("Введите название коллектива:", reply_markup=flow_cancel_keyboard())


@router.message(BandFlow.create_name)
async def create_band_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text.strip())
    await state.set_state(BandFlow.create_role)
    await message.answer(
        "Ваша роль в коллективе? Например: гитарист, вокалист, барабанщик.",
        reply_markup=flow_cancel_keyboard(),
    )


@router.message(BandFlow.create_role)
async def create_band_role(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    band = await api.create_band(message.from_user.id, data["name"], message.text.strip())
    user = await api.get_user(message.from_user.id)
    invite_link = await build_band_invite_link(message.bot, band["invite_code"])
    await state.clear()
    await send_schedule_menu(
        message,
        user,
        f"Коллектив создан: {band['name']}\nКод приглашения: {band['invite_code']}\nСсылка: {invite_link}",
    )


@router.message(F.text == "🔑 Вступить по коду")
async def join_band_start(message: Message, state: FSMContext) -> None:
    if not await has_full_access(message):
        return
    await state.set_state(BandFlow.join_code)
    await message.answer("Введите код приглашения:", reply_markup=flow_cancel_keyboard())


@router.message(BandFlow.join_code)
async def join_band_code(message: Message, state: FSMContext) -> None:
    await state.update_data(invite_code=message.text.strip())
    await state.set_state(BandFlow.join_role)
    await message.answer("Ваша роль в коллективе?", reply_markup=flow_cancel_keyboard())


@router.message(BandFlow.join_role)
async def join_band_role(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    band = await api.join_band(message.from_user.id, data["invite_code"], message.text.strip())
    user = await api.get_user(message.from_user.id)
    await state.clear()
    await send_schedule_menu(message, user, f"Вы вступили в коллектив: {band['name']}")


@router.message(BandFlow.edit_value)
async def user_band_edit_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    band_id = int(data["user_band_edit_id"])
    field = data["user_band_edit_field"]
    raw = message.text.strip()
    if not raw:
        await message.answer("Значение не должно быть пустым.", reply_markup=flow_cancel_keyboard())
        return
    try:
        if field == "name":
            band = await api.update_band(message.from_user.id, band_id, raw)
        elif field == "member_role":
            member_id = int(data["user_band_member_id"])
            await api.update_band_member_role(message.from_user.id, band_id, member_id, raw)
            band = await api.get_band(band_id)
        else:
            await state.clear()
            await message.answer("Это поле нельзя редактировать.", reply_markup=bands_menu_keyboard())
            return
    except RuntimeError as exc:
        await state.clear()
        await message.answer(f"Не получилось обновить коллектив: {exc}", reply_markup=bands_menu_keyboard())
        return
    await state.clear()
    await message.answer(
        "Коллектив обновлен.\n\n" + format_band_card(band),
        reply_markup=user_band_detail_keyboard(band_id, user_is_band_owner(band, message.from_user.id)),
    )


@router.message(F.text == "🎵 Мои коллективы")
async def my_bands(message: Message) -> None:
    if not await has_full_access(message):
        return
    bands = await api.list_bands(message.from_user.id)
    if not bands:
        await message.answer("У вас пока нет коллективов.", reply_markup=bands_menu_keyboard())
        return
    bands = sorted(bands, key=lambda item: item.get("created_at", ""), reverse=True)
    await message.answer("Мои коллективы", reply_markup=user_bands_list_keyboard(bands))


@router.message(F.text == "📋 Расписание")
async def schedule_button(message: Message) -> None:
    user = await api.get_user(message.from_user.id)
    await send_schedule_menu(message, user, "Расписание на ближайшие 7 дней")


@router.message(F.text == "🎧 Мои репетиции")
async def my_rehearsals_button(message: Message) -> None:
    if not await has_full_access(message):
        return
    rows = await api.my_bookings(message.from_user.id)
    if not rows:
        await message.answer("У вас пока нет будущих репетиций.", reply_markup=rehearsals_menu_keyboard(await get_webapp_base_url()))
        return
    await message.answer("Мои репетиции", reply_markup=user_bookings_list_keyboard(rows))


@router.message(F.text == "📅 Забронировать")
async def booking_start(message: Message, state: FSMContext) -> None:
    if not await has_full_access(message):
        return
    await start_booking_flow(message, state, message.from_user.id)


@router.message(BookingFlow.edit_value)
async def user_booking_edit_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    booking_id = int(data["user_booking_edit_id"])
    field = data["user_booking_edit_field"]
    raw = message.text.strip()
    payload: dict = {}
    if field == "date":
        try:
            date.fromisoformat(raw)
        except ValueError:
            await message.answer("Введите дату в формате YYYY-MM-DD.", reply_markup=flow_cancel_keyboard())
            return
        payload["booking_date"] = raw
    elif field == "time":
        parsed = parse_booking_window(raw)
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
        payload["start_time"] = start.time().isoformat()
        payload["end_time"] = end.time().isoformat()
    elif field == "song":
        payload["song_title"] = None if raw == "-" else raw
    else:
        await state.clear()
        await message.answer("Это поле нельзя редактировать.", reply_markup=rehearsals_menu_keyboard(await get_webapp_base_url()))
        return
    try:
        updated = await api.update_booking(message.from_user.id, booking_id, payload)
    except RuntimeError as exc:
        await state.clear()
        await message.answer(f"Не получилось обновить бронь: {exc}", reply_markup=rehearsals_menu_keyboard(await get_webapp_base_url()))
        return
    rows = await api.my_bookings(message.from_user.id)
    booking = next((item for item in rows if item["id"] == updated["id"]), None)
    await state.clear()
    await refresh_all_schedule_menus(message.bot)
    await message.answer(
        "Бронь обновлена.\n\n" + format_booking_card(booking or updated),
        reply_markup=user_booking_detail_keyboard(updated["id"]),
    )


@router.message(BookingFlow.band_id)
async def booking_band(message: Message, state: FSMContext) -> None:
    if not message.text.strip().isdigit():
        await message.answer("Введите номер коллектива из списка.", reply_markup=flow_cancel_keyboard())
        return
    data = await state.get_data()
    bands = data.get("booking_bands", [])
    index = int(message.text.strip()) - 1
    if index < 0 or index >= len(bands):
        await message.answer("Выберите номер коллектива из списка.", reply_markup=flow_cancel_keyboard())
        return
    band = bands[index]
    band_id = band["id"]
    await state.update_data(band_id=band_id, band_name=band["name"])
    dates = []
    today = datetime.now().date()
    for offset in range(8):
        target_date = today + timedelta(days=offset)
        day_slots = await api.available_slots(target_date, 30, band_id)
        if day_slots:
            dates.append({"date": target_date.isoformat(), "count": len(day_slots)})
    if not dates:
        await message.answer("На ближайшие 7 дней нет свободного времени.")
        await state.clear()
        return
    await state.update_data(booking_dates=dates)
    await state.set_state(BookingFlow.booking_date)
    text = "Выберите день:\n" + "\n".join(
        f"{idx + 1}. {format_booking_date(item['date'])} | свободно: {item['count']}" for idx, item in enumerate(dates)
    )
    await message.answer(text, reply_markup=flow_cancel_keyboard())


@router.message(BookingFlow.booking_date)
async def booking_date_choose(message: Message, state: FSMContext) -> None:
    if not message.text.strip().isdigit():
        await message.answer("Введите номер дня из списка.", reply_markup=flow_cancel_keyboard())
        return
    data = await state.get_data()
    dates = data.get("booking_dates", [])
    index = int(message.text.strip()) - 1
    if index < 0 or index >= len(dates):
        await message.answer("Выберите номер дня из списка.", reply_markup=flow_cancel_keyboard())
        return
    selected_date = dates[index]["date"]
    band_id = data["band_id"]
    slots = [
        {"date": selected_date, **slot}
        for slot in await api.available_slots(date.fromisoformat(selected_date), 30, band_id)
    ]
    if not slots:
        await message.answer("На этот день уже нет свободных слотов. Попробуйте выбрать другой день.", reply_markup=flow_cancel_keyboard())
        return
    await state.update_data(selected_date=selected_date, slots=slots)
    await state.set_state(BookingFlow.slot)
    text = f"{format_booking_date(selected_date)}. Выберите время:\n" + "\n".join(
        f"{idx + 1}. {slot['start_time'][:5]}" for idx, slot in enumerate(slots)
    )
    await message.answer(text, reply_markup=flow_cancel_keyboard())


@router.message(BookingFlow.slot)
async def booking_slot(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        idx = int(message.text.strip()) - 1
        slot = data["slots"][idx]
    except (ValueError, IndexError):
        await message.answer("Введите номер слота из списка.", reply_markup=flow_cancel_keyboard())
        return
    band_id = data.get("band_id")
    end_options = await get_end_time_options(slot["date"], slot["start_time"], band_id)
    if not end_options:
        await message.answer("Для этого старта нет доступного окончания.", reply_markup=flow_cancel_keyboard())
        return
    limit_status = None if band_id is None else await api.booking_limit_status(message.from_user.id, band_id, date.fromisoformat(slot["date"]))
    await state.update_data(selected_start_slot=slot, end_options=end_options)
    await state.set_state(BookingFlow.end_time)
    text = build_end_time_text(slot["date"], slot["start_time"], limit_status, end_options) + "\n" + "\n".join(
        f"{idx + 1}. {option['label']}" for idx, option in enumerate(end_options)
    )
    await message.answer(text, reply_markup=flow_cancel_keyboard())


@router.message(BookingFlow.end_time)
async def booking_end_time(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        idx = int(message.text.strip()) - 1
        slot = data["end_options"][idx]
    except (ValueError, IndexError):
        await message.answer("Введите номер окончания из списка.", reply_markup=flow_cancel_keyboard())
        return
    await state.update_data(selected_slot=slot)
    if data.get("is_personal_booking"):
        await state.update_data(purpose="personal")
        await state.set_state(BookingFlow.song_title)
        await message.answer("Что будете репетировать? Если пока не ясно, отправьте '-'.", reply_markup=booking_song_keyboard())
        return
    await state.set_state(BookingFlow.purpose)
    await message.answer(
        f"Выбрано: {format_booking_date(slot['date'])} {slot['start_time'][:5]}-{slot['end_time'][:5]}\n"
        "Цель репетиции? Напишите 'concert' если к концерту или 'self' если для себя.",
        reply_markup=flow_cancel_keyboard(),
    )


@router.message(BookingFlow.purpose)
async def booking_purpose(message: Message, state: FSMContext) -> None:
    purpose = message.text.strip().lower()
    if purpose not in {"concert", "self"}:
        await message.answer("Введите concert или self.", reply_markup=flow_cancel_keyboard())
        return
    await state.update_data(purpose=purpose)
    await state.set_state(BookingFlow.song_title)
    await message.answer("Какую песню репетируете? Если пока не ясно, отправьте '-'.", reply_markup=booking_song_keyboard())


@router.message(BookingFlow.song_title)
async def booking_song(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    slot = data["selected_slot"]
    raw_song = message.text.strip()
    song_title = None if raw_song == "-" else raw_song
    user = await api.get_user(message.from_user.id)
    try:
        booking = await api.create_booking(
            message.from_user.id,
            data["band_id"],
            date.fromisoformat(slot["date"]),
            datetime.strptime(slot["start_time"], "%H:%M:%S").time(),
            datetime.strptime(slot["end_time"], "%H:%M:%S").time(),
            data["purpose"],
            song_title,
        )
    except RuntimeError as exc:
        await state.clear()
        await send_schedule_menu(message, user, f"Не получилось создать бронь: {exc}")
        return
    await state.clear()
    await refresh_all_schedule_menus(message.bot)
    await send_schedule_menu(
        message,
        user,
        f"Бронь создана: {booking['booking_date']} {booking['start_time'][:5]}-{booking['end_time'][:5]}",
    )


@router.message(F.text == "🎤 Концерты")
async def events(message: Message) -> None:
    if not await has_full_access(message):
        return
    items = await api.list_events()
    open_events = [item for item in items if item.get("status") == "open"]
    preview = "\n".join(f"- {item['event_date']} | {item['title']}" for item in open_events[:3])
    text = "Концерты"
    if preview:
        text += "\n\nОткрытые концерты:\n" + preview
    else:
        text += "\n\nСейчас нет открытых концертов."
    await message.answer(text, reply_markup=concerts_menu_keyboard())


@router.message(F.text == "Подать список")
async def submit_list_start(message: Message, state: FSMContext) -> None:
    events = await api.list_events()
    if not events:
        await message.answer("Пока нет концертов.")
        return
    await message.answer(
        "Выберите концерт, на который хотите подать номер:",
        reply_markup=user_events_keyboard(events),
    )


@router.message(EventFlow.event_id)
async def submit_list_event(message: Message, state: FSMContext) -> None:
    if not message.text.strip().isdigit():
        await message.answer("Введите ID числом.")
        return
    bands = await api.list_bands(message.from_user.id)
    if not bands:
        await message.answer("У вас пока нет коллективов.")
        await state.clear()
        return
    await state.update_data(event_id=int(message.text.strip()), bands=bands)
    await state.set_state(EventFlow.band_id)
    await message.answer(
        "Введите ID коллектива:\n" + "\n".join(f"{band['id']}. {band['name']}" for band in bands),
        reply_markup=flow_cancel_keyboard(),
    )


@router.message(EventFlow.band_id)
async def submit_list_band(message: Message, state: FSMContext) -> None:
    if not message.text.strip().isdigit():
        await message.answer("Введите ID числом.")
        return
    band_id = int(message.text.strip())
    data = await state.get_data()
    band = next((item for item in data["bands"] if item["id"] == band_id), None)
    if band is None:
        await message.answer("Выберите коллектив из списка.")
        return
    await state.update_data(band_id=band_id, members=band["members"])
    await state.set_state(EventFlow.members)
    lines = ["Введите ID участников через запятую:"]
    for member in band["members"]:
        user = member["user"]
        lines.append(
            f"{member['id']}. {user['last_name'] or ''} {user['first_name'] or ''} | {user['study_group'] or '-'} | {member['instrument_role']}"
        )
    await message.answer("\n".join(lines), reply_markup=flow_cancel_keyboard())


@router.message(EventFlow.members)
async def submit_list_members(message: Message, state: FSMContext) -> None:
    try:
        member_ids = [int(item.strip()) for item in message.text.split(",") if item.strip()]
    except ValueError:
        await message.answer("Введите ID через запятую. Пример: 1,2,3")
        return
    data = await state.get_data()
    await api.submit_application(message.from_user.id, data["event_id"], data["band_id"], data.get("song_title", "песня не указана"), member_ids)
    user = await api.get_user(message.from_user.id)
    await state.clear()
    await send_schedule_menu(message, user, "Список отправлен.")


@router.message(F.text == "⚙️ Админ")
async def admin_menu(message: Message) -> None:
    user = await api.get_user(message.from_user.id)
    if user["role"] != "admin":
        await message.answer("Нет доступа.")
        return
    await message.answer("Админ-меню", reply_markup=admin_inline_keyboard())


@router.message(F.text == "Заявки пользователей")
async def pending_users(message: Message) -> None:
    users = await api.list_users(message.from_user.id, "pending")
    if not users:
        await message.answer("Новых заявок нет.")
        return
    lines = ["Заявки на регистрацию:"]
    for user in users:
        lines.append(
            f"{user['telegram_id']} | {user['last_name'] or '-'} {user['first_name'] or '-'} | "
            f"{user['study_group'] or '-'} | @{user['telegram_username'] or '-'}"
        )
    await message.answer("\n".join(lines))


@router.message(F.text.in_({"История броней", "История действий"}))
async def action_history(message: Message) -> None:
    rows = await api.action_history(message.from_user.id)
    if not rows:
        await message.answer("История действий пока пустая.")
        return
    lines = ["Последние действия:"]
    for item in rows[:30]:
        created_at = item["created_at"].replace("T", " ")[:16]
        lines.append(f"{created_at} | {item['actor_name']} | {item['description'] or item['action']}")
    await message.answer("\n".join(lines))


@router.message(F.text == "Одобрить пользователя")
async def approve_user_start(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminFlow.approve_user)
    await message.answer("Введите @username или Telegram ID пользователя для одобрения:", reply_markup=flow_cancel_keyboard())


@router.message(F.text == "Добавить админа")
async def add_admin_start(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminFlow.add_admin)
    await message.answer("Введите @username или Telegram ID пользователя, которому нужно выдать админку:", reply_markup=flow_cancel_keyboard())


@router.message(AdminFlow.approve_user)
async def approve_user_save(message: Message, state: FSMContext) -> None:
    target = message.text.strip()
    if not target:
        await message.answer("Введите @username или Telegram ID.")
        return
    user = await api.moderate_user("approve", message.from_user.id, target)
    await state.clear()
    await message.answer("Пользователь одобрен.", reply_markup=admin_users_keyboard())
    try:
        await message.bot.send_message(user["telegram_id"], "Ваша регистрация одобрена. Теперь доступны все функции AITU Music Club Bot.")
    except Exception:
        pass


@router.message(AdminFlow.add_admin)
async def add_admin_save(message: Message, state: FSMContext) -> None:
    target = message.text.strip()
    if not target:
        await message.answer("Введите @username или Telegram ID.")
        return
    try:
        user = await api.promote_admin(message.from_user.id, target)
    except RuntimeError as exc:
        await message.answer(f"Не получилось добавить админа: {exc}", reply_markup=admin_users_keyboard())
        await state.clear()
        return
    await state.clear()
    name = f"{user.get('last_name') or ''} {user.get('first_name') or ''}".strip()
    username = f"@{user['telegram_username']}" if user.get("telegram_username") else str(user["telegram_id"])
    await message.answer(f"Админ добавлен: {name or username}", reply_markup=admin_admins_keyboard())
    try:
        await message.bot.send_message(user["telegram_id"], "Вам выдали права админа в AITU Music Club Bot.")
    except Exception:
        pass


@router.message(AdminFlow.remove_admin)
async def remove_admin_save(message: Message, state: FSMContext) -> None:
    target = message.text.strip()
    if not target:
        await message.answer("Введите @username или Telegram ID.")
        return
    try:
        user = await api.demote_admin(message.from_user.id, target)
    except RuntimeError as exc:
        await message.answer(f"Не получилось снять админку: {exc}", reply_markup=admin_admins_keyboard())
        await state.clear()
        return
    await state.clear()
    name = f"{user.get('last_name') or ''} {user.get('first_name') or ''}".strip()
    username = f"@{user['telegram_username']}" if user.get("telegram_username") else str(user["telegram_id"])
    admins = await api.list_admins(message.from_user.id)
    await message.answer(f"Админка снята: {name or username}\n\n{format_admins_list(admins)}", reply_markup=admin_admins_keyboard())
    try:
        await message.bot.send_message(user["telegram_id"], "С вас сняли права админа в AITU Music Club Bot.")
    except Exception:
        pass


@router.message(AdminFlow.band_edit_value)
async def admin_band_edit_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    band_id = int(data["admin_band_edit_id"])
    field = data["admin_band_edit_field"]
    raw = message.text.strip()
    if not raw:
        await message.answer("Значение не должно быть пустым.", reply_markup=flow_cancel_keyboard())
        return
    try:
        if field == "name":
            band = await api.update_band(message.from_user.id, band_id, raw)
        elif field == "member_role":
            member_id = int(data["admin_band_member_id"])
            await api.update_band_member_role(message.from_user.id, band_id, member_id, raw)
            band = await api.get_band(band_id)
        else:
            await state.clear()
            await message.answer("Это поле нельзя редактировать.", reply_markup=admin_inline_keyboard())
            return
    except RuntimeError as exc:
        await state.clear()
        await message.answer(f"Не получилось обновить коллектив: {exc}", reply_markup=admin_band_detail_keyboard(band_id))
        return
    await state.clear()
    await message.answer("Коллектив обновлен.\n\n" + format_band_card(band), reply_markup=admin_band_detail_keyboard(band_id))


@router.message(AdminFlow.booking_edit_value)
async def admin_booking_edit_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    booking_id = int(data["admin_booking_edit_id"])
    field = data["admin_booking_edit_field"]
    raw = message.text.strip()
    payload: dict = {}
    if field == "date":
        try:
            date.fromisoformat(raw)
        except ValueError:
            await message.answer("Введите дату в формате YYYY-MM-DD.", reply_markup=flow_cancel_keyboard())
            return
        payload["booking_date"] = raw
    elif field == "time":
        parsed = parse_booking_window(raw)
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
        payload["start_time"] = start.time().isoformat()
        payload["end_time"] = end.time().isoformat()
    elif field == "song":
        payload["song_title"] = None if raw == "-" else raw
    else:
        await state.clear()
        await message.answer("Это поле нельзя редактировать.", reply_markup=admin_schedule_keyboard())
        return
    try:
        updated = await api.update_booking(message.from_user.id, booking_id, payload)
    except RuntimeError as exc:
        await state.clear()
        await message.answer(f"Не получилось обновить бронь: {exc}", reply_markup=admin_schedule_keyboard())
        return
    rows = await api.schedule()
    booking = next((item for item in rows if item["id"] == updated["id"]), None)
    await state.clear()
    await refresh_all_schedule_menus(message.bot)
    await message.answer("Бронь обновлена.\n\n" + format_booking_card(booking or updated), reply_markup=admin_booking_detail_keyboard(updated["id"]))


@router.message(F.text == "Отклонить пользователя")
async def reject_user_start(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminFlow.reject_user)
    await message.answer(
        "Введите @username или Telegram ID и причину через пробел. Например: @username не студент AITU",
        reply_markup=flow_cancel_keyboard(),
    )


@router.message(AdminFlow.reject_user)
async def reject_user_save(message: Message, state: FSMContext) -> None:
    parts = message.text.strip().split(maxsplit=1)
    if not parts:
        await message.answer("Первым должен быть @username или Telegram ID.")
        return
    reason = parts[1] if len(parts) > 1 else None
    await api.moderate_user("reject", message.from_user.id, parts[0], reason)
    await state.clear()
    await message.answer("Пользователь отклонен.", reply_markup=admin_users_keyboard())


@router.message(F.text == "Забанить пользователя")
async def ban_user_start(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminFlow.ban_user)
    await message.answer("Введите @username или Telegram ID и причину через пробел:", reply_markup=flow_cancel_keyboard())


@router.message(AdminFlow.ban_user)
async def ban_user_save(message: Message, state: FSMContext) -> None:
    parts = message.text.strip().split(maxsplit=1)
    if not parts:
        await message.answer("Первым должен быть @username или Telegram ID.")
        return
    await api.moderate_user("ban", message.from_user.id, parts[0], parts[1] if len(parts) > 1 else None)
    await state.clear()
    await message.answer("Пользователь забанен.", reply_markup=admin_users_keyboard())


@router.message(F.text == "Удалить пользователя")
async def delete_user_start(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminFlow.delete_user)
    await message.answer("Введите @username или Telegram ID и причину через пробел:", reply_markup=flow_cancel_keyboard())


@router.message(AdminFlow.delete_user)
async def delete_user_save(message: Message, state: FSMContext) -> None:
    parts = message.text.strip().split(maxsplit=1)
    if not parts:
        await message.answer("Первым должен быть @username или Telegram ID.")
        return
    await api.moderate_user("delete", message.from_user.id, parts[0], parts[1] if len(parts) > 1 else None)
    await state.clear()
    await message.answer("Пользователь удален из активной базы.", reply_markup=admin_users_keyboard())


@router.message(F.text.in_({"Включить день", "Выключить день"}))
async def admin_day_start(message: Message, state: FSMContext) -> None:
    await state.update_data(is_enabled=message.text == "Включить день")
    await state.set_state(AdminFlow.day_toggle)
    await message.answer("Введите номер дня недели: 0=Пн, 1=Вт, ..., 6=Вс", reply_markup=flow_cancel_keyboard())


@router.message(F.text.in_({"Лимит часов", "Лимит в неделю"}))
async def weekly_booking_limit_start(message: Message, state: FSMContext) -> None:
    current = await api.get_weekly_booking_limit(message.from_user.id)
    await state.set_state(AdminFlow.weekly_booking_limit)
    await message.answer(
        f"Текущий лимит: {format_hours(current['weekly_booking_limit_minutes'])} в неделю на коллектив.\n"
        "Введите новый лимит в часах. Например: 4 или 3.5",
        reply_markup=flow_cancel_keyboard(),
    )


@router.message(F.text == "Лимит в день")
async def daily_booking_limit_start(message: Message, state: FSMContext) -> None:
    current = await api.get_daily_booking_limit(message.from_user.id)
    await state.set_state(AdminFlow.daily_booking_limit)
    current_text = "выключен" if current["daily_booking_limit_minutes"] == 0 else format_hours(current["daily_booking_limit_minutes"])
    await message.answer(
        f"Текущий дневной лимит: {current_text} на коллектив.\n"
        "Введите новый лимит в часах. Например: 2 или 1.5.\n"
        "Введите 0, чтобы выключить дневной лимит.",
        reply_markup=flow_cancel_keyboard(),
    )


@router.message(F.text == "Часы кабинета")
async def booking_window_start(message: Message, state: FSMContext) -> None:
    current = await api.get_booking_window(message.from_user.id)
    await state.set_state(AdminFlow.booking_window)
    await message.answer(
        f"Текущие часы кабинета: {current['start_time']}-{current['end_time']}.\n"
        "Введите новое окно в формате 08:00-21:00",
        reply_markup=flow_cancel_keyboard(),
    )


@router.message(F.text == "Занять время")
async def staff_booking_start(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminFlow.staff_booking_date)
    await message.answer(
        "Введите дату, когда нужно занять кабинет, в формате YYYY-MM-DD.\n"
        "Например: 2026-06-25",
        reply_markup=flow_cancel_keyboard(),
    )


@router.message(F.text == "Удалить бронь")
async def delete_booking_start(message: Message, state: FSMContext) -> None:
    rows = await api.schedule()
    await state.update_data(admin_delete_bookings=rows)
    if not rows:
        await message.answer("На ближайшие 7 дней броней нет.", reply_markup=admin_schedule_keyboard())
        return
    await message.answer("Выберите бронь, которую нужно удалить:", reply_markup=admin_booking_delete_list_keyboard(rows))


@router.message(AdminFlow.booking_window)
async def booking_window_save(message: Message, state: FSMContext) -> None:
    parsed = parse_booking_window(message.text)
    if parsed is None:
        await message.answer("Введите окно в формате 08:00-21:00", reply_markup=flow_cancel_keyboard())
        return
    start, end = parsed
    if start.time() >= end.time():
        await message.answer("Начало должно быть раньше конца. Например: 08:00-21:00", reply_markup=flow_cancel_keyboard())
        return
    updated = await api.update_booking_window(message.from_user.id, start.time(), end.time())
    await state.clear()
    await message.answer(
        f"Часы кабинета обновлены: {updated['start_time']}-{updated['end_time']}.",
        reply_markup=admin_schedule_keyboard(),
    )


@router.message(AdminFlow.staff_booking_date)
async def staff_booking_date_save(message: Message, state: FSMContext) -> None:
    try:
        booking_date = date.fromisoformat(message.text.strip())
    except ValueError:
        await message.answer("Введите дату в формате YYYY-MM-DD. Например: 2026-06-25", reply_markup=flow_cancel_keyboard())
        return
    now = datetime.now()
    if booking_date < now.date() or booking_date > (now + timedelta(days=7)).date():
        await message.answer("Можно занять кабинет только в пределах ближайших 7 дней.", reply_markup=flow_cancel_keyboard())
        return
    await state.update_data(staff_booking_date=booking_date.isoformat())
    await state.set_state(AdminFlow.staff_booking_time)
    await message.answer(
        "Введите время в формате 14:00-18:00.\n"
        "Начало и конец должны быть по полчаса: 08:00, 08:30, 09:00 и т.д.",
        reply_markup=flow_cancel_keyboard(),
    )


@router.message(AdminFlow.staff_booking_time)
async def staff_booking_time_save(message: Message, state: FSMContext) -> None:
    parsed = parse_booking_window(message.text)
    if parsed is None:
        await message.answer("Введите время в формате 14:00-18:00", reply_markup=flow_cancel_keyboard())
        return
    start, end = parsed
    if start.time() >= end.time():
        await message.answer("Начало должно быть раньше конца. Например: 14:00-18:00", reply_markup=flow_cancel_keyboard())
        return
    if start.minute not in {0, 30} or end.minute not in {0, 30}:
        await message.answer("Время должно идти по полчаса: 14:00, 14:30, 15:00.", reply_markup=flow_cancel_keyboard())
        return
    await state.update_data(staff_booking_start=start.time().isoformat(), staff_booking_end=end.time().isoformat())
    await state.set_state(AdminFlow.staff_booking_title)
    await message.answer("Введите название или причину. Например: мастер-класс, soundcheck, собрание.", reply_markup=flow_cancel_keyboard())


@router.message(AdminFlow.staff_booking_title)
async def staff_booking_title_save(message: Message, state: FSMContext) -> None:
    title = message.text.strip()
    if not title:
        await message.answer("Название не должно быть пустым.", reply_markup=flow_cancel_keyboard())
        return
    if len(title) > 150:
        await message.answer("Название должно быть не длиннее 150 символов.", reply_markup=flow_cancel_keyboard())
        return
    data = await state.get_data()
    try:
        booking = await api.create_staff_booking(
            message.from_user.id,
            date.fromisoformat(data["staff_booking_date"]),
            datetime.strptime(data["staff_booking_start"], "%H:%M:%S").time(),
            datetime.strptime(data["staff_booking_end"], "%H:%M:%S").time(),
            title,
        )
    except RuntimeError as exc:
        await state.clear()
        await message.answer(f"Не получилось занять кабинет: {exc}", reply_markup=admin_schedule_keyboard())
        return
    await state.clear()
    await refresh_all_schedule_menus(message.bot)
    await message.answer(
        f"Кабинет занят: {booking['booking_date']} {booking['start_time'][:5]}-{booking['end_time'][:5]} | {title}",
        reply_markup=admin_schedule_keyboard(),
    )


@router.message(AdminFlow.daily_booking_limit)
async def daily_booking_limit_save(message: Message, state: FSMContext) -> None:
    try:
        hours = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("Введите число часов. Например: 2 или 1.5. Для выключения: 0", reply_markup=flow_cancel_keyboard())
        return
    if hours < 0 or hours > 24:
        await message.answer("Лимит должен быть от 0 до 24 часов.", reply_markup=flow_cancel_keyboard())
        return
    updated = await api.update_daily_booking_limit(message.from_user.id, hours)
    await state.clear()
    if updated["daily_booking_limit_minutes"] == 0:
        text = "Дневной лимит выключен."
    else:
        text = f"Дневной лимит обновлен: {format_hours(updated['daily_booking_limit_minutes'])} в день на коллектив."
    await message.answer(text, reply_markup=admin_settings_keyboard())


@router.message(AdminFlow.weekly_booking_limit)
async def weekly_booking_limit_save(message: Message, state: FSMContext) -> None:
    try:
        hours = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("Введите число часов. Например: 4 или 3.5", reply_markup=flow_cancel_keyboard())
        return
    if hours <= 0 or hours > 24:
        await message.answer("Лимит должен быть больше 0 и не больше 24 часов.", reply_markup=flow_cancel_keyboard())
        return
    updated = await api.update_weekly_booking_limit(message.from_user.id, hours)
    await state.clear()
    await message.answer(
        f"Лимит обновлен: {format_hours(updated['weekly_booking_limit_minutes'])} в неделю на коллектив.",
        reply_markup=admin_settings_keyboard(),
    )


@router.message(AdminFlow.day_toggle)
async def admin_day_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        day = int(message.text.strip())
    except ValueError:
        await message.answer("Введите число от 0 до 6.")
        return
    await api.update_day(message.from_user.id, day, data["is_enabled"])
    await state.clear()
    await message.answer("День обновлен.", reply_markup=admin_schedule_keyboard())


@router.message(F.text == "Создать концерт")
async def event_create_start(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminFlow.event_title)
    await message.answer("Введите название концерта:", reply_markup=flow_cancel_keyboard())


@router.message(AdminFlow.event_edit_value)
async def event_edit_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    event_id = data["event_edit_id"]
    field = data["event_edit_field"]
    raw = message.text.strip()
    payload: dict = {}
    if field in {"event_date", "submission_deadline"}:
        if raw == "-" and field == "submission_deadline":
            payload[field] = None
        else:
            try:
                date.fromisoformat(raw)
            except ValueError:
                await message.answer("Неверный формат даты. Пример: 2026-06-25", reply_markup=flow_cancel_keyboard())
                return
            payload[field] = raw
    elif field == "location":
        payload[field] = None if raw == "-" else raw
    elif field == "title":
        if not raw:
            await message.answer("Название не должно быть пустым.", reply_markup=flow_cancel_keyboard())
            return
        payload[field] = raw
    else:
        await state.clear()
        await message.answer("Это поле нельзя редактировать.", reply_markup=admin_inline_keyboard())
        return
    updated = await api.update_event(message.from_user.id, event_id, payload)
    await state.clear()
    await message.answer(
        "Концерт обновлен.\n\n" + format_event_card(updated),
        reply_markup=admin_event_detail_keyboard(event_id, updated["status"]),
    )


@router.message(AdminFlow.event_title)
async def event_title(message: Message, state: FSMContext) -> None:
    await state.update_data(title=message.text.strip())
    await state.set_state(AdminFlow.event_date)
    await message.answer("Введите дату концерта YYYY-MM-DD:", reply_markup=flow_cancel_keyboard())


@router.message(AdminFlow.event_date)
async def event_date(message: Message, state: FSMContext) -> None:
    try:
        date.fromisoformat(message.text.strip())
    except ValueError:
        await message.answer("Неверный формат. Пример: 2026-06-25")
        return
    await state.update_data(event_date=message.text.strip())
    await state.set_state(AdminFlow.event_location)
    await message.answer("Введите место проведения:", reply_markup=flow_cancel_keyboard())


@router.message(AdminFlow.event_location)
async def event_location(message: Message, state: FSMContext) -> None:
    await state.update_data(location=message.text.strip())
    await state.set_state(AdminFlow.event_deadline)
    await message.answer("Введите дедлайн подачи списков YYYY-MM-DD или '-' если дедлайна нет:", reply_markup=flow_cancel_keyboard())


@router.message(AdminFlow.event_deadline)
async def event_deadline(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    raw_deadline = message.text.strip()
    if raw_deadline != "-":
        try:
            date.fromisoformat(raw_deadline)
        except ValueError:
            await message.answer("Неверный формат. Пример: 2026-06-25")
            return
        data["submission_deadline"] = raw_deadline
    event = await api.create_event(message.from_user.id, data)
    await state.clear()
    events = await api.list_events()
    await message.answer(f"Концерт создан: {event['title']}", reply_markup=admin_events_keyboard(events))


@router.message(F.text == "Назад")
async def back(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await api.get_user(message.from_user.id)
    await show_menu(message, user)


@router.message()
async def fallback(message: Message, state: FSMContext) -> None:
    user = await api.upsert_user(message.from_user)
    if not user["profile_completed"]:
        await prompt_next_profile_step(message, state, user)
        return
    if user["status"] == "pending":
        await notify_admins_about_registration(message, user)
        await send_pending_notice(message)
        return
    if user["status"] == "rejected":
        try:
            user = await api.resubmit_registration(message.from_user.id)
        except RuntimeError:
            retry_after = user.get("registration_retry_after")
            reason = user.get("moderation_reason") or "без причины"
            if retry_after:
                await message.answer(
                    "Ваша заявка была отклонена, но это не бан.\n"
                    f"Причина: {reason}\n"
                    f"Повторно подать заявку можно после: {retry_after}"
                )
            else:
                await message.answer(
                    "Ваша заявка была отклонена, но это не бан. Попробуйте подать заявку позже или обратитесь к админам."
                )
            return
        await notify_admins_about_registration(message, user, force=True)
        await message.answer("Заявка отправлена повторно.")
        await send_pending_notice(message)
        return
    if user["status"] in {"banned", "deleted"}:
        await message.answer("Ваш аккаунт не имеет доступа к боту. Обратитесь к админам AITU Music Club.")
        return
    await show_menu(message, user)




