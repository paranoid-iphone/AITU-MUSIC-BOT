from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.api.routes import admin, bands, bookings, events, users
from app.core.config import settings
from app.db.session import create_db_and_tables
from app.services.bookings import seed_default_schedule
from app.db.session import AsyncSessionLocal
from app.services.errors import AppError
from app.services import bookings as booking_service

app = FastAPI(title=settings.app_name)


@app.on_event("startup")
async def on_startup() -> None:
    await create_db_and_tables()
    async with AsyncSessionLocal() as session:
        await seed_default_schedule(session)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/webapp/schedule", response_class=HTMLResponse)
async def schedule_webapp() -> str:
    return """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AITU Music Club Schedule</title>
  <style>
    :root { color-scheme: light; --green:#2f7d4f; --soft:#eef7ef; --line:#d9e8da; --busy:#d9eadb; --staff:#dce7ff; --text:#172018; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: Inter, system-ui, -apple-system, Segoe UI, sans-serif; color:var(--text); background:#f7faf7; }
    header { position:sticky; top:0; z-index:3; padding:14px 14px 10px; background:rgba(247,250,247,.94); backdrop-filter: blur(10px); border-bottom:1px solid var(--line); }
    h1 { margin:0; font-size:20px; line-height:1.15; }
    .meta { margin-top:4px; color:#68766b; font-size:12px; }
    .wrap { padding:12px; overflow:auto; }
    table { width:100%; min-width:880px; border-collapse:separate; border-spacing:0; background:white; border:1px solid var(--line); border-radius:12px; overflow:hidden; }
    th, td { border-right:1px solid var(--line); border-bottom:1px solid var(--line); vertical-align:top; }
    th:last-child, td:last-child { border-right:0; }
    tr:last-child td { border-bottom:0; }
    th { position:sticky; top:58px; z-index:2; background:var(--soft); padding:10px 8px; font-size:13px; text-align:left; color:#315a3c; }
    .time { width:72px; background:#fbfdfb; color:#7a887d; font-size:12px; padding:8px; white-space:nowrap; }
    td { height:54px; padding:4px; min-width:116px; }
    .event { border-left:4px solid var(--green); background:var(--busy); border-radius:7px; padding:6px 7px; min-height:44px; box-shadow:0 1px 0 rgba(0,0,0,.04); }
    .event.staff { background:var(--staff); border-left-color:#4a6fd6; }
    .song { font-weight:750; font-size:13px; margin-bottom:2px; }
    .band, .purpose { font-size:11px; color:#435247; line-height:1.25; }
    .empty { color:#b2beb4; font-size:11px; padding:6px; }
  </style>
</head>
<body>
  <header>
    <h1>AITU Music Club</h1>
    <div class="meta" id="meta">Расписание на ближайшие 7 дней</div>
  </header>
  <div class="wrap"><table id="schedule"></table></div>
  <script>
    const hours = Array.from({length: 27}, (_, i) => {
      const total = 8 * 60 + i * 30;
      return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
    });
    const dayNames = ["Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"];
    const purposeMap = { concert: "к концерту", self: "для себя", staff: "ивент / стафф" };
    function nextDays() {
      const now = new Date();
      return Array.from({length: 7}, (_, i) => {
        const d = new Date(now);
        d.setDate(now.getDate() + i);
        return d;
      });
    }
    function isoDate(d) {
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    }
    async function load() {
      const res = await fetch("/webapp/schedule-data", { cache: "no-store" });
      const bookings = await res.json();
      const days = nextDays();
      const table = document.getElementById("schedule");
      table.innerHTML = "";
      const head = document.createElement("tr");
      head.innerHTML = "<th>Время</th>" + days.map(d => `<th>${dayNames[d.getDay()]}<br>${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth()+1).padStart(2, "0")}</th>`).join("");
      table.appendChild(head);
      for (const time of hours) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td class="time">${time}</td>`;
        for (const day of days) {
          const date = isoDate(day);
          const cell = document.createElement("td");
          const item = bookings.find(b => b.booking_date === date && b.start_time.slice(0,5) === time);
          if (item) {
            const div = document.createElement("div");
            div.className = `event ${item.purpose === "staff" ? "staff" : ""}`;
            div.innerHTML = `<div class="song">${item.song_title || "песня не указана"}</div><div class="band">${item.band_name}</div><div class="purpose">${item.start_time.slice(0,5)}-${item.end_time.slice(0,5)} · ${purposeMap[item.purpose] || item.purpose}</div>`;
            cell.appendChild(div);
          } else {
            cell.innerHTML = '<span class="empty">свободно</span>';
          }
          tr.appendChild(cell);
        }
        table.appendChild(tr);
      }
      document.getElementById("meta").textContent = `Расписание на ближайшие 7 дней · обновлено ${new Date().toLocaleString("ru-RU")}`;
    }
    load();
    setInterval(load, 30000);
  </script>
</body>
</html>
    """


@app.get("/webapp/schedule-data")
async def schedule_webapp_data() -> list[dict]:
    async with AsyncSessionLocal() as session:
        rows = await booking_service.list_schedule(session)
    return [
        {
            **item,
            "booking_date": item["booking_date"].isoformat(),
            "start_time": item["start_time"].isoformat(),
            "end_time": item["end_time"].isoformat(),
        }
        for item in rows
    ]


app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(bands.router, prefix="/bands", tags=["bands"])
app.include_router(bookings.router, prefix="/bookings", tags=["bookings"])
app.include_router(events.router, prefix="/events", tags=["events"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])
