from __future__ import annotations

from datetime import date, datetime, time, timedelta
from io import BytesIO
from pathlib import Path
from textwrap import wrap

from PIL import Image, ImageDraw, ImageFont


WIDTH = 1600
LEFT_GUTTER = 112
RIGHT_PAD = 36
TOP_PAD = 34
TITLE_H = 82
HEADER_H = 74
ROW_H = 48
START_HOUR = 8
END_HOUR = 21
DAYS = 7

BG = "#f5f7f2"
PANEL = "#ffffff"
GRID = "#dfe6d8"
TEXT = "#1f2a24"
MUTED = "#718174"
TIME_TEXT = "#68766a"
ACCENT = "#3f7f5f"
SELF = "#dbeedd"
CONCERT = "#dbe8ff"
STAFF = "#ffe8cc"
EMPTY = "#f8faf6"
PAST = "#ecefed"
PAST_LINE = "#d6ddd7"
PAST_TEXT = "#87938a"


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


FONT_TITLE = _font(34, True)
FONT_DAY = _font(22, True)
FONT_DATE = _font(18)
FONT_TIME = _font(17)
FONT_CARD_TITLE = _font(18, True)
FONT_CARD_BAND = _font(15, True)
FONT_CARD_META = _font(14)
FONT_SMALL = _font(13)


def _parse_time(value: str) -> time:
    return datetime.strptime(value[:5], "%H:%M").time()


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _round_rect(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], fill: str, outline: str | None = None, radius: int = 12) -> None:
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline)


def _fit_lines(text: str, max_chars: int, max_lines: int) -> list[str]:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return []
    lines = wrap(cleaned, width=max_chars) or [cleaned]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".") + "..."
    return lines


def _purpose_label(value: str) -> str:
    return {"concert": "к концерту", "self": "для себя", "staff": "стафф"}.get(value, value)


def _card_color(value: str) -> str:
    if value == "concert":
        return CONCERT
    if value == "staff":
        return STAFF
    return SELF


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _draw_pill(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, fill: str, text_fill: str = "#36503c") -> int:
    width = _text_width(draw, text, FONT_SMALL) + 16
    draw.rounded_rectangle((x, y, x + width, y + 22), radius=11, fill=fill)
    draw.text((x + 8, y + 4), text, fill=text_fill, font=FONT_SMALL)
    return width


def _slot_y(minutes: int, grid_top: int) -> int:
    return grid_top + ((minutes - START_HOUR * 60) * ROW_H) // 30


def _draw_today_past(draw: ImageDraw.ImageDraw, day_idx: int, col_w: int, grid_top: int, grid_h: int, now: datetime) -> None:
    current_minutes = now.hour * 60 + now.minute
    start_minutes = START_HOUR * 60
    end_minutes = END_HOUR * 60
    if current_minutes <= start_minutes:
        return
    cutoff = min(current_minutes, end_minutes)
    if cutoff <= start_minutes:
        return
    x1 = LEFT_GUTTER + day_idx * col_w + 1
    x2 = LEFT_GUTTER + (day_idx + 1) * col_w - 1
    y1 = grid_top + 1
    y2 = min(grid_top + grid_h - 1, _slot_y(cutoff, grid_top))
    draw.rectangle((x1, y1, x2, y2), fill=PAST)
    for y in range(y1 - col_w, y2, 18):
        draw.line((x1, y + col_w, x2, y), fill=PAST_LINE, width=1)
    if y2 - y1 > 38:
        draw.text((x1 + 18, y1 + 16), "уже прошло", fill=PAST_TEXT, font=FONT_SMALL)


def _week_days(today: date) -> list[date]:
    return [today + timedelta(days=offset) for offset in range(DAYS)]


