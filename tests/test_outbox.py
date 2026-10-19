from datetime import datetime, timedelta

from sqlalchemy import select

from app.db.models import OutboxEvent
from app.services import outbox


async def _rows(session) -> list[OutboxEvent]:
    return list((await session.execute(select(OutboxEvent).order_by(OutboxEvent.id))).scalars())


async def test_events_are_stored_and_stamped_once_published(session, published):
    outbox.queue_notification(session, 1, "hello")
    outbox.queue_schedule_changed(session)
    await outbox.commit_and_publish(session)

    assert published == [{"type": "notify", "telegram_id": 1, "text": "hello"}, {"type": "schedule_changed"}]
    rows = await _rows(session)
    assert [row.payload for row in rows] == published
    assert all(row.published_at is not None for row in rows)


async def test_rolled_back_events_are_not_stored_or_published(session, published):
    outbox.queue_notification(session, 1, "never sent")
    await session.rollback()
    outbox.queue_notification(session, 2, "sent")
    await outbox.commit_and_publish(session)

    assert published == [{"type": "notify", "telegram_id": 2, "text": "sent"}]
    assert [row.payload["telegram_id"] for row in await _rows(session)] == [2]


async def test_a_rolled_back_schedule_change_does_not_suppress_the_next_one(session, published):
    outbox.queue_schedule_changed(session)
    await session.rollback()
    outbox.queue_schedule_changed(session)
    await outbox.commit_and_publish(session)

    assert published == [{"type": "schedule_changed"}]


async def test_schedule_changed_is_queued_once_per_transaction(session, published):
    outbox.queue_schedule_changed(session)
    outbox.queue_schedule_changed(session)
    await outbox.commit_and_publish(session)

    assert published == [{"type": "schedule_changed"}]
    assert len(await _rows(session)) == 1


async def test_a_commit_without_events_touches_nothing(session, published):
    await outbox.commit_and_publish(session)

    assert published == []
    assert await _rows(session) == []


async def test_events_stay_unpublished_when_redis_is_down(session, monkeypatch):
    async def _down(batch):
        return False

    monkeypatch.setattr(outbox, "publish", _down)
    outbox.queue_notification(session, 1, "later")
    await outbox.commit_and_publish(session)

    [row] = await _rows(session)
    assert row.published_at is None


async def test_republish_sends_old_unpublished_events_and_stamps_them(session, published, monkeypatch):
    async def _down(batch):
        return False

    with monkeypatch.context() as patch:
        patch.setattr(outbox, "publish", _down)
        outbox.queue_notification(session, 1, "later")
        await outbox.commit_and_publish(session)
    [row] = await _rows(session)
    row.created_at = datetime.utcnow() - timedelta(minutes=1)
    await session.commit()

    assert await outbox.republish_unpublished(session) == 1

    assert published == [{"type": "notify", "telegram_id": 1, "text": "later"}]
    assert (await _rows(session))[0].published_at is not None
    assert await outbox.republish_unpublished(session) == 0


async def test_republish_leaves_fresh_events_to_their_own_request(session, published, monkeypatch):
    async def _down(batch):
        return False

    with monkeypatch.context() as patch:
        patch.setattr(outbox, "publish", _down)
        outbox.queue_notification(session, 1, "fresh")
        await outbox.commit_and_publish(session)

    assert await outbox.republish_unpublished(session) == 0
    assert published == []


async def test_republish_keeps_events_while_redis_is_still_down(session, monkeypatch):
    async def _down(batch):
        return False

    monkeypatch.setattr(outbox, "publish", _down)
    outbox.queue_notification(session, 1, "later")
    await outbox.commit_and_publish(session)
    [row] = await _rows(session)
    row.created_at = datetime.utcnow() - timedelta(minutes=1)
    await session.commit()

    assert await outbox.republish_unpublished(session) == 0
    assert (await _rows(session))[0].published_at is None


async def test_purge_forgets_only_old_published_events(session, published):
    outbox.queue_notification(session, 1, "old")
    outbox.queue_notification(session, 2, "recent")
    await outbox.commit_and_publish(session)
    old, recent = await _rows(session)
    old.published_at = datetime.utcnow() - outbox.KEEP_PUBLISHED - timedelta(hours=1)
    unpublished = OutboxEvent(payload={"type": "notify", "telegram_id": 3, "text": "waiting"})
    unpublished.created_at = datetime.utcnow() - outbox.KEEP_PUBLISHED - timedelta(days=1)
    session.add(unpublished)
    await session.commit()

    assert await outbox.purge_published(session) == 1

    assert {row.payload["telegram_id"] for row in await _rows(session)} == {recent.payload["telegram_id"], 3}
