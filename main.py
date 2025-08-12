from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from starlette.requests import Request

from report_logic import router as report_router
from alarm_logic import router as alarm_router
from calculated_telemetry import router as calculated_router

from thingsboard_auth import get_admin_jwt
from alarm_aggregation_scheduler import scheduler, stop_scheduler
from config import TB_ACCOUNTS

import threading
import os
import requests
import logging
import base64
import json

# === Logging config ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === FastAPI setup ===
app = FastAPI()

# === CORS for ThingsBoard dashboard ===
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_origin_regex="https?://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === API Routers ===
app.include_router(report_router)
app.include_router(alarm_router)
app.include_router(calculated_router)

# === Cloud ThingsBoard host ===
TB_HOST = os.getenv("TB_BASE_URL", "https://thingsboard.cloud")


# === Helpers ===
def _b64url_decode(payload_part: str) -> bytes:
    """Base64url decode with padding fix."""
    rem = len(payload_part) % 4
    if rem:
        payload_part += "=" * (4 - rem)
    return base64.urlsafe_b64decode(payload_part.encode("utf-8"))

def extract_jwt_user_info(jwt_token: str) -> dict:
    """
    Decode JWT payload without verification (we only need display fields like email/authority).
    """
    try:
        parts = jwt_token.split(".")
        if len(parts) < 2:
            return {}
        payload_raw = _b64url_decode(parts[1])
        return json.loads(payload_raw.decode("utf-8"))
    except Exception as e:
        logger.warning(f"[JWT] Failed to decode token payload: {e}")
        return {}


# === Global handler for FastAPI validation errors (422) ===
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.error(f"[VALIDATION ERROR] {exc}")
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder({
            "detail": exc.errors(),
            "body": exc.body
        }),
    )

# === Device list for widgets ===
@app.get("/my_devices/")
def get_my_devices(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=400, detail="Missing Bearer token")
    jwt_token = authorization.split(" ", 1)[1]

    user_info = extract_jwt_user_info(jwt_token)
    authority = user_info.get("authority", "")
    customer_id = (user_info.get("customerId") or {}).get("id", "")

    headers = {"X-Authorization": f"Bearer {jwt_token}"}
    if authority == "CUSTOMER_USER":
        url = f"{TB_HOST}/api/customer/{customer_id}/deviceInfos?pageSize=1000&page=0"
    elif authority == "TENANT_ADMIN":
        url = f"{TB_HOST}/api/tenant/devices?pageSize=1000&page=0"
    else:
        raise HTTPException(status_code=403, detail=f"Unsupported authority: {authority or 'UNKNOWN'}")

    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        devices = resp.json().get("data", [])
    except requests.RequestException as e:
        logger.error(f"[my_devices] TB request failed: {e}")
        raise HTTPException(status_code=502, detail="ThingsBoard upstream error")

    return [{"name": d["name"], "id": d["id"]["id"]} for d in devices]

# === XLS download endpoint ===
@app.get("/download/{filename}")
def download_csv(filename: str):
    file_path = os.path.join(os.getcwd(), filename)
    if os.path.exists(file_path):
        response = FileResponse(
            path=file_path,
            filename=filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response
    raise HTTPException(status_code=404, detail="File not found")

@app.get("/healthcheck")
def health_check():
    return {"status": "ok"}

@app.get("/admin_jwt_status")
def admin_jwt_status(account_id: str | None = None):
    # Allow optional account selection; default to the first configured account
    account = account_id or (list(TB_ACCOUNTS.keys())[0] if TB_ACCOUNTS else "ACCOUNT1")
    token = get_admin_jwt(account_id=account, base_url=TB_HOST)
    if token:
        return {"status": "success", "message": f"Admin JWT retrieved for {account}"}
    else:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve Admin JWT for {account}")

@app.on_event("startup")
async def start_alarm_scheduler():
    logger.info("[Scheduler] Starting background scheduler thread...")
    thread = threading.Thread(target=scheduler, daemon=True)
    thread.start()

@app.on_event("shutdown")
async def shutdown_alarm_scheduler():
    logger.info("[Scheduler] Shutting down scheduler...")
    stop_scheduler()