def render_schedule_image(bookings: list[dict], generated_at: datetime | None = None) -> bytes:
    now = generated_at or datetime.now()
    days = _week_days(now.date())
    grid_top = TOP_PAD + TITLE_H + HEADER_H
    grid_h = ((END_HOUR - START_HOUR) * 2) * ROW_H
    height = grid_top + grid_h + 40
    col_w = (WIDTH - LEFT_GUTTER - RIGHT_PAD) // DAYS

    image = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(image)

    _round_rect(draw, (24, 20, WIDTH - 24, height - 20), PANEL, radius=24)
    draw.text((LEFT_GUTTER, TOP_PAD), "AITU Music Club", fill=TEXT, font=FONT_TITLE)
    draw.text((LEFT_GUTTER, TOP_PAD + 42), "Расписание кабинета на ближайшие 7 дней", fill=MUTED, font=FONT_DATE)
    draw.text((WIDTH - 320, TOP_PAD + 14), f"обновлено {now.strftime('%d.%m %H:%M')}", fill=MUTED, font=FONT_DATE)
    legend_x = WIDTH - 520
    legend_y = TOP_PAD + 48
    for label, color in (("для себя", SELF), ("к концерту", CONCERT), ("стафф", STAFF)):
        legend_w = _draw_pill(draw, legend_x, legend_y, label, color)
        legend_x += legend_w + 8

    weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    for idx, day in enumerate(days):
        x = LEFT_GUTTER + idx * col_w
        draw.rectangle((x, TOP_PAD + TITLE_H, x + col_w, grid_top), fill="#edf4ea")
        draw.text((x + 18, TOP_PAD + TITLE_H + 14), weekdays[day.weekday()], fill=ACCENT, font=FONT_DAY)
        draw.text((x + 18, TOP_PAD + TITLE_H + 42), day.strftime("%d.%m"), fill=MUTED, font=FONT_DATE)

    for slot in range((END_HOUR - START_HOUR) * 2 + 1):
        y = grid_top + slot * ROW_H
        minutes = START_HOUR * 60 + slot * 30
        line_fill = GRID if slot % 2 == 0 else "#edf1e9"
        draw.line((LEFT_GUTTER, y, WIDTH - RIGHT_PAD, y), fill=line_fill, width=1)
        if slot < (END_HOUR - START_HOUR) * 2:
            label = f"{minutes // 60:02d}:{minutes % 60:02d}"
            draw.text((42, y + 10), label, fill=TIME_TEXT, font=FONT_TIME)

    for idx in range(DAYS + 1):
        x = LEFT_GUTTER + idx * col_w
        draw.line((x, TOP_PAD + TITLE_H, x, grid_top + grid_h), fill=GRID, width=1)

    draw.rectangle((LEFT_GUTTER, grid_top, WIDTH - RIGHT_PAD, grid_top + grid_h), outline=GRID, width=2)

    for day_idx, day in enumerate(days):
        if day == now.date():
            _draw_today_past(draw, day_idx, col_w, grid_top, grid_h, now)

    bookings_by_day: dict[str, list[dict]] = {day.isoformat(): [] for day in days}
    for booking in bookings:
        if booking.get("booking_date") in bookings_by_day:
            bookings_by_day[booking["booking_date"]].append(booking)

    for day_idx, day in enumerate(days):
        day_rows = bookings_by_day[day.isoformat()]
        if not day_rows:
            x = LEFT_GUTTER + day_idx * col_w
            label_y = grid_top + 16
            if day == now.date():
                current_minutes = now.hour * 60 + now.minute
                if current_minutes >= END_HOUR * 60:
                    continue
                if current_minutes > START_HOUR * 60:
                    label_y = _slot_y(current_minutes, grid_top) + 16
            draw.text((x + 22, label_y), "свободно", fill="#a5b0a6", font=FONT_SMALL)
            continue
        for booking in day_rows:
            start = _minutes(_parse_time(booking["start_time"]))
            end = _minutes(_parse_time(booking["end_time"]))
            top = grid_top + max(0, start - START_HOUR * 60) // 30 * ROW_H + 4
            bottom = grid_top + max(0, end - START_HOUR * 60) // 30 * ROW_H - 4
            if bottom <= grid_top or top >= grid_top + grid_h:
                continue
            top = max(grid_top + 4, top)
            bottom = min(grid_top + grid_h - 4, bottom)
            x1 = LEFT_GUTTER + day_idx * col_w + 8
            x2 = LEFT_GUTTER + (day_idx + 1) * col_w - 8
            _round_rect(draw, (x1, top, x2, bottom), _card_color(booking.get("purpose", "self")), "#c7d2c6", radius=10)
            draw.rectangle((x1, top + 8, x1 + 5, bottom - 8), fill=ACCENT)

            purpose = booking.get("purpose", "self")
            title = booking.get("song_title") or "песня не указана"
            meta = f"{booking['start_time'][:5]}-{booking['end_time'][:5]}"
            band = booking.get("band_name") or ""
            created_by = booking.get("created_by") or ""
            card_h = bottom - top
            y = top + 9
            max_lines = 2 if card_h >= 92 else 1
            for line in _fit_lines(title, 17, max_lines):
                draw.text((x1 + 16, y), line, fill=TEXT, font=FONT_CARD_TITLE)
                y += 22
            if bottom - y > 18 and band:
                for line in _fit_lines(band, 20, 1):
                    draw.text((x1 + 16, y), line, fill="#314b39", font=FONT_CARD_BAND)
                    y += 19
            if bottom - y > 20:
                pill_x = x1 + 16
                pill_x += _draw_pill(draw, pill_x, y, meta, "#f8fbf4") + 6
                if pill_x + 72 < x2:
                    _draw_pill(draw, pill_x, y, _purpose_label(purpose), "#f8fbf4")
                y += 24
            if card_h >= 132 and bottom - y > 16 and created_by:
                for line in _fit_lines(f"создал: {created_by}", 22, 1):
                    draw.text((x1 + 16, y), line, fill=MUTED, font=FONT_SMALL)

    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
