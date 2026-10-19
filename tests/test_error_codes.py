"""Error responses carry a stable code that the Mini App translates; the English message stays for the bot and logs."""
from pathlib import Path

import pytest

from app.bot.i18n import load_locale
from app.services import errors
from app.services.errors import ERROR_CODES, describe_error
from tests.test_webapp import client, tg  # noqa: F401  (`client` is a fixture)

APP = Path(__file__).resolve().parent.parent / "app"


def test_known_messages_get_their_code():
    assert describe_error("Slot is not available") == {"detail": "Slot is not available", "code": "slot_unavailable"}
    # several messages may share one code
    assert describe_error("Band booking cannot use personal purpose")["code"] == "purpose_mismatch"
    assert describe_error("Personal booking must use personal purpose")["code"] == "purpose_mismatch"


def test_messages_with_numbers_pass_them_on_as_params():
    assert describe_error("Weekly booking limit exceeded: 3.5h per band") == {
        "detail": "Weekly booking limit exceeded: 3.5h per band",
        "code": "weekly_limit",
        "params": {"n": "3.5"},
    }
    assert describe_error("Daily booking limit exceeded: 2h per band")["params"] == {"n": "2"}
    assert describe_error("Too many future bookings: at most 3 per band")["code"] == "future_limit"
    assert describe_error("Date must be within the next 14 days")["params"] == {"n": "14"}
    assert describe_error("At most 3 open move requests at a time")["code"] == "too_many_requests"
    assert describe_error("At most 5 waiting-list entries at a time")["code"] == "too_many_entries"


def test_unknown_messages_stay_plain():
    assert describe_error("Something nobody listed") == {"detail": "Something nobody listed"}


@pytest.mark.parametrize("language", ["ru", "en", "kz"])
def test_every_code_is_translated_and_numbers_keep_their_placeholder(language):
    strings = load_locale(language)
    numbered = {code for code, _ in errors._NUMBERED_MESSAGES}
    for code in ERROR_CODES:
        text = strings.get(f"webapp.error.code.{code}")
        assert text, f"{language}: no translation for {code}"
        assert ("{n}" in text) == (code in numbered), f"{language}: {code}"


def test_every_listed_message_is_still_raised_somewhere():
    """A reworded message would silently lose its translation; this catches the drift."""
    source = "\n".join(path.read_text(encoding="utf-8") for path in APP.rglob("*.py") if path.name != "errors.py")
    missing = [message for _, message in errors._FIXED_MESSAGES if f'"{message}"' not in source]
    assert missing == []


async def test_the_api_returns_the_code_next_to_the_message(client, make_user):
    user = await make_user()

    response = await client.post("/webapp/api/bands/join", json={"invite_code": "NOPE1234"}, headers=tg(user.telegram_id))

    assert response.status_code == 404
    assert response.json() == {"detail": "Invite code not found", "code": "invite_not_found"}
