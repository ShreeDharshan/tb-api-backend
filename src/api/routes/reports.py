import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse

from src.models.report import ReportRequestModel
from src.services.report_service import generate_report_file, resolve_report_path

logger = logging.getLogger("api.reports")
router = APIRouter()


@router.post("/generate_report/")
def generate_report(
    body: ReportRequestModel,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_tb_account: Optional[str] = Header(None, alias="X-TB-Account"),
):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    try:
        return generate_report_file(body, x_tb_account)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/download/{filename}")
def download_report(filename: str):
    try:
        file_path = resolve_report_path(filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return FileResponse(
        file_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )
