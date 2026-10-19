import asyncio
import logging
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import admin, bands, bookings, events, groups, history, move_requests, planning, users, waitlist, webapp, webapp_admin
from app.core.config import settings
from app.db.session import prepare_database
from app.services.bookings import seed_default_schedule
from app.db.session import AsyncSessionLocal
from app.services import planning as planning_service
from app.services import move_requests as move_request_service
from app.services import waitlist as waitlist_service
from app.services import outbox, repeat_offers, sheets_sync
from app.services.errors import AppError
from app.services.weeks import allocation_time, next_week_start
from app.core.timezone import now_local

app = FastAPI(title=settings.app_name)
logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone=settings.timezone)
sheets_task: asyncio.Task | None = None
REPEAT_OFFER_JOB_MINUTES = 5
OUTBOX_JOB_SECONDS = 30


async def run_sunday_allocation() -> None:
    """Close requests and allocate the room for the coming week."""
    async with AsyncSessionLocal() as session:
        try:
            summary = await planning_service.run_weekly_allocation(session, None, next_week_start(now_local().date()))
            logger.info("Weekly allocation done: %s assigned, %s unassigned", len(summary["assigned"]), len(summary["unassigned"]))
        except AppError as exc:
            logger.info("Weekly allocation skipped: %s", exc)
        except Exception:
            logger.exception("Weekly allocation failed")


async def run_repeat_offers() -> None:
    """Offer to repeat rehearsals that have just started (their slot a week later is now bookable)."""
    async with AsyncSessionLocal() as session:
        try:
            offered = await repeat_offers.offer_repeats(session)
            if offered:
                logger.info("Queued %s repeat offer(s)", offered)
        except Exception:
            logger.exception("Repeat offers failed")


async def run_expire_move_requests() -> None:
    """Close "please move" requests that timed out or whose booking is gone."""
    async with AsyncSessionLocal() as session:
        try:
            expired = await move_request_service.expire_stale(session)
            if expired:
                logger.info("Expired %s move request(s)", expired)
        except Exception:
            logger.exception("Expiring move requests failed")


async def run_cleanup_waitlist() -> None:
    """Drop waiting-list entries whose time has started."""
    async with AsyncSessionLocal() as session:
        try:
            await waitlist_service.cleanup_past(session)
        except Exception:
            logger.exception("Cleaning the waiting list failed")


async def run_outbox_retry() -> None:
    """Push bot events that missed Redis and forget the ones published long ago."""
    async with AsyncSessionLocal() as session:
        try:
            resent = await outbox.republish_unpublished(session)
            if resent:
                logger.info("Republished %s outbox event(s)", resent)
            await outbox.purge_published(session)
        except Exception:
            logger.exception("Outbox retry failed")


async def catch_up_missed_allocations() -> None:
    async with AsyncSessionLocal() as session:
        try:
            done = await planning_service.catch_up_allocations(session)
            if done:
                logger.info("Caught up missed allocations for weeks: %s", [week.isoformat() for week in done])
        except Exception:
            logger.exception("Catch-up allocation failed")


@app.on_event("startup")
async def on_startup() -> None:
    await prepare_database()
    async with AsyncSessionLocal() as session:
        await seed_default_schedule(session)
    scheduler.add_job(run_repeat_offers, "interval", minutes=REPEAT_OFFER_JOB_MINUTES, id="repeat_offers")
    scheduler.add_job(run_expire_move_requests, "interval", minutes=REPEAT_OFFER_JOB_MINUTES, id="expire_move_requests")
    scheduler.add_job(run_cleanup_waitlist, "interval", minutes=REPEAT_OFFER_JOB_MINUTES, id="cleanup_waitlist")
    scheduler.add_job(run_outbox_retry, "interval", seconds=OUTBOX_JOB_SECONDS, id="outbox_retry")
    if settings.weekly_planning_enabled:
        run_at = allocation_time()
        scheduler.add_job(run_sunday_allocation, "cron", day_of_week="sun", hour=run_at.hour, minute=run_at.minute, id="weekly_allocation")
    scheduler.start()
    if settings.weekly_planning_enabled:
        await catch_up_missed_allocations()
    global sheets_task
    if sheets_sync.is_enabled():
        sheets_task = asyncio.create_task(sheets_sync.sync_worker(AsyncSessionLocal))
        sheets_sync.request_sync()  # push the current state right after (re)start


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
    if sheets_task is not None:
        sheets_task.cancel()


@app.middleware("http")
async def restrict_tunnel_access(request: Request, call_next):
    # ngrok adds X-Forwarded-Host; the bot calls the backend directly without it.
    # Public tunnel traffic may only reach the schedule mini-app and the health check.
    if "x-forwarded-host" in request.headers and not (
        request.url.path.startswith("/webapp/") or request.url.path == "/health"
    ):
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    return await call_next(request)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


WEBAPP_DIR = Path(__file__).resolve().parent / "webapp" / "static"


class RevalidatingStaticFiles(StaticFiles):
    """Telegram's webview caches aggressively; make it revalidate (ETag) so a deploy shows up immediately."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


@app.get("/webapp/", include_in_schema=False)
@app.get("/webapp/schedule", include_in_schema=False)
async def webapp_index() -> FileResponse:
    return FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/webapp/static", RevalidatingStaticFiles(directory=WEBAPP_DIR), name="webapp-static")


app.include_router(webapp.router)
app.include_router(webapp_admin.router)
app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(groups.router, prefix="/groups", tags=["groups"])
app.include_router(history.router, prefix="/history", tags=["history"])
app.include_router(planning.router, prefix="/planning", tags=["planning"])
app.include_router(bands.router, prefix="/bands", tags=["bands"])
app.include_router(bookings.router, prefix="/bookings", tags=["bookings"])
app.include_router(move_requests.router, prefix="/move-requests", tags=["move-requests"])
app.include_router(waitlist.router, prefix="/waitlist", tags=["waitlist"])
app.include_router(events.router, prefix="/events", tags=["events"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])
