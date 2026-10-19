"""The action journal (audit log) in the Mini App: the main admin reads it, searches it and pages through it."""
from datetime import datetime, timedelta

from app.bot import keyboards
from app.core.config import settings as app_settings
from app.db import models
from tests.test_webapp import client, tg  # noqa: F401  (`client` is a fixture)
from tests.test_webapp_admin import API, staff

JOURNAL = f"{API}/actions"


async def add_actions(session, rows: list[tuple[str, str, int | None]]) -> list[int]:
    """(action, description, actor_id) rows, oldest first; returns their ids in the same order."""
    items = [
        models.AuditLog(
            actor_id=actor_id,
            action=action,
            entity_type=action.split(".")[0],
            description=description,
            created_at=datetime(2026, 1, 1, 20, 0) + timedelta(minutes=index),
        )
        for index, (action, description, actor_id) in enumerate(rows)
    ]
    session.add_all(items)
    await session.commit()
    return [item.id for item in items]


def _ids(body) -> list[int]:
    return [item["id"] for item in body["items"]]


async def _get(client, user, query: str = "") -> dict:
    response = await client.get(f"{JOURNAL}{query}", headers=tg(user.telegram_id))
    assert response.status_code == 200, response.text
    return response.json()


async def test_only_the_main_admin_reads_the_journal(client, session, make_user):
    main_admin, group_admin, _, _ = await staff(make_user, session)
    dsvr = await make_user(role="dsvr")
    plain = await make_user()

    assert (await client.get(JOURNAL, headers=tg(main_admin.telegram_id))).status_code == 200
    for user in (group_admin, dsvr, plain):
        assert (await client.get(JOURNAL, headers=tg(user.telegram_id))).status_code == 403
    assert (await client.get(JOURNAL)).status_code == 401


async def test_rows_carry_the_actor_and_club_time_and_system_rows_are_named(client, session, make_user):
    main_admin, _, _, _ = await staff(make_user, session)
    actor = await make_user(last_name="Ivanov", first_name="Oleg")
    actor_id = actor.id
    [by_actor, by_system] = await add_actions(session, [("user.ban", "Забанил пользователя", actor_id), ("planning.allocate", "Авто", None)])

    body = await _get(client, main_admin)

    system, person = body["items"]
    assert (system["id"], person["id"]) == (by_system, by_actor)
    assert person["actor_name"] == "Ivanov Oleg" and system["actor_name"] == "система"
    assert person["created_local"] == "2026-01-02T01:00"  # 20:00 UTC in Asia/Almaty (+5)


async def test_search_category_and_paging(client, session, make_user):
    main_admin, _, _, _ = await staff(make_user, session)
    actor = await make_user(last_name="Ivanov", first_name="Oleg", telegram_username="olegi")
    actor_id = actor.id
    ids = await add_actions(
        session,
        [
            ("user.approve", "Одобрил заявку: @anna", actor_id),
            ("band.rename", "Переименовал коллектив: Rockers -> Metallers", None),
            ("user.ban", "Забанил пользователя: @spam. Причина: 100% спам", None),
            ("settings.weekly_limit.update", "Обновил недельный лимит: 4 ч", None),
            ("user.reject", "Отклонил заявку: @bob", actor_id),
        ],
    )

    assert _ids(await _get(client, main_admin, "?category=user")) == [ids[4], ids[2], ids[0]]
    assert _ids(await _get(client, main_admin, "?category=band")) == [ids[1]]
    assert _ids(await _get(client, main_admin, "?q=ROCKERS")) == [ids[1]]
    assert _ids(await _get(client, main_admin, "?q=ivanov")) == [ids[4], ids[0]]  # by the actor's name
    assert _ids(await _get(client, main_admin, "?q=@olegi&category=user")) == [ids[4], ids[0]]
    assert _ids(await _get(client, main_admin, "?q=%25")) == [ids[2]]  # a literal percent sign
    assert _ids(await _get(client, main_admin, "?q=nothing")) == []

    first = await _get(client, main_admin, "?limit=2")
    assert _ids(first) == [ids[4], ids[3]] and first["next_before_id"] == ids[3]
    second = await _get(client, main_admin, f"?limit=2&before_id={first['next_before_id']}")
    assert _ids(second) == [ids[2], ids[1]] and second["next_before_id"] == ids[1]
    last = await _get(client, main_admin, f"?limit=2&before_id={second['next_before_id']}")
    assert _ids(last) == [ids[0]] and last["next_before_id"] is None


async def test_unknown_categories_and_oversized_queries_are_rejected(client, session, make_user):
    main_admin, _, _, _ = await staff(make_user, session)
    headers = tg(main_admin.telegram_id)

    assert (await client.get(f"{JOURNAL}?category=nonsense", headers=headers)).status_code == 422
    assert (await client.get(f"{JOURNAL}?q={'x' * 101}", headers=headers)).status_code == 422
    assert (await client.get(f"{JOURNAL}?limit=51", headers=headers)).status_code == 422


async def test_the_bots_quick_view_still_reads_the_same_journal(client, session, make_user):
    main_admin, _, _, _ = await staff(make_user, session)
    admin_tg = main_admin.telegram_id
    await add_actions(session, [("user.approve", "Одобрил заявку: @anna", None)])

    response = await client.get(
        "/admin/actions/history",
        params={"telegram_id": admin_tg},
        headers={"X-Internal-Token": app_settings.internal_api_token},
    )

    assert response.status_code == 200
    assert [row["description"] for row in response.json()] == ["Одобрил заявку: @anna"]


def test_the_bot_opens_the_journal_in_the_mini_app():
    urls = [
        button.web_app.url
        for row in keyboards.admin_actions_keyboard("https://club.example/").inline_keyboard
        for button in row
        if button.web_app
    ]

    assert "https://club.example/webapp/schedule?screen=admin&tab=history&view=actions" in urls
    assert "https://club.example/webapp/schedule?screen=admin&tab=history" in urls
