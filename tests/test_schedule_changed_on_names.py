"""Names are printed on the schedule image, so changing one must queue `schedule_changed`."""
from app.services import bands, users


def _schedule_changed(published) -> int:
    return len([event for event in published if event["type"] == "schedule_changed"])


async def test_renaming_a_band_refreshes_the_schedule_only_when_the_name_changes(session, make_user, make_band, published):
    leader = await make_user()
    leader_tg = leader.telegram_id
    band_id = (await make_band(leader, "Rockers")).id
    published.clear()

    await bands.update_band_name(session, band_id, leader_tg, "Rockers")
    assert _schedule_changed(published) == 0

    await bands.update_band_name(session, band_id, leader_tg, "Metallers")
    assert _schedule_changed(published) == 1


async def test_changing_the_name_of_an_approved_member_refreshes_the_schedule(session, make_user, published):
    member = await make_user(first_name="Иван")
    member_tg = member.telegram_id
    published.clear()

    await users.update_profile(session, member_tg, first_name="Иван", study_group="SE-2402")
    assert _schedule_changed(published) == 0

    await users.update_profile(session, member_tg, first_name="Пётр")
    assert _schedule_changed(published) == 1


async def test_a_name_set_during_registration_does_not_refresh_the_schedule(session, make_user, published):
    pending = await make_user(status="pending", first_name=None)
    pending_tg = pending.telegram_id
    published.clear()

    await users.update_profile(session, pending_tg, first_name="Иван")
    assert _schedule_changed(published) == 0


async def test_a_new_username_of_an_approved_member_refreshes_the_schedule(session, make_user, published):
    member = await make_user(telegram_username="old_name")
    member_tg = member.telegram_id
    published.clear()

    await users.upsert_telegram_user(session, member_tg, "old_name", "Full Name")
    assert _schedule_changed(published) == 0

    await users.upsert_telegram_user(session, member_tg, "new_name", "Full Name")
    assert _schedule_changed(published) == 1


async def test_deleting_a_member_refreshes_the_schedule(session, make_user, published):
    admin = await make_user(role="admin", group_slug=None)
    victim = await make_user()
    admin_tg, victim_tg = admin.telegram_id, victim.telegram_id
    published.clear()

    await users.soft_delete_user(session, admin_tg, str(victim_tg), None)
    assert _schedule_changed(published) == 1
