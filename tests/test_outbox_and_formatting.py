import json

from app.bot.handlers_history import format_history
from app.services import outbox


class _BrokenRedis:
    async def rpush(self, *args, **kwargs):
        raise ConnectionError("redis is down")


class _RecordingRedis:
    def __init__(self):
        self.pushed = []

    async def rpush(self, key, *values):
        self.pushed.append((key, values))


async def test_publish_swallows_redis_errors(monkeypatch):
    monkeypatch.undo()  # use the real publish, not the autouse capture
    monkeypatch.setattr(outbox, "_redis", lambda: _BrokenRedis())
    await outbox.publish([{"type": "schedule_changed"}])  # must not raise


async def test_publish_pushes_json_to_the_outbox_list(monkeypatch):
    monkeypatch.undo()
    fake = _RecordingRedis()
    monkeypatch.setattr(outbox, "_redis", lambda: fake)
    await outbox.publish([{"type": "notify", "telegram_id": 1, "text": "Привет"}])
    key, values = fake.pushed[0]
    assert key == "bot:outbox"
    assert json.loads(values[0]) == {"type": "notify", "telegram_id": 1, "text": "Привет"}


def test_format_history_renders_update_with_local_time():
    rows = [
        {
            "created_at": "2026-10-16T12:00:00",
            "action": "updated",
            "actor_name": "Петрова Мария",
            "band_name": "Dancers",
            "before": {"date": "2026-10-20", "start": "14:00", "end": "15:00", "song_title": "A"},
            "after": {"date": "2026-10-20", "start": "16:00", "end": "17:00", "song_title": "A"},
            "reason": None,
        }
    ]
    text = format_history(rows, "История")
    assert "16.10 17:00" in text  # 12:00 UTC is 17:00 in Asia/Almaty
    assert "20.10 14:00–15:00 · A → 20.10 16:00–17:00 · A" in text
    assert "Dancers" in text


def test_format_history_empty():
    assert "Изменений пока нет" in format_history([], "История")
