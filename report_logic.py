# report_logic.py
import os
import io
import math
import logging
from typing import List, Optional, Dict, Any, Union
from datetime import datetime

import pandas as pd
import requests
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, field_validator

from thingsboard_auth import get_admin_jwt

logger = logging.getLogger("report_logic")

TB_HOST = os.getenv("TB_BASE_URL", "https://thingsboard.cloud").rstrip("/")
REQUEST_TIMEOUT = float(os.getenv("TB_HTTP_TIMEOUT", "20"))

# Keys your rule-chain produces (can be filtered by the widget)
NORMALIZED_KEYS = [
    "height", "direction", "lift_status",
    "current_floor_index", "current_floor_label",
    "x_vibe", "y_vibe", "z_vibe",
    "x_jerk", "y_jerk", "z_jerk",
    "temperature", "humidity", "sound_level", "door_open"
]

router = APIRouter()


# ------------------------- Helpers -------------------------

def _parse_date_to_ms(v: Union[str, int, float]) -> int:
    """
    Accepts:
      - ms timestamp (int/str)
      - ISO datetime '2025-08-08T06:58:39Z'
      - 'yyyy-mm-dd'  (ThingsBoard widget <input type=date> returns this)
      - 'dd/mm/yyyy'  (in case some UIs use this)
    Returns epoch ms (int).
    """
    if v is None:
        raise ValueError("Missing date")

    if isinstance(v, (int, float)) and not math.isnan(float(v)):
        ts = int(v)
        return ts * 1000 if ts < 10_000_000_000 else ts

    s = str(v).strip()
    if s.isdigit():
        ts = int(s)
        return ts * 1000 if ts < 10_000_000_000 else ts

    # ISO
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        pass

    # yyyy-mm-dd or dd/mm/yyyy
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(s, fmt)
            return int(dt.timestamp() * 1000)
        except Exception:
            continue

    raise ValueError(f"Unsupported date format: {s}")


