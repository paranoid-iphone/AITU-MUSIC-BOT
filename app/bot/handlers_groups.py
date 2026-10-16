from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers import answer_callback, api, edit_screen
from app.bot.keyboards import flow_cancel_keyboard, group_detail_keyboard, groups_keyboard
from app.bot.states import AdminFlow

router = Router()


async def _require_main_admin(callback: CallbackQuery) -> bool:
    user = await api.get_user(callback.from_user.id)
    if user.get("role") != "admin":
        await answer_callback(callback, "Нет доступа", show_alert=True)
        return False
    return True


async def _show_group(callback_or_message, telegram_id: int, group_id: int, prefix: str = "") -> None:
    groups = await api.list_groups()
    group = next((item for item in groups if item["id"] == group_id), None)
    if group is None:
        text, keyboard = "Группа не найдена.", groups_keyboard(groups)
    else:
        admins = await api.list_group_admins(telegram_id, group_id)
        lines = [f"{prefix}Группа: {group['title']}", "", "Админы группы:"]
        for admin in admins:
            name = f"{admin.get('last_name') or ''} {admin.get('first_name') or ''}".strip() or str(admin["telegram_id"])
            lines.append(f"- {name} (@{admin['telegram_username']})" if admin.get("telegram_username") else f"- {name}")
        if not admins:
            lines.append("пока нет")
        text, keyboard = "\n".join(lines), group_detail_keyboard(group_id, admins)
    if isinstance(callback_or_message, CallbackQuery):
        await edit_screen(callback_or_message, text, keyboard)
    else:
        await callback_or_message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "groups:list")
async def groups_list(callback: CallbackQuery) -> None:
    if not await _require_main_admin(callback):
        return
    await edit_screen(callback, "Группы: выберите, чтобы управлять админами.", groups_keyboard(await api.list_groups()))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("groups:open:"))
async def groups_open(callback: CallbackQuery) -> None:
    if not await _require_main_admin(callback):
        return
    await _show_group(callback, callback.from_user.id, int(callback.data.rsplit(":", maxsplit=1)[-1]))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("groups:add:"))
async def groups_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_main_admin(callback):
        return
    await state.update_data(group_admin_group_id=int(callback.data.rsplit(":", maxsplit=1)[-1]))
    await state.set_state(AdminFlow.group_admin_add)
    await callback.message.answer(
        "Отправьте @username или Telegram ID пользователя. Он должен быть одобрен.",
        reply_markup=flow_cancel_keyboard(),
    )
    await answer_callback(callback)


@router.message(AdminFlow.group_admin_add)
async def groups_add_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    group_id = int(data["group_admin_group_id"])
    target = (message.text or "").strip()
    try:
        user = await api.add_group_admin(message.from_user.id, group_id, target)
    except RuntimeError as exc:
        await message.answer(f"Не получилось назначить админа: {exc}", reply_markup=flow_cancel_keyboard())
        return
    await state.clear()
    await _show_group(message, message.from_user.id, group_id, prefix=f"Админ группы назначен: @{user['telegram_username'] or user['telegram_id']}\n\n")
    try:
        await message.bot.send_message(user["telegram_id"], "Вас назначили админом группы. Откройте /start, чтобы увидеть админ-меню.")
    except Exception:
        pass


@router.callback_query(F.data.startswith("groups:remove:"))
async def groups_remove(callback: CallbackQuery) -> None:
    if not await _require_main_admin(callback):
        return
    _, _, group_id_raw, target = callback.data.split(":")
    group_id = int(group_id_raw)
    try:
        await api.remove_group_admin(callback.from_user.id, group_id, target)
    except RuntimeError as exc:
        await answer_callback(callback, str(exc), show_alert=True)
        return
    await _show_group(callback, callback.from_user.id, group_id)
    await answer_callback(callback, "Готово")
