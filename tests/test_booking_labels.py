from types import SimpleNamespace

from app.services.bookings import booking_owner_label


def _user(last_name=None, first_name=None, username=None, telegram_id=42):
    return SimpleNamespace(
        last_name=last_name, first_name=first_name, telegram_username=username, telegram_id=telegram_id
    )


def test_band_booking_is_labelled_with_the_band_name():
    booking = SimpleNamespace(band=SimpleNamespace(name="Nirvana"), purpose="band", created_by=_user("Иванов", "Иван"))
    assert booking_owner_label(booking) == "Nirvana"


def test_personal_booking_shows_the_author_with_fallbacks():
    full = SimpleNamespace(band=None, purpose="personal", created_by=_user("Иванов", "Иван", "ivan"))
    assert booking_owner_label(full) == "Личная репетиция | Иванов Иван"

    no_names = SimpleNamespace(band=None, purpose="personal", created_by=_user(username="ivan"))
    assert booking_owner_label(no_names) == "Личная репетиция | ivan"

    nothing = SimpleNamespace(band=None, purpose="personal", created_by=_user())
    assert booking_owner_label(nothing) == "Личная репетиция | 42"


def test_staff_booking_label():
    booking = SimpleNamespace(band=None, purpose="staff", created_by=_user("Иванов", "Иван"))
    assert booking_owner_label(booking) == "Стафф / кабинет"
