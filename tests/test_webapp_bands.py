"""Mini App band screens: list, create, join, rename, roles, responsible, owner, members, delete."""
from datetime import time

from sqlalchemy import select

from app.db import models
from app.services import bands as band_service
from app.services import bookings
from tests.test_webapp import NOW, client, tg  # noqa: F401  (`client` is a fixture)


async def with_member(session, make_user, make_band, **extra):
    leader = await make_user(last_name="Петров", first_name="Иван")
    band = await make_band(leader, "Rockers")
    member = await make_user(**extra)
    code = (await band_service.get_band_detail(session, band.id)).invite_code
    await band_service.join_band(session, member.telegram_id, code, "bass")
    session.expire(band, ["members"])  # the app has a session of its own; do not serve the test's cached members
    return leader, member, band, code


async def member_row(session, band, user):
    detail = await band_service.get_band_detail(session, band.id)
    return next(item for item in detail.members if item.user_id == user.id)


async def test_bands_list_shows_members_and_hides_the_code_from_plain_members(client, session, make_user, make_band):
    leader, member, band, code = await with_member(session, make_user, make_band, telegram_username="bassist")
    as_leader = (await client.get("/webapp/api/bands", headers=tg(leader.telegram_id))).json()["bands"]
    assert [item["name"] for item in as_leader] == ["Rockers"]
    assert as_leader[0]["invite_code"] == code and as_leader[0]["can_manage"] is True
    assert [(m["is_owner"], m["is_responsible"], m["is_me"]) for m in as_leader[0]["members"]] == [
        (True, True, True),
        (False, False, False),
    ]
    assert as_leader[0]["members"][1]["instrument_role"] == "bass"
    assert as_leader[0]["members"][1]["telegram_username"] == "bassist"

    as_member = (await client.get("/webapp/api/bands", headers=tg(member.telegram_id))).json()["bands"]
    assert as_member[0]["invite_code"] is None and as_member[0]["can_manage"] is False
    assert as_member[0]["members"][1]["is_me"] is True


async def test_bands_list_only_has_the_viewers_own_bands(client, session, make_user, make_band):
    await with_member(session, make_user, make_band)
    outsider = await make_user()
    assert (await client.get("/webapp/api/bands", headers=tg(outsider.telegram_id))).json() == {"bands": []}


async def test_bands_endpoints_need_an_approved_member(client, make_user):
    pending = await make_user(status="pending")
    assert (await client.get("/webapp/api/bands")).status_code == 401
    assert (await client.get("/webapp/api/bands", headers=tg(pending.telegram_id))).status_code == 403
    assert (await client.post("/webapp/api/bands", json={"name": "X"}, headers=tg(pending.telegram_id))).status_code == 403
    assert (await client.post("/webapp/api/bands/join", json={"invite_code": "ABC"}, headers=tg(pending.telegram_id))).status_code == 403


async def test_create_band_makes_the_viewer_leader_and_responsible(client, make_user):
    user = await make_user()
    response = await client.post(
        "/webapp/api/bands", json={"name": "  New band  ", "instrument_role": "drums"}, headers=tg(user.telegram_id)
    )
    assert response.status_code == 200, response.text
    band = response.json()
    assert band["name"] == "New band" and band["invite_code"] and band["can_manage"] is True
    assert (band["members"][0]["is_owner"], band["members"][0]["is_responsible"], band["members"][0]["instrument_role"]) == (True, True, "drums")
    assert band["group_title"]


async def test_create_band_rejects_a_blank_name(client, make_user):
    user = await make_user()
    assert (await client.post("/webapp/api/bands", json={"name": "   "}, headers=tg(user.telegram_id))).status_code == 422


