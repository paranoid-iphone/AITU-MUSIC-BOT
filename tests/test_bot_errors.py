from types import SimpleNamespace

import httpx
import pytest

from app.bot.api_client import BackendClient, BackendUnavailable
from app.bot.errors import on_error
from app.bot.i18n import load_locale, t

ERROR_KEYS = ("error_backend_unavailable", "error_failed", "error_generic")


def client_with(handler) -> BackendClient:
    return BackendClient(transport=httpx.MockTransport(handler))


async def test_connection_error_becomes_backend_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(BackendUnavailable) as caught:
        await client_with(handler).request("GET", "/health")
    assert str(caught.value) == t("ru", "error_backend_unavailable")


async def test_timeout_becomes_backend_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(BackendUnavailable):
        await client_with(handler).request("GET", "/health")


async def test_server_error_hides_the_raw_detail():
    client = client_with(lambda request: httpx.Response(503, json={"detail": "Internal Server Error"}))
    with pytest.raises(BackendUnavailable) as caught:
        await client.request("GET", "/health")
    assert "Internal Server Error" not in str(caught.value)


async def test_client_error_keeps_its_detail_and_is_not_unavailable():
    client = client_with(lambda request: httpx.Response(400, json={"detail": "Slot is taken"}))
    with pytest.raises(RuntimeError) as caught:
        await client.request("POST", "/bookings")
    assert not isinstance(caught.value, BackendUnavailable)
    assert str(caught.value) == "Slot is taken"


async def test_empty_and_json_answers_still_work():
    assert await client_with(lambda request: httpx.Response(204)).request("DELETE", "/x") is None
    assert await client_with(lambda request: httpx.Response(200, json={"ok": True})).request("GET", "/x") == {"ok": True}


def make_event(exception: Exception, *, callback=None, message=None):
    update = SimpleNamespace(update_id=7, callback_query=callback, message=message)
    return SimpleNamespace(update=update, exception=exception)


class Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail = fail

    async def answer(self, text=None, show_alert=False):
        self.calls.append((text, show_alert))
        if self.fail:
            raise RuntimeError("message deleted")


async def test_callback_gets_an_alert_when_the_backend_is_down():
    callback = Recorder()
    handled = await on_error(make_event(BackendUnavailable(t("ru", "error_backend_unavailable")), callback=callback))
    assert handled is True
    assert callback.calls == [(t("ru", "error_backend_unavailable"), True)]


async def test_message_gets_the_backend_detail():
    message = Recorder()
    await on_error(make_event(RuntimeError("Slot is taken"), message=message))
    assert message.calls == [(t("ru", "error_failed").format(detail="Slot is taken"), False)]


async def test_unexpected_exception_does_not_leak_details():
    message = Recorder()
    await on_error(make_event(ValueError("secret internals"), message=message))
    assert message.calls == [(t("ru", "error_generic"), False)]


async def test_failure_to_answer_is_swallowed():
    assert await on_error(make_event(ValueError("boom"), message=Recorder(fail=True))) is True


async def test_update_without_a_user_is_only_logged():
    assert await on_error(make_event(ValueError("boom"))) is True


@pytest.mark.parametrize("language", ["ru", "en", "kz"])
def test_error_texts_exist_in_every_locale(language):
    locale = load_locale(language)
    for key in ERROR_KEYS:
        assert locale.get(key), (language, key)
    assert "{detail}" in locale["error_failed"]
