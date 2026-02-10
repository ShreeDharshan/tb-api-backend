from fastapi import APIRouter, HTTPException

from src.config import get_settings

router = APIRouter()


@router.get("/")
def root() -> None:
    raise HTTPException(status_code=404, detail="Nothing to see here.")


@router.get("/healthz")
def healthz() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.app_version,
    }
