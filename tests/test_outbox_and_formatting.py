import json

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
