# calculated_telemetry.py
import os
import json
import time
import logging
from typing import Dict, Any, Optional, Tuple

import requests
from fastapi import APIRouter, Header, HTTPException, Request

from thingsboard_auth import get_admin_jwt

# Optional live counters (for door/idle aggregation without DB reads)
try:
    from live_counters import process_pack_out_sample
except Exception:
    process_pack_out_sample = None  # safe no-op if not present

logger = logging.getLogger("calculated_telemetry")
logging.basicConfig(level=logging.INFO)

router = APIRouter()

# ---------------------------------------------------------------------------
# Accounts helpers (mirrors main.py behavior)
# ---------------------------------------------------------------------------

def _load_tb_accounts() -> Dict[str, str]:
    """
    Supports either:
      - TB_ACCOUNTS='{"account1":"https://thingsboard.cloud","eu":"https://eu.thingsboard.cloud"}'
      - TB_BASE_URL='https://thingsboard.cloud' (fallback -> {"default": TB_BASE_URL})
    """
    raw = os.getenv("TB_ACCOUNTS", "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data:
                return {str(k): str(v) for k, v in data.items()}
        except Exception as e:
            logger.warning("[INIT] Failed to parse TB_ACCOUNTS: %s", e)

    base = os.getenv("TB_BASE_URL", "https://thingsboard.cloud").strip()
    return {"default": base}

TB_ACCOUNTS = _load_tb_accounts()
logger.info("[INIT] Loaded ThingsBoard accounts: %s", list(TB_ACCOUNTS.keys()))

def _choose_base_url(x_tb_account: Optional[str], body_account: Optional[str]) -> str:
    key = (x_tb_account or body_account or "").strip()
    if key:
        if key in TB_ACCOUNTS:
            return TB_ACCOUNTS[key]
        lk = key.lower()
        if lk in TB_ACCOUNTS:
            return TB_ACCOUNTS[lk]
    # default to the first/only configured URL
    return next(iter(TB_ACCOUNTS.values()))

# ---------------------------------------------------------------------------
# ThingsBoard REST helpers
# ---------------------------------------------------------------------------

def _auth_headers(jwt: str) -> Dict[str, str]:
    return {"X-Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}

def _tb_get(url: str, jwt: str, params: Optional[dict] = None, timeout: int = 25) -> requests.Response:
    return requests.get(url, headers=_auth_headers(jwt), params=params or {}, timeout=timeout)

def tb_find_device_by_name(base: str, jwt: str, device_name: str) -> Dict[str, Any]:
    """
    GET /api/tenant/devices?deviceName={name}
    Returns device JSON or {} if not found.
    """
    url = f"{base.rstrip('/')}/api/tenant/devices"
    r = _tb_get(url, jwt, params={"deviceName": device_name})
    if r.status_code == 200:
        try:
            d = r.json() or {}
            # TB may return actual object or 404-like in body; be defensive
            if isinstance(d, dict) and d.get("id"):
                return d
        except Exception:
            pass
        return {}
    if r.status_code == 404:
        return {}
    # If user token without tenant rights, you can use /api/user/devices paging instead.
    # For this endpoint we expect admin JWT; raise otherwise.
    r.raise_for_status()
    return {}

def tb_save_ts(base: str, jwt: str, device_id: str, values: Dict[str, Any], ts_ms: Optional[int] = None) -> None:
    """
    Write telemetry synchronously (optional; not used for counters since we flush them later).
    """
    url = f"{base.rstrip('/')}/api/plugins/telemetry/DEVICE/{device_id}/timeseries/ANY"
    body = {"ts": ts_ms or int(time.time() * 1000), "values": {}}
    for k, v in values.items():
        if isinstance(v, (dict, list)):
            body["values"][k] = json.dumps(v, separators=(",", ":"))
        else:
            body["values"][k] = v
    r = requests.post(url, headers=_auth_headers(jwt), data=json.dumps(body), timeout=25)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=f"TB save_ts failed: {r.text}")

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _extract_pack_out_and_ts(payload: Dict[str, Any]) -> Tuple[Optional[str], int]:
    """
    Try to find a pack_out (or pack_raw) string and a timestamp (ms) in flexible payloads.
    """
    ts_ms = int(payload.get("ts") or payload.get("timestamp") or int(time.time() * 1000))
    pack_out = None

    # direct
    if isinstance(payload.get("pack_out"), str):
        pack_out = payload["pack_out"]

    # NEW: accept pack_raw as alias
    if pack_out is None and isinstance(payload.get("pack_raw"), str):
        pack_out = payload["pack_raw"]

    # nested "telemetry"
    if pack_out is None:
        telem = payload.get("telemetry")
        if isinstance(telem, dict):
            if isinstance(telem.get("pack_out"), str):
                pack_out = telem["pack_out"]
            elif isinstance(telem.get("pack_raw"), str):
                pack_out = telem["pack_raw"]
            elif isinstance(telem.get("pack_out"), (dict, list)):
                pack_out = json.dumps(telem["pack_out"], separators=(",", ":"))

    # nested "data"
    if pack_out is None:
        data = payload.get("data")
        if isinstance(data, dict):
            if isinstance(data.get("pack_out"), str):
                pack_out = data["pack_out"]
            elif isinstance(data.get("pack_raw"), str):
                pack_out = data["pack_raw"]
            elif isinstance(data.get("pack_out"), (dict, list)):
                pack_out = json.dumps(data["pack_out"], separators=(",", ":"))

    # fallback: accept already-stringified 'payload'
    if pack_out is None and isinstance(payload.get("payload"), str) and "pack_out" in payload["payload"]:
        try:
            j = json.loads(payload["payload"])
            if isinstance(j, dict):
                if isinstance(j.get("pack_out"), str):
                    pack_out = j["pack_out"]
                elif isinstance(j.get("pack_raw"), str):
                    pack_out = j["pack_raw"]
        except Exception:
            pass

    return pack_out, ts_ms

# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/calculated-telemetry/")
async def calculated_telemetry(
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_tb_account: Optional[str] = Header(None, alias="X-TB-Account"),
):
    """
    Accepts a flexible JSON payload that includes:
      - deviceName (preferred) OR deviceId
      - account (optional; else from X-TB-Account; else default from env)
      - pack_out string (JSON or k=v|k=v), and optional ts (ms)
    Behavior:
      - Resolves the device via admin JWT (tenant scope).
      - Feeds the sample to live counters (if enabled) for door/idle aggregation.
      - Optionally can emit other derived telemetry (currently not writing anything here).
    Returns 200 with a small status body.
    """
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Resolve TB base URL
    base = _choose_base_url(x_tb_account, body.get("account"))
    jwt = get_admin_jwt()

    # Resolve device
    device_id = None
    device_name = None

    # If deviceId is provided directly, prefer it
    raw_id = body.get("deviceId") or body.get("device_id")
    if isinstance(raw_id, str) and len(raw_id) >= 10:
        device_id = raw_id

    # Otherwise use deviceName
    device_name = body.get("deviceName") or body.get("device_name") or body.get("name")
    if not device_id:
        if not device_name:
            raise HTTPException(status_code=400, detail="deviceName or deviceId is required")
        dev = tb_find_device_by_name(base, jwt, device_name)
        status = 200 if dev else 404
        logger.info("[DEVICE] lookup %s@%s -> %s", device_name, _account_label(x_tb_account, body.get("account")), status)
        if not dev:
            raise HTTPException(status_code=404, detail=f"Device '{device_name}' not found in account")
        device_id = (dev.get("id") or {}).get("id")
        # Normalize device_name from TB, in case caller used a nickname
        device_name = dev.get("name") or device_name
    else:
        # If we have an ID but not a name, keep the provided name if present
        logger.info("[DEVICE] using provided id %s", device_id)

    # Extract pack_out + timestamp (ms)
    pack_out_str, ts_ms = _extract_pack_out_and_ts(body)

    # Feed live counters (if module is available and we have pack_out)
    fed = False
    if process_pack_out_sample and isinstance(pack_out_str, str):
        try:
            process_pack_out_sample(device_id, device_name or "", ts_ms, pack_out_str)
            fed = True
        except Exception as e:
            logger.exception("[LIVE_COUNTERS] process error for %s (%s): %s", device_name, device_id, e)

    # OPTIONAL: write any immediate derived telemetry here if you want (skipped by design)
    # tb_save_ts(base, jwt, device_id, {"some_calculated_key": ... }, ts_ms)

    return {
        "ok": True,
        "account": _account_label(x_tb_account, body.get("account")),
        "deviceId": device_id,
        "deviceName": device_name,
        "fed_counters": fed,
        "ts_ms": ts_ms,
        "has_pack_out": isinstance(pack_out_str, str)
    }

# ---------------------------------------------------------------------------
# Utils
# ---------------------------------------------------------------------------

def _account_label(hdr: Optional[str], body_val: Optional[str]) -> str:
    v = (hdr or body_val or "default").strip()
    return v