async def test_join_by_code_ignores_case_and_spaces(client, session, make_user, make_band):
    leader = await make_user()
    band = await make_band(leader, "Rockers")
    code = (await band_service.get_band_detail(session, band.id)).invite_code
    guest = await make_user()
    response = await client.post(
        "/webapp/api/bands/join", json={"invite_code": f" {code.lower()} ", "instrument_role": "keys"}, headers=tg(guest.telegram_id)
    )
    assert response.status_code == 200, response.text
    joined = response.json()
    assert joined["id"] == band.id and joined["invite_code"] is None and joined["can_manage"] is False
    assert [m["instrument_role"] for m in joined["members"] if m["is_me"]] == ["keys"]


async def test_join_with_an_unknown_code_is_404(client, make_user):
    user = await make_user()
    assert (await client.post("/webapp/api/bands/join", json={"invite_code": "NOPE1234"}, headers=tg(user.telegram_id))).status_code == 404


async def test_leader_renames_sets_roles_and_responsible(client, session, make_user, make_band):
    leader, member, band, _ = await with_member(session, make_user, make_band)
    headers = tg(leader.telegram_id)
    target = await member_row(session, band, member)

    renamed = await client.patch(f"/webapp/api/bands/{band.id}", json={"name": "Metal"}, headers=headers)
    assert renamed.status_code == 200 and renamed.json()["name"] == "Metal"

    role = await client.patch(f"/webapp/api/bands/{band.id}/members/{target.id}", json={"instrument_role": "vocals"}, headers=headers)
    assert [m["instrument_role"] for m in role.json()["members"] if not m["is_owner"]] == ["vocals"]

    responsible = await client.patch(f"/webapp/api/bands/{band.id}/responsible", json={"member_id": target.id}, headers=headers)
    assert [(m["is_owner"], m["is_responsible"]) for m in responsible.json()["members"]] == [(True, False), (False, True)]


async def test_leader_hands_over_leadership(client, session, make_user, make_band):
    leader, member, band, _ = await with_member(session, make_user, make_band)
    target = await member_row(session, band, member)
    response = await client.patch(f"/webapp/api/bands/{band.id}/owner", json={"member_id": target.id}, headers=tg(leader.telegram_id))
    assert response.status_code == 200, response.text
    assert response.json()["can_manage"] is False  # the old leader is a plain member now
    assert [m["is_owner"] for m in response.json()["members"]] == [True, False]
    assert (await client.patch(f"/webapp/api/bands/{band.id}", json={"name": "Mine"}, headers=tg(leader.telegram_id))).status_code == 403
    assert (await client.patch(f"/webapp/api/bands/{band.id}", json={"name": "Ours"}, headers=tg(member.telegram_id))).status_code == 200


async def test_plain_member_cannot_manage_the_band(client, session, make_user, make_band):
    leader, member, band, _ = await with_member(session, make_user, make_band)
    own = await member_row(session, band, leader)
    headers = tg(member.telegram_id)
    assert (await client.patch(f"/webapp/api/bands/{band.id}", json={"name": "Hijack"}, headers=headers)).status_code == 403
    assert (await client.patch(f"/webapp/api/bands/{band.id}/owner", json={"member_id": (await member_row(session, band, member)).id}, headers=headers)).status_code == 403
    assert (await client.patch(f"/webapp/api/bands/{band.id}/responsible", json={"member_id": own.id}, headers=headers)).status_code == 403
    assert (await client.patch(f"/webapp/api/bands/{band.id}/members/{own.id}", json={"instrument_role": "x"}, headers=headers)).status_code == 403
    assert (await client.delete(f"/webapp/api/bands/{band.id}/members/{own.id}", headers=headers)).status_code == 403
    assert (await client.delete(f"/webapp/api/bands/{band.id}", headers=headers)).status_code == 403
    assert (await band_service.get_band_detail(session, band.id)).name == "Rockers"


async def test_admin_of_another_group_cannot_manage_the_band(client, session, make_user, make_group_admin, make_band):
    leader, _, band, _ = await with_member(session, make_user, make_band)
    dance_admin = await make_user(group_slug="dance")
    await make_group_admin(dance_admin, "dance")
    headers = tg(dance_admin.telegram_id)
    assert (await client.patch(f"/webapp/api/bands/{band.id}", json={"name": "Hijack"}, headers=headers)).status_code == 403
    assert (await client.delete(f"/webapp/api/bands/{band.id}", headers=headers)).status_code == 403