def _tb_get(url: str, jwt: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
    return requests.get(
        url,
        headers={"X-Authorization": f"Bearer {jwt}"},
        params=params,
        timeout=REQUEST_TIMEOUT,
    )


def _lookup_device_by_name(admin_jwt: str, device_name: str) -> Optional[Dict[str, Any]]:
    """
    Try fast lookup by name; fall back to a list & filter if needed.
    """
    # Fast path (works on TB Cloud): /api/tenant/devices?deviceName=<name>
    url = f"{TB_HOST}/api/tenant/devices"
    r = _tb_get(url, admin_jwt, params={"deviceName": device_name})
    if r.status_code == 200:
        try:
            d = r.json()
            if isinstance(d, dict) and d.get("id", {}).get("id"):
                return d
        except Exception:
            pass

    # Fallback: list page and filter by name
    url = f"{TB_HOST}/api/tenant/devices?pageSize=200&page=0&sortProperty=createdTime&sortOrder=DESC"
    r = _tb_get(url, admin_jwt)
    if r.status_code == 200:
        try:
            for d in r.json().get("data", []):
                if d.get("name") == device_name:
                    return d
        except Exception:
            pass
    logger.error("Device '%s' not found via ThingsBoard APIs", device_name)
    return None


def _fetch_timeseries(
    admin_jwt: str,
    device_id: str,
    keys: List[str],
    start_ts: int,
    end_ts: int,
    limit: int = 100000
) -> Dict[str, List[Dict[str, Any]]]:
    url = f"{TB_HOST}/api/plugins/telemetry/DEVICE/{device_id}/values/timeseries"
    params = {
        "keys": ",".join(keys),
        "startTs": start_ts,
        "endTs": end_ts,
        "limit": limit,
        "agg": "NONE",
    }
    r = _tb_get(url, admin_jwt, params=params)
    if r.status_code != 200:
        logger.error("TS fetch failed %s: %s", r.status_code, r.text[:300])
        raise HTTPException(status_code=502, detail="Failed to fetch timeseries from ThingsBoard")
    return r.json()


def _timeseries_to_frame(ts_dict: Dict[str, List[Dict[str, Any]]]) -> pd.DataFrame:
    frames = []
    for key, rows in ts_dict.items():
        if not rows:
            continue
        df = pd.DataFrame(rows)
        if "ts" not in df or "value" not in df:
            continue
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        # Numeric if possible, else keep strings (e.g., labels)
        df["value"] = pd.to_numeric(df["value"], errors="ignore")
        frames.append(df.rename(columns={"value": key})[["ts", key]])

    if not frames:
        return pd.DataFrame()

    out = frames[0].set_index("ts")
    for f in frames[1:]:
        out = out.join(f.set_index("ts"), how="outer")
    return out.sort_index()


def _write_excel(filename: str, sheets: Dict[str, pd.DataFrame]) -> None:
    with pd.ExcelWriter(filename, engine="openpyxl") as writer:
        for name, df in sheets.items():
            # Excel sheet name max 31 chars
            safe = (name or "Sheet")[:31]
            if df.empty:
                pd.DataFrame().to_excel(writer, sheet_name=safe, index=False)
            else:
                df = df.reset_index().rename(columns={"ts": "timestamp_utc"})
                df.to_excel(writer, sheet_name=safe, index=False)


# ------------------------- Models -------------------------

class GenerateReportIn(BaseModel):
    # Matches your widget payload exactly
    device_name: str
    data_types: List[str]
    include_alarms: bool = False
    start_date: Union[str, int, float]
    end_date: Union[str, int, float]

    @field_validator("data_types")
    @classmethod
    def _validate_keys(cls, v: List[str]):
        if not v:
            raise ValueError("data_types cannot be empty")
        # Only keep known keys; ignore typos silently
        return [k for k in v if k in NORMALIZED_KEYS]


# ------------------------- Route -------------------------

@router.post("/generate_report/")
def generate_report(
    body: GenerateReportIn,
    authorization: str = Header(...),
    account_id: str = Header(default="account1", alias="X-Account-ID"),
):
    """
    Generates an Excel report for a single device by *name* over a date range.
    - Accepts yyyy-mm-dd (widget), dd/mm/yyyy, ISO, or ms timestamps.
    - Returns: filename + download_url that the widget uses.
    """
    # Parse/validate bearer
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    # Normalize dates
    try:
        start_ms = _parse_date_to_ms(body.start_date)
        end_ms = _parse_date_to_ms(body.end_date)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if end_ms <= start_ms:
        raise HTTPException(status_code=422, detail="end_date must be after start_date")

    # Admin JWT for the account (so we can query any device under the tenant)
    admin_jwt = get_admin_jwt(account_id, TB_HOST)
    if not admin_jwt:
        raise HTTPException(status_code=500, detail="Failed to retrieve admin JWT")

    # Resolve device by name -> id
    device = _lookup_device_by_name(admin_jwt, body.device_name)
    if not device:
        raise HTTPException(status_code=404, detail=f"Device '{body.device_name}' not found")
    device_id = device.get("id", {}).get("id") or device.get("id")
    if not device_id:
        raise HTTPException(status_code=500, detail="Malformed device object from ThingsBoard")

    # Fetch timeseries for selected keys
    keys = body.data_types
    ts_raw = _fetch_timeseries(admin_jwt, device_id, keys, start_ms, end_ms)
    df = _timeseries_to_frame(ts_raw)

    # Optionally, add alarms sheet later (body.include_alarms). For now, telemetry only.
    sheets: Dict[str, pd.DataFrame] = {body.device_name: df}

    # Write file to disk (served by /download/{filename})
    ts_suffix = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"report_{body.device_name}_{ts_suffix}.xlsx".replace(" ", "_")
    _write_excel(filename, sheets)

    return {
        "ok": True,
        "filename": filename,
        "download_url": f"/download/{filename}",
        "device_name": body.device_name,
        "from": start_ms,
        "to": end_ms,
        "keys": keys,
        "include_alarms": bool(body.include_alarms),
    }
