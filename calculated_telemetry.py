# calculated_telemetry.py
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple, List

import requests
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

# Common pack parser helpers
from pack_format import (
    parse_pack_raw,
    ts_seconds,
    door_to_bit,
    get_float,
)

# Your existing admin JWT helper (signature: get_admin_jwt(account_id: str, host: str) -> str)
from thingsboard_auth import get_admin_jwt

# ------------------------------------------------------------------------------
# Setup
# ------------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("calculated_telemetry")

router = APIRouter()

# Multi-account TB endpoints via env TB_ACCOUNTS='{"acct":"https://thingsboard.cloud", ...}'
try:
    ACCOUNTS = json.loads(os.getenv("TB_ACCOUNTS", "{}"))
    if not isinstance(ACCOUNTS, dict):
        raise ValueError("TB_ACCOUNTS must be a JSON object")
except json.JSONDecodeError:
    raise RuntimeError("Invalid JSON format for TB_ACCOUNTS environment variable")

logger.info(f"[INIT] Loaded ThingsBoard accounts: {list(ACCOUNTS.keys())}")

# ------------------------------------------------------------------------------
# Models
# ------------------------------------------------------------------------------
class CalcIn(BaseModel):
    deviceName: str = Field(...)
    device_token: Optional[str] = Field(default=None)
    pack_raw: str = Field(..., description="k=v|k=v packed raw string")
    ts: Optional[int] = Field(default=None, description="epoch ms (optional; else parsed ts or now)")

# ------------------------------------------------------------------------------
# Caches / state (in-memory; switch to Redis/DB if you need durability)
# ------------------------------------------------------------------------------
_device_id_cache: Dict[str, str] = {}             # f"{account}:{device}" -> deviceId
_floor_meta_cache: Dict[str, Dict[str, Any]] = {} # f"{account}:{device}" -> {boundaries, labels, home_floor, ts}
_movement_state: Dict[str, Dict[str, Any]] = {}   # device -> {"prev_h": float, "last_ts": int}

FLOOR_CACHE_TTL_SEC = 300       # 5 minutes
MOVEMENT_DEADBAND_MM = 20.0     # avoid flapping for tiny height changes

# ------------------------------------------------------------------------------
# TB REST helpers
# ------------------------------------------------------------------------------
def _admin_headers(account_id: str) -> Dict[str, str]:
    host = ACCOUNTS[account_id]
    jwt = get_admin_jwt(account_id, host)
    return {"Content-Type": "application/json", "X-Authorization": f"Bearer {jwt}"}

def _get_device_id(device: str, account_id: str) -> Optional[str]:
    key = f"{account_id}:{device}"
    if key in _device_id_cache:
        return _device_id_cache[key]
    host = ACCOUNTS[account_id]
    url = f"{host}/api/tenant/devices?deviceName={device}"
    r = requests.get(url, headers=_admin_headers(account_id), timeout=10)
    logger.info(f"[DEVICE] lookup {device}@{account_id} -> {r.status_code}")
    if r.ok:
        try:
            dev_id = r.json()["id"]["id"]
            _device_id_cache[key] = dev_id
            return dev_id
        except Exception as e:
            logger.error(f"[DEVICE] parse error: {e}")
    else:
        logger.error(f"[DEVICE] {r.status_code}: {r.text}")
    return None

def _fetch_server_attributes(device_id: str, account_id: str) -> Dict[str, Any]:
    host = ACCOUNTS[account_id]
    url = f"{host}/api/plugins/telemetry/DEVICE/{device_id}/values/attributes/SERVER_SCOPE"
    r = requests.get(url, headers=_admin_headers(account_id), timeout=10)
    r.raise_for_status()
    out = {}
    for item in r.json():
        out[item["key"]] = item.get("value")
    return out

def _get_floor_meta(device: str, account_id: str) -> Tuple[list, list, Optional[int]]:
    """
    Returns (boundaries, labels, home_floor); caches for FLOOR_CACHE_TTL_SEC.
    boundaries: list[int] of floor boundaries/centers in mm
    labels: list[str] (size == len(boundaries)-1)
    """
    key = f"{account_id}:{device}"
    now = time.time()
    cached = _floor_meta_cache.get(key)
    if cached and now - cached.get("ts", 0) < FLOOR_CACHE_TTL_SEC:
        return cached["boundaries"], cached["labels"], cached.get("home_floor")

    dev_id = _get_device_id(device, account_id)
    boundaries: List[int] = []
    labels: List[str] = []
    home_floor: Optional[int] = None
    if dev_id:
        try:
            attrs = _fetch_server_attributes(dev_id, account_id)
            fb_raw = attrs.get("floor_boundaries")  # e.g. "0,3000,6000,..."
            fl_raw = attrs.get("floor_labels")      # e.g. "B3,B2,B1,G,1,2,..."
            hf_raw = attrs.get("home_floor")
            if isinstance(fb_raw, str):
                boundaries = [int(x.strip()) for x in fb_raw.split(",") if x.strip().lstrip("-").isdigit()]
            if isinstance(fl_raw, str):
                labels = [x.strip() for x in fl_raw.split(",")]
            if isinstance(hf_raw, (int, float, str)) and f"{hf_raw}".lstrip("-").isdigit():
                home_floor = int(hf_raw)
        except Exception as e:
            logger.error(f"[ATTR] fetch/parse failed: {e}")

    if not boundaries:
        boundaries = [0, 3000, 6000, 9000, 12000, 15000, 18000]
    if not labels:
        labels = [str(i) for i in range(max(0, len(boundaries) - 1))]
    labels = labels[: max(0, len(boundaries) - 1)]

    _floor_meta_cache[key] = {"boundaries": boundaries, "labels": labels, "home_floor": home_floor, "ts": now}
    return boundaries, labels, home_floor

