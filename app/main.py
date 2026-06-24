from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import admin, bands, bookings, events, users
from app.core.config import settings
from app.db.session import create_db_and_tables
from app.services.bookings import seed_default_schedule
from app.db.session import AsyncSessionLocal
from app.services.errors import AppError

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


app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(bands.router, prefix="/bands", tags=["bands"])
app.include_router(bookings.router, prefix="/bookings", tags=["bookings"])
app.include_router(events.router, prefix="/events", tags=["events"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])
