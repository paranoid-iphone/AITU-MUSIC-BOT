from aiogram.fsm.state import State, StatesGroup


class Onboarding(StatesGroup):
    first_name = State()
    last_name = State()
    study_group = State()
    group = State()
    language = State()


class ProfileFlow(StatesGroup):
    first_name = State()
    last_name = State()
    study_group = State()
    language = State()
    personal_event_title = State()
    personal_event_date = State()
    personal_event_location = State()
    personal_event_role = State()
    personal_event_edit_value = State()


class AdminFlow(StatesGroup):
    event_title = State()
    event_date = State()
    event_location = State()
    event_deadline = State()
    event_edit_value = State()


class PlanFlow(StatesGroup):
    band = State()
    duration = State()
    day = State()
    time = State()
    more = State()
    purpose = State()
    song = State()


class EventFlow(StatesGroup):
    event_id = State()
    band_id = State()
    song_title = State()
    members = State()
    application_song_title = State()
    application_members = State()
