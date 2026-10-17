"""Mini App admin API: requests, users and roles, plus the user notifications those actions queue."""
import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.db import models
from tests.test_webapp import client, tg  # noqa: F401  (`client` is a fixture)
from tests.test_webapp_auth import init_data

API = "/webapp/api/admin"
APPROVED_TEXT = "Ваша регистрация одобрена. Теперь доступны все функции AITU Music Club Bot."
REJECTED_TEXT = (
    "Ваша регистрация отклонена, но это не бан.\n"
    "Повторно подать заявку можно после: {retry_after}.\n"
    "Когда срок пройдет, отправьте /start."
)
PROMOTED_TEXT = "Вам выдали права админа в AITU Music Club Bot."
DEMOTED_TEXT = "С вас сняли права админа в AITU Music Club Bot."
GROUP_ADMIN_TEXT = "Вас назначили админом группы. Откройте /start, чтобы увидеть админ-меню."


async def group_id(session, slug: str) -> int:
    return (await session.execute(select(models.Group.id).where(models.Group.slug == slug))).scalar_one()


async def staff(make_user, session):
    """A main admin, a group admin of `music`, a pending `music` user and a pending user of another group."""
    main_admin = await make_user(role="admin", group_slug=None)
    group_admin = await make_user(group_slug="music")
    session.add(models.GroupAdmin(group_id=await group_id(session, "music"), user_id=group_admin.id))
    await session.commit()
    pending_music = await make_user(status="pending", group_slug="music")
    pending_other = await make_user(status="pending", group_slug="dance")
    return main_admin, group_admin, pending_music, pending_other


def notifications(published) -> list[dict]:
    return [event for event in published if event["type"] == "notify"]


async def test_admin_endpoints_reject_everyone_but_admins(client, make_user):
    plain = await make_user()
    dsvr = await make_user(role="dsvr")
    pending = await make_user(status="pending")
    for user in (plain, dsvr, pending):
        assert (await client.get(f"{API}/users", headers=tg(user.telegram_id))).status_code == 403
    assert (await client.get(f"{API}/users")).status_code == 401


async def test_group_admin_sees_only_their_group_and_counts_match(client, session, make_user):
    main_admin, group_admin, pending_music, pending_other = await staff(make_user, session)

    scoped = (await client.get(f"{API}/users", headers=tg(group_admin.telegram_id))).json()
    ids = {item["telegram_id"] for item in scoped["users"]}
    assert pending_music.telegram_id in ids and pending_other.telegram_id not in ids
    assert scoped["counts"]["pending"] == 1

    everything = (await client.get(f"{API}/users", headers=tg(main_admin.telegram_id))).json()
    ids = {item["telegram_id"] for item in everything["users"]}
    assert {pending_music.telegram_id, pending_other.telegram_id} <= ids
    assert everything["counts"]["pending"] == 2


async def test_users_filter_by_status_and_search(client, session, make_user):
    main_admin, _, pending_music, pending_other = await staff(make_user, session)
    named = await make_user(last_name="Уникальный", telegram_username="needle_guy")
    headers = tg(main_admin.telegram_id)

    pending = (await client.get(f"{API}/users", params={"status": "pending"}, headers=headers)).json()
    assert {item["telegram_id"] for item in pending["users"]} == {pending_music.telegram_id, pending_other.telegram_id}
    assert pending["counts"]["approved"] >= 1  # counts ignore the status filter

    for query in ("Уникальн", "needle", "@needle_guy", str(named.telegram_id)):
        found = (await client.get(f"{API}/users", params={"q": query}, headers=headers)).json()["users"]
        assert [item["telegram_id"] for item in found] == [named.telegram_id], query

    assert (await client.get(f"{API}/users", params={"q": "%"}, headers=headers)).json()["users"] == []
    assert (await client.get(f"{API}/users", params={"q": "ne_dle"}, headers=headers)).json()["users"] == []
    blank = (await client.get(f"{API}/users", params={"q": "   "}, headers=headers)).json()["users"]
    unfiltered = (await client.get(f"{API}/users", headers=headers)).json()["users"]
    assert len(blank) == len(unfiltered) > 1


async def test_approve_notifies_once_and_is_idempotent(client, session, make_user, published):
    main_admin, _, pending_music, _ = await staff(make_user, session)
    url = f"{API}/users/{pending_music.telegram_id}/approve"

    first = await client.post(url, headers=tg(main_admin.telegram_id))
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "approved"
    assert notifications(published) == [{"type": "notify", "telegram_id": pending_music.telegram_id, "text": APPROVED_TEXT}]

    second = await client.post(url, headers=tg(main_admin.telegram_id))
    assert second.status_code == 200 and second.json()["status"] == "approved"
    assert len(notifications(published)) == 1