# ------------------------------------------------------------------------------
# Core math
# ------------------------------------------------------------------------------
def _compute_height(parsed: Dict[str, Any], boundaries: list) -> float:
    """
    Prefer 'h' if present; else 'maxBoundary - laser_val'; else 'height_raw'; else 0.
    """
    h = get_float(parsed, "h")
    if h is not None:
        return float(h)
    laser = get_float(parsed, "laser_val")
    if laser is not None and boundaries:
        max_b = float(boundaries[-1])
        return max(0.0, max_b - float(laser))
    hr = get_float(parsed, "height_raw")
    if hr is not None:
        return float(hr)
    return 0.0

def _floor_index(h: float, boundaries: list) -> int:
    if len(boundaries) < 2:
        return 0
    for i in range(len(boundaries) - 1):
        if boundaries[i] <= h < boundaries[i + 1]:
            return i
    return len(boundaries) - 2

def _derive_motion(device: str, h: float) -> Tuple[str, str, float]:
    """
    Returns (dir='U/D/S', st='M/I', velocity_mm)
    """
    st = _movement_state.setdefault(device, {})
    prev_h = st.get("prev_h")
    if prev_h is None:
        st["prev_h"] = h
        st["last_ts"] = int(time.time() * 1000)
        return "S", "I", 0.0
    vel = h - float(prev_h)
    if   vel >  MOVEMENT_DEADBAND_MM: dirc, status = "U", "M"
    elif vel < -MOVEMENT_DEADBAND_MM: dirc, status = "D", "M"
    else:                             dirc, status = "S", "I"
    st["prev_h"] = h
    st["last_ts"] = int(time.time() * 1000)
    return dirc, status, vel

def _build_pack_calc(ts_sec: int, h: float, fi: int, fl: str, dirc: str, status: str, door_bit: Optional[int]) -> str:
    """
    Compact calculated string saved per second in TB.
    """
    parts = []
    def add(k, v): parts.append(f"{k}={'' if v is None else v}")
    add("v", 1)
    add("ts", ts_sec)
    add("h", round(h))
    add("fi", fi)
    add("fl", fl)
    add("dir", dirc)    # U/D/S
    add("st", status)   # M/I
    add("door", door_bit)
    return "|".join(parts)

# ------------------------------------------------------------------------------
# Endpoint
# ------------------------------------------------------------------------------
@router.post("/calculated-telemetry/")
def calculated_telemetry(
    payload: CalcIn,
    x_account_id: str = Header(...),
    authorization: Optional[str] = Header(None),  # not used; we use admin JWT for TB reads
):
    """
    Rule Chain posts {deviceName, pack_raw, ts?}.
    We compute floor/direction/status/door and return:
        {"pack_calc": "v=1|ts=...|h=...|fi=...|fl=...|dir=U|st=M|door=1", "ts": <ms>}
    """
    if x_account_id not in ACCOUNTS:
        raise HTTPException(status_code=400, detail="Invalid account ID")

    device = payload.deviceName
    parsed = parse_pack_raw(payload.pack_raw)

    # Timestamp:
    #  - prefer payload.ts (ms)
    #  - else parsed ts (seconds) -> convert to ms
    #  - else now
    ts_ms = payload.ts
    if ts_ms is None:
        sec = ts_seconds(parsed)
        ts_ms = int(sec * 1000) if isinstance(sec, int) else int(time.time() * 1000)
    ts_sec = int(ts_ms // 1000)

    # Floor metadata (cached)
    boundaries, labels, home_floor = _get_floor_meta(device, x_account_id)

    # Core deriveds
    h = _compute_height(parsed, boundaries)
    fi = _floor_index(h, boundaries)
    fl = labels[fi] if 0 <= fi < len(labels) else str(fi)
    dirc, status, _vel = _derive_motion(device, h)
    door_bit = door_to_bit(parsed.get("door_val"))

    pack_calc = _build_pack_calc(ts_sec, h, fi, fl, dirc, status, door_bit)

    # Return both pack_calc and ts (ms) so Save TS can use device time if you set useServerTs=false
    return {"pack_calc": pack_calc, "ts": ts_ms}
