import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.database import engine
from app.routers.attendance import router as attendance_router
from app.routers.auth import router as auth_router
from app.routers.dashboard import router as dashboard_router
from app.routers.leave import router as leave_router
from app.routers.announcements import router as announcements_router
from app.routers.push_devices import router as push_devices_router
from app.routers.push_dispatch import router as push_dispatch_router


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


app = FastAPI(
    title=settings.app_name,
    description="API HRIS Kota Pelangi Group",
    version="1.2.0",
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
UPLOAD_DIRECTORY = PROJECT_ROOT / "uploads"
UPLOAD_DIRECTORY.mkdir(parents=True, exist_ok=True)

app.mount(
    "/uploads",
    StaticFiles(directory=UPLOAD_DIRECTORY),
    name="uploads",
)

app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(attendance_router)
app.include_router(leave_router)
app.include_router(announcements_router)
app.include_router(push_devices_router)
app.include_router(push_dispatch_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8081",
        "http://localhost:19006",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "status": True,
        "message": "HRIS API berjalan",
        "environment": settings.app_env,
    }


@app.get("/api/health")
def health_check():
    return {
        "status": True,
        "service": settings.app_name,
        "version": "1.2.0",
    }


@app.get("/api/health/database")
def database_health_check():
    try:
        with engine.connect() as connection:
            database_name = connection.execute(
                text("SELECT current_database()")
            ).scalar_one()
            database_version = connection.execute(
                text("SELECT version()")
            ).scalar_one()

        return {
            "status": True,
            "message": "Koneksi PostgreSQL berhasil",
            "database": database_name,
            "version": database_version,
        }
    except SQLAlchemyError:
        logger.exception("Koneksi PostgreSQL gagal")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Koneksi database gagal. Periksa log server.",
        )
