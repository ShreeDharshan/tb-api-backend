import os
import json
import time
import logging
from typing import Dict, Any, Optional, Tuple

import requests
from fastapi import APIRouter, Header, HTTPException, Request

from thingsboard_auth import get_admin_jwt

# Optional live counters (door/idle aggregation without DB reads)
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

def _choose_base_url(*candidates: Optional[str]) -> str:
    """
    Choose base URL from multiple possible account id inputs (header or body).
    """
    for key in candidates:
        if not key:
            continue
        k = key.strip()
        if not k:
            continue
        if k in TB_ACCOUNTS:
            return TB_ACCOUNTS[k]
        lk = k.lower()
        if lk in TB_ACCOUNTS:
            return TB_ACCOUNTS[lk]
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
            if isinstance(d, dict) and d.get("id"):
                return d
        except Exception:
            pass
        return {}
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    return {}

def tb_save_ts(base: str, jwt: str, device_id: str, values: Dict[str, Any], ts_ms: Optional[int] = None) -> None:
    """
    Write telemetry synchronously (not used here; counters flush separately).
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

def _extract_pack_out_or_raw_and_ts(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], int]:
    """
    Try to find pack_out or pack_raw and a timestamp (ms) in flexible payloads.
    Returns (pack_out, pack_raw, ts_ms). If only pack_raw exists, we will treat it as pack_out.
    """
    ts_ms = int(payload.get("ts") or payload.get("timestamp") or int(time.time() * 1000))
    pack_out = None
    pack_raw = None

    def _pick(d: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        po = d.get("pack_out")
        pr = d.get("pack_raw")
        return (po if isinstance(po, str) else None,
                pr if isinstance(pr, str) else None)

    # direct
    pack_out, pack_raw = _pick(payload)

    # nested "telemetry"
    if not pack_out and not pack_raw:
        telem = payload.get("telemetry")
        if isinstance(telem, dict):
            pack_out, pack_raw = _pick(telem)
            if not pack_out and isinstance(telem.get("pack_out"), (dict, list)):
                pack_out = json.dumps(telem["pack_out"], separators=(",", ":"))

    # nested "data"
    if not pack_out and not pack_raw:
        data = payload.get("data")
        if isinstance(data, dict):
            pack_out, pack_raw = _pick(data)
            if not pack_out and isinstance(data.get("pack_out"), (dict, list)):
                pack_out = json.dumps(data["pack_out"], separators=(",", ":"))

    # fallback: accept already-stringified 'payload'
    if not pack_out and not pack_raw and isinstance(payload.get("payload"), str):
        try:
            j = json.loads(payload["payload"])
            if isinstance(j, dict):
                po, pr = _pick(j)
                if po:
                    pack_out = po
                elif pr:
                    pack_raw = pr
        except Exception:
            pass

    return pack_out, pack_raw, ts_ms

# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/calculated-telemetry/")
async def calculated_telemetry(
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_tb_account: Optional[str] = Header(None, alias="X-TB-Account"),
    x_account_id: Optional[str] = Header(None, alias="X-Account-ID"),
):
    """
    Accepts:
      - deviceName (preferred) OR deviceId
      - (optional) account header: X-TB-Account or X-Account-ID, or 'account' in body
      - pack_out string (JSON or k=v|k=v) OR pack_raw string; ts optional (ms)
    Behavior:
      - Resolves device via admin JWT.
      - Ensures we have a 'pack_out' string (if only pack_raw present, we use that).
      - Feeds the sample to live counters (if module is available).
      - Returns a body that includes 'pack_out' so the rule-chain can save it.
    """
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    base = _choose_base_url(x_tb_account, x_account_id, body.get("account"))
    jwt = get_admin_jwt()

    # Resolve device
    device_id = None
    device_name = None

    # Prefer provided deviceId
    raw_id = body.get("deviceId") or body.get("device_id")
    if isinstance(raw_id, str) and len(raw_id) >= 10:
        device_id = raw_id

    # Otherwise resolve by name
    device_name = body.get("deviceName") or body.get("device_name") or body.get("name")
    if not device_id:
        if not device_name:
            raise HTTPException(status_code=400, detail="deviceName or deviceId is required")
        dev = tb_find_device_by_name(base, jwt, device_name)
        status = 200 if dev else 404
        logger.info("[DEVICE] lookup %s@%s -> %s", device_name, _account_label(x_tb_account, x_account_id, body.get("account")), status)
        if not dev:
            raise HTTPException(status_code=404, detail=f"Device '{device_name}' not found in account")
        device_id = (dev.get("id") or {}).get("id")
        device_name = dev.get("name") or device_name
    else:
        logger.info("[DEVICE] using provided id %s", device_id)

    # Extract pack_out/pack_raw + timestamp (ms)
    pack_out_str, pack_raw_str, ts_ms = _extract_pack_out_or_raw_and_ts(body)

    # If pack_out missing but pack_raw present, treat raw as out (k=v|... works with counters)
    if not isinstance(pack_out_str, str) and isinstance(pack_raw_str, str):
        pack_out_str = pack_raw_str

    # Feed live counters (if available and we have a string)
    fed = False
    if process_pack_out_sample and isinstance(pack_out_str, str):
        try:
            process_pack_out_sample(device_id, device_name or "", ts_ms, pack_out_str)
            fed = True
        except Exception as e:
            logger.exception("[LIVE_COUNTERS] process error for %s (%s): %s", device_name, device_id, e)

    # Respond with pack_out so rule chain can store it
    resp = {
        "ok": True,
        "account": _account_label(x_tb_account, x_account_id, body.get("account")),
        "deviceId": device_id,
        "deviceName": device_name,
        "fed_counters": fed,
        "ts_ms": ts_ms,
        "has_pack_out": isinstance(pack_out_str, str),
    }
    if isinstance(pack_out_str, str):
        resp["pack_out"] = pack_out_str

    return resp

# ---------------------------------------------------------------------------
# Utils
# ---------------------------------------------------------------------------

def _account_label(h1: Optional[str], h2: Optional[str], body_val: Optional[str]) -> str:
    for v in (h1, h2, body_val):
        if v is not None:
            vv = str(v).strip()
            if vv:
                return vv
    return "default"
