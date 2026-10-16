from app.services.allocator import Request, Window, allocate

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
ROOM = (8 * 60, 21 * 60)


def h(hours: float) -> int:
    return int(hours * 60)


def win(priority, day, start, end):
    return Window(priority, day, h(start), h(end))


def req(id, band, duration, *windows, order=0):
    return Request(id=id, band_id=band, duration=h(duration), windows=tuple(windows), order=order)


def run(requests, busy=None, closed=None, daily_limit=0, band_day=None):
    return allocate(requests, busy or {}, *ROOM, closed_days=closed, daily_limit=daily_limit, band_day_minutes=band_day)


def slots(result):
    return {a.request_id: (a.day, a.start / 60, a.end / 60, a.priority) for a in result.assignments}


def test_example_from_the_club_dance_and_music_share_wednesday():
    dance = req(1, 10, 2, win(1, WED, 13, 15))
    music = req(2, 20, 3, win(1, WED, 14, 18))
    result = run([music, dance])
    assert slots(result) == {1: (WED, 13, 15, 1), 2: (WED, 15, 18, 1)}
    assert result.unassigned == []


def test_second_wish_is_used_when_first_is_taken():
    a = req(1, 10, 2, win(1, TUE, 18, 20), win(2, WED, 14, 18), order=0)
    b = req(2, 20, 2, win(1, TUE, 18, 20), win(2, SAT, 12, 16), order=1)
    result = run([a, b])
    assert slots(result) == {1: (TUE, 18, 20, 1), 2: (SAT, 12, 14, 2)}


def test_higher_priority_wish_of_one_beats_lower_priority_wish_of_another():
    # A cannot have Monday (busy); its second wish is Tuesday. B wants Tuesday first.
    a = req(1, 10, 1, win(1, MON, 10, 11), win(2, TUE, 10, 11), order=0)
    b = req(2, 20, 1, win(1, TUE, 10, 11), order=1)
    result = run([a, b], busy={MON: [(h(9), h(12))]})
    assert slots(result) == {2: (TUE, 10, 11, 1)}
    assert result.unassigned == [1]


def test_earliest_free_start_inside_the_window_is_used():
    result = run([req(1, 10, 1, win(1, WED, 12, 18))], busy={WED: [(h(12), h(14)), (h(15), h(16))]})
    assert slots(result) == {1: (WED, 14, 15, 1)}


def test_closed_day_and_outside_room_hours_are_never_used():
    closed = run([req(1, 10, 1, win(1, SUN, 10, 12))], closed={SUN})
    assert closed.assignments == [] and closed.unassigned == [1]
    # Window 06:00-09:00 is clamped to the room opening at 08:00; a 2h request cannot fit before 09:00.
    outside = run([req(1, 10, 2, win(1, MON, 6, 9))])
    assert outside.unassigned == [1]
    clamped = run([req(1, 10, 1, win(1, MON, 6, 9))])
    assert slots(clamped) == {1: (MON, 8, 9, 1)}


def test_a_band_that_already_got_a_slot_does_not_take_the_last_one():
    # Everything competes for Tue 10-12 (two 1h places). Band 10 has two requests, band 20 has one.
    requests = [
        req(1, 10, 1, win(1, TUE, 10, 12), order=0),
        req(2, 10, 1, win(1, TUE, 10, 12), order=1),
        req(3, 20, 1, win(1, TUE, 10, 12), order=2),
    ]
    result = run(requests)
    assert set(slots(result)) == {1, 3}
    assert result.unassigned == [2]


def test_tighter_window_goes_first():
    flexible = req(1, 10, 1, win(1, WED, 12, 18), order=0)
    tight = req(2, 20, 1, win(1, WED, 12, 13), order=1)
    result = run([flexible, tight])
    assert slots(result)[2] == (WED, 12, 13, 1)
    assert slots(result)[1] == (WED, 13, 14, 1)


def test_daily_limit_counts_existing_and_new_minutes():
    requests = [req(1, 10, 2, win(1, TUE, 10, 14)), req(2, 10, 2, win(1, TUE, 14, 18), order=1)]
    result = run(requests, daily_limit=h(3))
    assert set(slots(result)) == {1}
    already = run([req(1, 10, 2, win(1, TUE, 10, 14))], daily_limit=h(3), band_day={(10, TUE): h(2)})
    assert already.unassigned == [1]


def test_no_two_assignments_overlap_on_a_busy_week():
    requests = [req(i, i, 2, win(1, WED, 12, 20), win(2, THU, 12, 20), win(3, FRI, 12, 20), order=i) for i in range(1, 9)]
    result = run(requests)
    by_day: dict[int, list[tuple[int, int]]] = {}
    for a in result.assignments:
        by_day.setdefault(a.day, []).append((a.start, a.end))
    for intervals in by_day.values():
        intervals.sort()
        assert all(first[1] <= second[0] for first, second in zip(intervals, intervals[1:]))
    # 3 days * 8h of window / 2h = 12 places for 8 requests: everybody fits.
    assert result.unassigned == []


def test_input_is_not_mutated_and_result_is_deterministic():
    busy = {WED: [(h(10), h(11))]}
    requests = [req(1, 10, 1, win(1, WED, 10, 14)), req(2, 20, 1, win(1, WED, 10, 14), order=1)]
    first = run(requests, busy=busy)
    second = run(requests, busy=busy)
    assert first == second
    assert busy == {WED: [(h(10), h(11))]}