async def test_reject_ban_delete_store_reason_and_only_reject_notifies(client, session, make_user, published):
    main_admin, _, pending_music, pending_other = await staff(make_user, session)
    victim = await make_user()
    headers = tg(main_admin.telegram_id)

    rejected = await client.post(
        f"{API}/users/{pending_music.telegram_id}/reject", json={"reason": "не студент"}, headers=headers
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected" and rejected.json()["moderation_reason"] == "не студент"
    sent = notifications(published)
    assert len(sent) == 1 and sent[0]["telegram_id"] == pending_music.telegram_id
    assert sent[0]["text"].startswith("Ваша регистрация отклонена, но это не бан.")
    assert "Повторно подать заявку можно после: 20" in sent[0]["text"]  # the retry date, not the placeholder

    banned = await client.post(f"{API}/users/{pending_other.telegram_id}/ban", json={"reason": "спам"}, headers=headers)
    assert banned.json()["status"] == "banned"
    deleted = await client.post(f"{API}/users/{victim.telegram_id}/delete", json={}, headers=headers)
    assert deleted.json()["status"] == "deleted"
    assert len(notifications(published)) == 1  # ban and delete stay silent

    too_long = await client.post(f"{API}/users/{victim.telegram_id}/ban", json={"reason": "x" * 256}, headers=headers)
    assert too_long.status_code == 422


async def test_group_admin_cannot_touch_staff_other_groups_or_themselves(client, session, make_user):
    main_admin, group_admin, _, pending_other = await staff(make_user, session)
    headers = tg(group_admin.telegram_id)
    for target in (main_admin, pending_other, group_admin):
        assert (await client.post(f"{API}/users/{target.telegram_id}/ban", json={}, headers=headers)).status_code == 403
    target = await make_user(group_slug="music")
    body = {"target": str(target.telegram_id), "enabled": True}
    assert (await client.get(f"{API}/staff", headers=headers)).status_code == 403
    assert (await client.post(f"{API}/staff/admin", json=body, headers=headers)).status_code == 403
    assert (await client.post(f"{API}/staff/dsvr", json=body, headers=headers)).status_code == 403
    music = await group_id(session, "music")
    assert (await client.post(f"{API}/groups/{music}/admins", json=body, headers=headers)).status_code == 403


async def test_group_admin_cannot_restore_a_banned_user_but_main_admin_can(client, session, make_user):
    main_admin, group_admin, _, _ = await staff(make_user, session)
    banned = await make_user(status="banned", group_slug="music")
    url = f"{API}/users/{banned.telegram_id}/approve"
    assert (await client.post(url, headers=tg(group_admin.telegram_id))).status_code == 403
    restored = await client.post(url, headers=tg(main_admin.telegram_id))
    assert restored.status_code == 200 and restored.json()["status"] == "approved"


async def test_users_without_a_group_are_invisible_to_group_admins(client, session, make_user):
    main_admin, group_admin, _, _ = await staff(make_user, session)
    loner = await make_user(status="pending", group_slug=None)

    listed = (await client.get(f"{API}/users", headers=tg(group_admin.telegram_id))).json()["users"]
    assert loner.telegram_id not in {item["telegram_id"] for item in listed}
    assert (await client.get(f"{API}/users/{loner.telegram_id}", headers=tg(group_admin.telegram_id))).status_code == 403
    assert (await client.post(f"{API}/users/{loner.telegram_id}/approve", headers=tg(group_admin.telegram_id))).status_code == 403

    as_main = (await client.get(f"{API}/users", headers=tg(main_admin.telegram_id))).json()["users"]
    assert loner.telegram_id in {item["telegram_id"] for item in as_main}
    assert (await client.get(f"{API}/users/{loner.telegram_id}", headers=tg(main_admin.telegram_id))).status_code == 200


async def test_initial_admin_and_self_are_protected(client, session, make_user, monkeypatch):
    main_admin, _, pending_music, _ = await staff(make_user, session)
    headers = tg(main_admin.telegram_id)
    monkeypatch.setattr(type(settings), "initial_admin_ids", property(lambda self: {pending_music.telegram_id}))

    assert (await client.post(f"{API}/users/{pending_music.telegram_id}/ban", json={}, headers=headers)).status_code == 403
    assert (await client.post(f"{API}/users/{main_admin.telegram_id}/ban", json={}, headers=headers)).status_code == 403
    own = {"target": str(main_admin.telegram_id), "enabled": False}
    assert (await client.post(f"{API}/staff/admin", json=own, headers=headers)).status_code == 403


async def test_stale_init_data_reads_but_cannot_change(client, session, make_user):
    main_admin, _, pending_music, _ = await staff(make_user, session)
    stale = {"X-Telegram-Init-Data": init_data(main_admin.telegram_id, age=2 * 60 * 60)}
    assert (await client.get(f"{API}/users", headers=stale)).status_code == 200
    assert (await client.post(f"{API}/users/{pending_music.telegram_id}/approve", headers=stale)).status_code == 401


async def test_staff_lists_and_role_changes(client, session, make_user, published):
    main_admin, _, _, _ = await staff(make_user, session)
    headers = tg(main_admin.telegram_id)
    candidate = await make_user(telegram_username="candidate", group_slug="music")
    music = await group_id(session, "music")

    promoted = await client.post(f"{API}/staff/admin", json={"target": "@candidate", "enabled": True}, headers=headers)
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["role"] == "admin"
    assert notifications(published)[-1] == {"type": "notify", "telegram_id": candidate.telegram_id, "text": PROMOTED_TEXT}
    demoted = await client.post(f"{API}/staff/admin", json={"target": "@candidate", "enabled": False}, headers=headers)
    assert demoted.json()["role"] == "user"
    assert notifications(published)[-1]["text"] == DEMOTED_TEXT

    on = await client.post(f"{API}/staff/dsvr", json={"target": str(candidate.telegram_id), "enabled": True}, headers=headers)
    assert on.json()["role"] == "dsvr"
    listed = (await client.get(f"{API}/staff", headers=headers)).json()
    assert candidate.telegram_id in {item["telegram_id"] for item in listed["dsvr"]}
    off = await client.post(f"{API}/staff/dsvr", json={"target": str(candidate.telegram_id), "enabled": False}, headers=headers)
    assert off.json()["role"] == "user"

    before = len(notifications(published))
    added = await client.post(
        f"{API}/groups/{music}/admins", json={"target": str(candidate.telegram_id), "enabled": True}, headers=headers
    )
    assert added.status_code == 200, added.text
    assert notifications(published)[before:] == [
        {"type": "notify", "telegram_id": candidate.telegram_id, "text": GROUP_ADMIN_TEXT}
    ]
    listed = (await client.get(f"{API}/staff", headers=headers)).json()
    music_row = next(item for item in listed["group_admins"] if item["group"]["id"] == music)
    assert candidate.telegram_id in {item["telegram_id"] for item in music_row["admins"]}
    assert main_admin.telegram_id in {item["telegram_id"] for item in listed["admins"]}

    removed = await client.post(
        f"{API}/groups/{music}/admins", json={"target": str(candidate.telegram_id), "enabled": False}, headers=headers
    )
    assert removed.status_code == 200
    listed = (await client.get(f"{API}/staff", headers=headers)).json()
    music_row = next(item for item in listed["group_admins"] if item["group"]["id"] == music)
    assert candidate.telegram_id not in {item["telegram_id"] for item in music_row["admins"]}


@pytest.mark.parametrize("path", ["staff/admin", "staff/dsvr"])
async def test_unknown_target_is_404_and_readable(client, session, make_user, path):
    main_admin, _, _, _ = await staff(make_user, session)
    response = await client.post(f"{API}/{path}", json={"target": "@nobody", "enabled": True}, headers=tg(main_admin.telegram_id))
    assert response.status_code == 404 and response.json()["detail"] == "User not found"


async def test_notifications_respect_the_opt_out(client, session, make_user, published):
    main_admin, _, _, _ = await staff(make_user, session)
    quiet = await make_user(status="pending", notifications_enabled=False)
    audits_before = (await session.execute(select(func.count()).select_from(models.AuditLog))).scalar_one()

    response = await client.post(f"{API}/users/{quiet.telegram_id}/approve", headers=tg(main_admin.telegram_id))
    assert response.status_code == 200 and response.json()["status"] == "approved"
    assert notifications(published) == []
    audits_after = (await session.execute(select(func.count()).select_from(models.AuditLog))).scalar_one()
    assert audits_after == audits_before + 1
