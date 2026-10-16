from app.bot.handlers_planning import _duration_label, _result_text, _windows_text


def test_duration_label():
    assert _duration_label(60) == "1 ч"
    assert _duration_label(90) == "1 ч 30 мин"


def test_windows_text_numbers_priorities_in_order():
    text = _windows_text(
        [
            {"day_of_week": 1, "start_time": "18:00:00", "end_time": "21:00:00"},
            {"day_of_week": 2, "start_time": "14:00:00", "end_time": "18:00:00"},
        ]
    )
    assert text.splitlines() == ["  1) Вт 18:00–21:00", "  2) Ср 14:00–18:00"]


def test_result_text_lists_assigned_and_unassigned():
    summary = {
        "assigned": [{"label": "Ср 21.10 13:00–15:00", "band_name": "Dancers", "priority": 1}],
        "unassigned": [{"band_name": "Musicians", "duration_minutes": 180}],
    }
    text = _result_text(summary, "Итог")
    assert "- Ср 21.10 13:00–15:00 · Dancers (приоритет 1)" in text
    assert "- Musicians (3 ч)" in text
    assert "Заявок нет" in _result_text({"assigned": [], "unassigned": []}, "Итог")
