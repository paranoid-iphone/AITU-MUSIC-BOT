"""Weekly room allocation: a pure function with no database access.

Several bands share one room. Each submits requests ("I need 2h, preferably Tue 18-21, otherwise Wed 14-18,
otherwise Sat 12-16"). The allocator hands out non-overlapping slots.

Strategy
- Priorities are processed in rounds: first everybody's priority-1 window, then priority-2 for those still
  without a slot, and so on. A request is therefore never given a lower-priority window while its
  higher-priority wish could have been honoured.
- Within a round, requests with the least room to manoeuvre go first (window length minus duration), then the
  band that received the fewest minutes this week, then the earliest submission.
- A request gets the earliest free start inside its window (30-minute steps).
All times are minutes from midnight; days are weekday numbers (Mon=0).
"""
from dataclasses import dataclass, field

STEP = 30


@dataclass(frozen=True)
class Window:
    priority: int
    day: int
    start: int
    end: int


@dataclass(frozen=True)
class Request:
    id: int
    band_id: int
    duration: int
    windows: tuple[Window, ...]
    order: int = 0  # submission order, lower is earlier


@dataclass(frozen=True)
class Assignment:
    request_id: int
    day: int
    start: int
    end: int
    priority: int


@dataclass
class AllocationResult:
    assignments: list[Assignment] = field(default_factory=list)
    unassigned: list[int] = field(default_factory=list)


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _fits(busy: list[tuple[int, int]], start: int, end: int) -> bool:
    return all(end <= b_start or start >= b_end for b_start, b_end in busy)


def _earliest_start(busy: list[tuple[int, int]], window: Window, duration: int, room_start: int, room_end: int) -> int | None:
    lo = max(window.start, room_start)
    hi = min(window.end, room_end)
    if lo % STEP:
        lo += STEP - lo % STEP
    start = lo
    while start + duration <= hi:
        if _fits(busy, start, start + duration):
            return start
        start += STEP
    return None


def allocate(
    requests: list[Request],
    busy: dict[int, list[tuple[int, int]]],
    room_start: int,
    room_end: int,
    closed_days: set[int] | None = None,
    daily_limit: int = 0,
    band_day_minutes: dict[tuple[int, int], int] | None = None,
) -> AllocationResult:
    """Distribute the room.

    busy: already occupied intervals per day (existing bookings, blocked past time).
    closed_days: weekdays on which the room cannot be used at all.
    daily_limit: max minutes per band per day (0 = unlimited).
    band_day_minutes: minutes a band already holds on a day (existing bookings), for the daily limit.
    """
    closed = closed_days or set()
    occupied = {day: _merge(list(intervals)) for day, intervals in busy.items()}
    band_day = dict(band_day_minutes or {})
    band_week: dict[int, int] = {}
    assigned: dict[int, Assignment] = {}

    max_priority = max((w.priority for r in requests for w in r.windows), default=0)
    for priority in range(1, max_priority + 1):
        round_items: list[tuple[Request, Window]] = []
        for request in requests:
            if request.id in assigned:
                continue
            window = next((w for w in request.windows if w.priority == priority), None)
            if window is not None:
                round_items.append((request, window))
        while round_items:
            # Tightest windows first, then the band served least so far this week (re-evaluated after every
            # placement, so one band cannot take everything), then first come first served.
            round_items.sort(
                key=lambda item: (
                    (item[1].end - item[1].start) - item[0].duration,
                    band_week.get(item[0].band_id, 0),
                    item[0].order,
                    item[0].id,
                )
            )
            request, window = round_items.pop(0)
            if window.day in closed:
                continue
            if daily_limit > 0 and band_day.get((request.band_id, window.day), 0) + request.duration > daily_limit:
                continue
            day_busy = occupied.setdefault(window.day, [])
            start = _earliest_start(day_busy, window, request.duration, room_start, room_end)
            if start is None:
                continue
            end = start + request.duration
            occupied[window.day] = _merge(day_busy + [(start, end)])
            band_day[(request.band_id, window.day)] = band_day.get((request.band_id, window.day), 0) + request.duration
            band_week[request.band_id] = band_week.get(request.band_id, 0) + request.duration
            assigned[request.id] = Assignment(request.id, window.day, start, end, priority)

    result = AllocationResult()
    result.assignments = sorted(assigned.values(), key=lambda a: (a.day, a.start, a.request_id))
    result.unassigned = [r.id for r in requests if r.id not in assigned]
    return result
