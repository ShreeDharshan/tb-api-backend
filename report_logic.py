from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
from typing import Optional, Dict, Any
import base64
import json
import os
import logging
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta

from thingsboard_auth import get_admin_jwt
from config import TB_ACCOUNTS

router = APIRouter()
logger = logging.getLogger("report_logic")

TB_HOST = os.getenv("TB_BASE_URL", "https://thingsboard.cloud")

# -----------------------
# Utilities
# -----------------------

def _b64url_decode(part: str) -> bytes:
    rem = len(part) % 4
    if rem:
        part += "=" * (4 - rem)
    return base64.urlsafe_b64decode(part.encode("utf-8"))

def _decode_jwt_payload(jwt_token: str) -> Dict[str, Any]:
    try:
        parts = jwt_token.split(".")
        if len(parts) < 2:
            return {}
        payload_raw = _b64url_decode(parts[1])
        return json.loads(payload_raw.decode("utf-8"))
    except Exception as e:
        logger.warning(f"[JWT] Failed to decode user payload: {e}")
        return {}

def _infer_account_id_from_email(email: str | None) -> Optional[str]:
    """
    Try to map user email/domain to an account key from TB_ACCOUNTS.
    Assumes TB_ACCOUNTS keys are like 'ACCOUNT1', 'ACCOUNT2', etc.
    You can extend mapping here if you maintain domain->account routing.
    """
    if not email:
        return None
    domain = email.split("@")[-1].lower()
    # Example rule: first configured account is the default
    if TB_ACCOUNTS:
        return list(TB_ACCOUNTS.keys())[0]
    return None

def _pick_account_id(jwt_payload: Dict[str, Any], explicit_header: Optional[str]) -> str:
    # 1) Explicit header wins
    if explicit_header:
        return explicit_header

    # 2) Try infer from email/domain
    email = jwt_payload.get("email")
    inferred = _infer_account_id_from_email(email)
    if inferred:
        return inferred

    # 3) Fallback to first configured account or ACCOUNT1
    return list(TB_ACCOUNTS.keys())[0] if TB_ACCOUNTS else "ACCOUNT1"


# -----------------------
# Endpoints
# -----------------------

@router.post("/generate_report/")
def generate_report(
    request: Request,
    authorization: str = Header(...),
    x_account_id: Optional[str] = Header(None),
):
    """
    Generates the Excel report.
    - `X-Account-ID` header is now OPTIONAL. If missing, we infer from JWT or fallback.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=400, detail="Missing Bearer token")
    jwt_token = authorization.split(" ", 1)[1]

    jwt_payload = _decode_jwt_payload(jwt_token)
    account_id = _pick_account_id(jwt_payload, x_account_id)

    admin_jwt = get_admin_jwt(account_id=account_id, base_url=TB_HOST)
    if not admin_jwt:
        raise HTTPException(status_code=500, detail=f"Could not obtain admin JWT for account {account_id}")

    # ---- Example: read query/body if any filters are posted (dates, device IDs, etc.)
    try:
        body = request.json() if hasattr(request, "json") else None  # Starlette Request .json() is async, so:
    except Exception:
        body = None

    # If you expect JSON body, parse it safely
    try:
        body = None
        if request.headers.get("content-type", "").startswith("application/json"):
            body = json.loads((request._body or request.scope.get("_body", b"")).decode("utf-8")) if hasattr(request, "_body") else None
    except Exception:
        body = None

    # ---- Fetch some data from TB (example: list devices & latest telemetry)
    headers = {"X-Authorization": f"Bearer {admin_jwt}"}
    try:
        # You can tailor the query per account/tenant/customer here
        resp = requests.get(f"{TB_HOST}/api/tenant/devices?pageSize=1000&page=0", headers=headers, timeout=30)
        resp.raise_for_status()
        devices = resp.json().get("data", [])
    except requests.RequestException as e:
        logger.error(f"[generate_report] TB device fetch failed: {e}")
        raise HTTPException(status_code=502, detail="ThingsBoard upstream error")

    # ---- Build a simple DataFrame as a placeholder
    rows = []
    for d in devices:
        rows.append({
            "Device Name": d.get("name"),
            "Device ID": (d.get("id") or {}).get("id"),
            "Type": d.get("type"),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        df = pd.DataFrame([{"Device Name": "—", "Device ID": "—", "Type": "—"}])

    # ---- Save to a dated Excel filename
    now = datetime.now(timezone.utc).astimezone()
    fname = f"report_{account_id}_{now.strftime('%Y%m%d_%H%M%S')}.xlsx"
    df.to_excel(fname, index=False)

    # ---- Return a JSON with link (and also support direct download endpoint)
    return JSONResponse({
        "status": "ok",
        "filename": fname,
        "download_url": f"/download/{fname}",
        "count": int(df.shape[0]),
        "account_id": account_id
    })


@router.get("/download/{filename}")
def download_file(filename: str):
    file_path = os.path.join(os.getcwd(), filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
