from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from starlette.requests import Request

from report_logic import router as report_router, extract_jwt_user_info
from alarm_logic import router as alarm_router
from calculated_telemetry import router as calculated_router
from thingsboard_auth import get_admin_jwt
from alarm_aggregation_scheduler import scheduler, stop_scheduler
from config import TB_ACCOUNTS  # (kept; not required in this file but useful for future scoping)

import threading
import os
import requests
import logging

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
TB_HOST = os.getenv("TB_BASE_URL", "https://thingsboard.cloud").rstrip("/")


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


def _parse_bearer(auth_header: str) -> str:
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Bearer token")
    return auth_header.split(" ", 1)[1]


def _extract_user_info(jwt_token: str, account_id: str):
    """
    Backward-compatible wrapper: try 2-arg signature first,
    then fall back to 1-arg if the function is older.
    """
    try:
        return extract_jwt_user_info(jwt_token, account_id)  # type: ignore[arg-type]
    except TypeError:
        # old signature: extract_jwt_user_info(jwt_token)
        return extract_jwt_user_info(jwt_token)  # type: ignore[call-arg]


# === Device list for widgets ===
@app.get("/my_devices/")
def get_my_devices(
    authorization: str = Header(...),
    account_id: str = Header(default="account1", alias="X-Account-ID"),
):
    """
    Returns devices visible to the caller, using their JWT and authority.
    Supports multi-account by expecting X-Account-ID (defaults to 'account1').
    """
    jwt_token = _parse_bearer(authorization)

    user_info = _extract_user_info(jwt_token, account_id)
    authority = (user_info or {}).get("authority", "")
    customer_id = (user_info or {}).get("customerId", {}).get("id", "")

    headers = {"X-Authorization": f"Bearer {jwt_token}"}

    if authority == "CUSTOMER_USER":
        url = f"{TB_HOST}/api/customer/{customer_id}/deviceInfos?pageSize=1000&page=0"
    elif authority == "TENANT_ADMIN":
        url = f"{TB_HOST}/api/tenant/devices?pageSize=1000&page=0"
    else:
        raise HTTPException(status_code=403, detail=f"Unsupported authority: {authority}")

    resp = requests.get(url, headers=headers, timeout=20)
    if resp.status_code != 200:
        logger.error("ThingsBoard device list failed: %s %s", resp.status_code, resp.text[:300])
        raise HTTPException(status_code=resp.status_code, detail="Failed to fetch devices from ThingsBoard")

    data = resp.json()
    devices = data.get("data", data)  # TB returns {"data":[...]} for tenant; may be array in some versions
    out = []
    for d in devices:
        try:
            out.append({"name": d["name"], "id": d["id"]["id"]})
        except Exception:
            # Some TB endpoints return 'id' as plain string
            out.append({"name": d.get("name"), "id": (d.get("id", {}) or {}).get("id", d.get("id"))})
    return out


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
def admin_jwt_status(account_id: str = Header(default="account1", alias="X-Account-ID")):
    """
    Verifies we can log in with the configured admin credentials for a given account.
    Requires X-Account-ID (defaults to account1).
    """
    token = get_admin_jwt(account_id, TB_HOST)
    if token:
        return {"status": "success", "message": "Admin JWT retrieved successfully", "account": account_id}
    raise HTTPException(status_code=500, detail="Failed to retrieve Admin JWT")


@app.on_event("startup")
async def start_alarm_scheduler():
    logger.info("[Scheduler] Starting background scheduler thread...")
    thread = threading.Thread(target=scheduler, daemon=True)
    thread.start()


@app.on_event("shutdown")
async def shutdown_alarm_scheduler():
    logger.info("[Scheduler] Shutting down scheduler...")
    stop_scheduler()
