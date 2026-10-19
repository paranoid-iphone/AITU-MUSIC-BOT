import asyncio
import json
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter

from app.bot import outbox_delivery
from app.bot.outbox_delivery import OUTBOX_KEY, PROCESSING_KEY


class FakeRedis:
    """The few list commands the worker uses, with Redis semantics (RPUSH on the right, consume from the left)."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {OUTBOX_KEY: [], PROCESSING_KEY: []}

    async def blmove(self, source, destination, timeout, src="LEFT", dest="RIGHT"):
        return await self.lmove(source, destination, src, dest)

    async def lmove(self, source, destination, src="LEFT", dest="RIGHT"):
        if not self.lists[source]:
            return None
        item = self.lists[source].pop(0 if src == "LEFT" else -1)
        if dest == "LEFT":
            self.lists[destination].insert(0, item)
        else:
            self.lists[destination].append(item)
        return item

    async def lrem(self, key, count, value):
        self.lists[key].remove(value)
        return 1


class FakeBot:
    def __init__(self, failures: list[Exception] | None = None) -> None:
        self.failures = list(failures or [])
        self.sent: list[tuple[int, str, object]] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append((chat_id, text, reply_markup))


class FakeRefresher:
    def __init__(self) -> None:
        self.dirty = 0

    def mark_dirty(self) -> None:
        self.dirty += 1


def _event(**extra) -> dict:
    return {"type": "notify", "telegram_id": 7, "text": "hi", **extra}


def _retry_after(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(method=SimpleNamespace(), message="flood", retry_after=seconds)


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    slept: list[float] = []

    async def _record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(outbox_delivery, "_sleep", _record)
    return slept


async def test_flood_limit_is_waited_out_and_the_same_event_is_sent(sleeps):
    bot = FakeBot([_retry_after(3), _retry_after(5)])

    await outbox_delivery.deliver(bot, _event(), FakeRefresher())

    assert sleeps == [3, 5]
    assert [(chat, text) for chat, text, _ in bot.sent] == [(7, "hi")]


async def test_network_errors_are_retried_with_a_growing_pause(sleeps):
    bot = FakeBot([TelegramNetworkError(method=SimpleNamespace(), message="down")] * 2)

    await outbox_delivery.deliver(bot, _event(), FakeRefresher())

    assert sleeps == [1, 2]
    assert len(bot.sent) == 1


async def test_gives_up_after_the_attempt_limit(sleeps):
    bot = FakeBot([_retry_after(1)] * 10)

    await outbox_delivery.deliver(bot, _event(), FakeRefresher())

    assert bot.sent == []
    assert len(sleeps) == outbox_delivery.MAX_ATTEMPTS - 1


async def test_a_blocked_user_is_dropped_without_retrying(sleeps):
    bot = FakeBot([TelegramForbiddenError(method=SimpleNamespace(), message="blocked")])

    await outbox_delivery.deliver(bot, _event(), FakeRefresher())

    assert bot.sent == [] and sleeps == []


async def test_events_with_buttons_carry_their_callbacks():
    bot = FakeBot()
    events = [
        {"type": "repeat_offer", "telegram_id": 1, "booking_id": 5, "text": "t", "button": "b"},
        {"type": "move_request", "telegram_id": 1, "request_id": 6, "text": "t", "accept": "a", "decline": "d"},
        {"type": "waitlist_offer", "telegram_id": 1, "entry_id": 8, "text": "t", "button": "b"},
    ]

    for event in events:
        await outbox_delivery.deliver(bot, event, FakeRefresher())

    callbacks = [[button.callback_data for button in markup.inline_keyboard[0]] for _, _, markup in bot.sent]
    assert callbacks == [["repeat:5"], ["move:accept:6", "move:decline:6"], ["waitlist:take:8"]]


async def test_schedule_changed_only_marks_the_menus_dirty():
    bot, refresher = FakeBot(), FakeRefresher()

    await outbox_delivery.deliver(bot, {"type": "schedule_changed"}, refresher)

    assert refresher.dirty == 1 and bot.sent == []


async def test_an_event_is_acknowledged_after_delivery():
    redis, bot = FakeRedis(), FakeBot()
    redis.lists[OUTBOX_KEY].append(json.dumps(_event()))

    assert await outbox_delivery.process_next(bot, redis, FakeRefresher()) is True

    assert len(bot.sent) == 1
    assert redis.lists == {OUTBOX_KEY: [], PROCESSING_KEY: []}
    assert await outbox_delivery.process_next(bot, redis, FakeRefresher()) is False


async def test_an_interrupted_delivery_stays_in_the_processing_list_and_is_restored_in_order():
    redis = FakeRedis()
    first, second, third = (json.dumps(_event(text=name)) for name in ("one", "two", "three"))
    redis.lists[OUTBOX_KEY] += [first, second, third]

    class Dying(FakeBot):
        async def send_message(self, *args, **kwargs):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await outbox_delivery.process_next(Dying(), redis, FakeRefresher())
    assert redis.lists[PROCESSING_KEY] == [first]

    # the next start also finds an event of an older crash that was taken after `first`
    redis.lists[PROCESSING_KEY].append(second)
    redis.lists[OUTBOX_KEY] = [third]
    assert await outbox_delivery.recover_processing(redis) == 2

    assert redis.lists[PROCESSING_KEY] == []
    assert redis.lists[OUTBOX_KEY] == [first, second, third]


async def test_a_malformed_event_is_dropped_instead_of_blocking_the_queue():
    redis, bot = FakeRedis(), FakeBot()
    redis.lists[OUTBOX_KEY] += ["not json", json.dumps({"type": "notify", "telegram_id": 1}), json.dumps(_event())]

    while await outbox_delivery.process_next(bot, redis, FakeRefresher()):
        pass

    assert [text for _, text, _ in bot.sent] == ["hi"]
    assert redis.lists == {OUTBOX_KEY: [], PROCESSING_KEY: []}
