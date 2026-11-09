"""The bot side of rehearsal reminders: the "don't remind me" button and the switch in the profile."""
from app.bot import handlers, handlers_reminders


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.edits: list[tuple[str, object]] = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))


class FakeCallback:
    def __init__(self, data: str, text: str = "🔔 Завтра в 18:00 репетиция «Echo» (до 20:00).") -> None:
        self.data = data
        self.from_user = type("From", (), {"id": 42})()
        self.message = FakeMessage(text)
        self.answers: list[tuple] = []

    async def answer(self, text=None, show_alert=False, **_):
        self.answers.append((text, show_alert))


class FakeApi:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.updates: list[tuple[int, dict]] = []

    async def update_settings(self, telegram_id, payload):
        if self.fail:
            raise RuntimeError("backend is down")
        self.updates.append((telegram_id, payload))
        return {"rehearsal_reminders_enabled": payload["rehearsal_reminders_enabled"]}


async def test_the_button_turns_reminders_off_and_leaves_the_reminder_text(monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(handlers_reminders, "api", api)
    callback = FakeCallback("reminders:off")

    await handlers_reminders.turn_reminders_off(callback)

    assert api.updates == [(42, {"rehearsal_reminders_enabled": False})]
    [(text, markup)] = callback.message.edits
    assert text.startswith("🔔 Завтра в 18:00") and "выключены" in text
    assert markup is None  # the button is gone


async def test_a_backend_failure_keeps_the_button_and_says_so(monkeypatch):
    monkeypatch.setattr(handlers_reminders, "api", FakeApi(fail=True))
    callback = FakeCallback("reminders:off")

    await handlers_reminders.turn_reminders_off(callback)

    assert callback.message.edits == []
    assert callback.answers and callback.answers[0][1] is True


def test_the_profile_shows_both_switches():
    user = {
        "first_name": "Иван", "last_name": "Петров", "study_group": "IT-2301", "language": "ru", "telegram_username": "ivan",
        "notifications_enabled": True, "rehearsal_reminders_enabled": False,
    }
    text = handlers.profile_text(user)
    assert "Уведомления: включены" in text
    assert "Напоминания о репетициях: выключены" in text


def test_the_profile_keyboard_has_the_reminders_switch():
    from app.bot.keyboards import profile_inline_keyboard

    callbacks = [button.callback_data for row in profile_inline_keyboard().inline_keyboard for button in row]
    assert "profile:reminders" in callbacks and "profile:notifications" in callbacks
