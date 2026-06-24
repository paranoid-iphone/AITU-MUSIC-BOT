from aiogram.fsm.state import State, StatesGroup


class Onboarding(StatesGroup):
    first_name = State()
    last_name = State()
    study_group = State()
    language = State()


class BandFlow(StatesGroup):
    create_name = State()
    create_role = State()
    join_code = State()
    join_role = State()


class ProfileFlow(StatesGroup):
    first_name = State()
    last_name = State()
    study_group = State()
    language = State()


class BookingFlow(StatesGroup):
    band_id = State()
    booking_date = State()
    slot = State()
    end_time = State()
    purpose = State()
    song_title = State()


class AdminFlow(StatesGroup):
    day_toggle = State()
    approve_user = State()
    reject_user = State()
    ban_user = State()
    delete_user = State()
    add_admin = State()
    weekly_booking_limit = State()
    daily_booking_limit = State()
    booking_window = State()
    staff_booking_date = State()
    staff_booking_time = State()
    staff_booking_title = State()
    event_title = State()
    event_date = State()
    event_location = State()
    event_deadline = State()
    event_edit_value = State()


class EventFlow(StatesGroup):
    event_id = State()
    band_id = State()
    song_title = State()
    members = State()