async def test_outsider_cannot_touch_a_band(client, session, make_user, make_band):
    leader, _, band, _ = await with_member(session, make_user, make_band)
    outsider = await make_user()
    own = await member_row(session, band, leader)
    headers = tg(outsider.telegram_id)
    assert (await client.patch(f"/webapp/api/bands/{band.id}", json={"name": "Hijack"}, headers=headers)).status_code == 403
    assert (await client.delete(f"/webapp/api/bands/{band.id}/members/{own.id}", headers=headers)).status_code == 403


async def test_leader_removes_a_member_but_not_the_leader(client, session, make_user, make_band):
    leader, member, band, _ = await with_member(session, make_user, make_band)
    headers = tg(leader.telegram_id)
    target = await member_row(session, band, member)
    own = await member_row(session, band, leader)
    assert (await client.delete(f"/webapp/api/bands/{band.id}/members/{own.id}", headers=headers)).status_code == 403
    assert (await client.delete(f"/webapp/api/bands/{band.id}/members/{target.id}", headers=headers)).status_code == 200
    assert (await client.get("/webapp/api/bands", headers=tg(member.telegram_id))).json() == {"bands": []}


async def test_removing_the_responsible_member_hands_it_back_to_the_leader(client, session, make_user, make_band):
    leader, member, band, _ = await with_member(session, make_user, make_band)
    headers = tg(leader.telegram_id)
    target = await member_row(session, band, member)
    await client.patch(f"/webapp/api/bands/{band.id}/responsible", json={"member_id": target.id}, headers=headers)
    await client.delete(f"/webapp/api/bands/{band.id}/members/{target.id}", headers=headers)
    session.expire(band, ["members"])  # a real request would open a new session
    band_after = (await client.get("/webapp/api/bands", headers=headers)).json()["bands"][0]
    assert [(m["is_owner"], m["is_responsible"]) for m in band_after["members"]] == [(True, True)]


async def test_deleting_a_band_cancels_its_future_bookings(client, session, make_user, make_band, freeze_now, published):
    freeze_now(NOW)
    leader, _, band, _ = await with_member(session, make_user, make_band)
    await bookings.create_booking(session, leader.telegram_id, band.id, NOW.date(), time(15), time(16), "self", "Song")
    response = await client.delete(f"/webapp/api/bands/{band.id}", headers=tg(leader.telegram_id))
    assert response.status_code == 200, response.text
    assert (await client.get("/webapp/api/bands", headers=tg(leader.telegram_id))).json() == {"bands": []}
    assert (await client.get("/webapp/api/schedule")).json()["bookings"] == []


async def test_band_changes_need_a_fresh_signature(client, session, make_user, make_band):
    from tests.test_webapp_auth import init_data

    leader, _, band, _ = await with_member(session, make_user, make_band)
    stale = {"X-Telegram-Init-Data": init_data(leader.telegram_id, age=2 * 60 * 60)}
    assert (await client.get("/webapp/api/bands", headers=stale)).status_code == 200  # reading allows a day-old sign-in
    assert (await client.patch(f"/webapp/api/bands/{band.id}", json={"name": "Late"}, headers=stale)).status_code == 401
    assert (await client.delete(f"/webapp/api/bands/{band.id}", headers=stale)).status_code == 401


def test_every_string_the_front_end_asks_for_exists_in_all_locales():
    import json
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "app"
    used = set(re.findall(r'"(webapp\.[a-z_.]+)"', (root / "webapp" / "static" / "app.js").read_text(encoding="utf-8")))
    for language in ("ru", "en", "kz"):
        strings = json.loads((root / "locales" / f"{language}.json").read_text(encoding="utf-8"))
        assert sorted(key for key in used if key not in strings) == [], language
