from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, Union, Tuple, Any, Dict, List
from datetime import datetime
import requests
import os
import logging
import time
import json

from thingsboard_auth import get_admin_jwt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("alarm_logic")

router = APIRouter()

# -----------------------------------------------------------------------------
# Accounts (TB_ACCOUNTS env: {"acctA":"https://tb.hostA","acctB":"https://tb.hostB"})
# -----------------------------------------------------------------------------
try:
    ACCOUNTS = json.loads(os.getenv("TB_ACCOUNTS", "{}"))
    if not isinstance(ACCOUNTS, dict):
        raise ValueError("TB_ACCOUNTS must be a JSON object")
except json.JSONDecodeError:
    raise RuntimeError("Invalid JSON format for TB_ACCOUNTS environment variable")

if not ACCOUNTS:
    logger.warning("[INIT] No ThingsBoard accounts configured (TB_ACCOUNTS empty)")
else:
    logger.info(f"[INIT] Loaded ThingsBoard accounts: {list(ACCOUNTS.keys())}")

# -----------------------------------------------------------------------------
# Thresholds & constants
# -----------------------------------------------------------------------------
THRESHOLDS: Dict[str, float] = {
    "humidity": 50.0,
    "temperature": 50.0,
    "x_jerk": 5.0,
    "y_jerk": 5.0,
    "z_jerk": 15.0,
    "x_vibe": 5.0,
    "y_vibe": 5.0,
    "z_vibe": 15.0,
    "sound_db": 80.0,  # added: do same as vibration/jerk
}

# +/- 50mm height zone for bucket counting
ZONE_MM = 50.0
# count needed within a zone to trigger alarm
BUCKET_COUNT_THRESHOLD = 3

TOLERANCE_MM = 10.0
DOOR_OPEN_THRESHOLD_SEC = 15

HTTP_TIMEOUT = 12  # seconds

# -----------------------------------------------------------------------------
# Payload
# -----------------------------------------------------------------------------
class TelemetryPayload(BaseModel):
    deviceName: str = Field(...)
    floor: str = Field(...)
    # allow str/int; most of the pipeline expects ms since epoch
    timestamp: Optional[Union[int, str]] = Field(default=None)

    height: Optional[Union[float, str]] = Field(default=None)
    current_floor_index: Optional[Union[int, str]] = Field(default=None)

    x_vibe: Optional[Union[float, str]] = Field(default=None)
    y_vibe: Optional[Union[float, str]] = Field(default=None)
    z_vibe: Optional[Union[float, str]] = Field(default=None)

    x_jerk: Optional[Union[float, str]] = Field(default=None)
    y_jerk: Optional[Union[float, str]] = Field(default=None)
    z_jerk: Optional[Union[float, str]] = Field(default=None)

    temperature: Optional[Union[float, str]] = Field(default=None)
    humidity: Optional[Union[float, str]] = Field(default=None)

    door_open: Optional[Union[bool, str, int]] = Field(default=None)

    # optional sound telemetry
    sound_db: Optional[Union[float, str]] = Field(default=None)

# -----------------------------------------------------------------------------
# In-memory state
# -----------------------------------------------------------------------------
device_cache: Dict[str, str] = {}                # (account:deviceName) -> deviceId
bucket_counts: Dict[str, Dict[str, List[Dict]]] = {}  # device -> key -> list[{center,count}]
device_door_state: Dict[str, bool] = {}
door_open_since: Dict[str, float] = {}           # seconds monotonic

# -----------------------------------------------------------------------------
# Parsers / helpers
# -----------------------------------------------------------------------------
def parse_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (ValueError, TypeError):
        return None

def parse_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (ValueError, TypeError):
        return None

def parse_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(int(value))
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "open", "on"):
            return True
        if v in ("false", "0", "no", "closed", "off"):
            return False
    return None

def epoch_ms_from_any(ts: Optional[Union[int, str]]) -> int:
    """
    Accept int ms, int sec, or ISO string. Fall back to now.
    """
    if ts is None:
        return int(time.time() * 1000)
    if isinstance(ts, int):
        # assume ms if very large, else seconds
        return ts if ts > 1_000_000_000_000 else ts * 1000
    if isinstance(ts, str):
        s = ts.strip()
        # if numeric string
        if s.isdigit():
            iv = int(s)
            return iv if iv > 1_000_000_000_000 else iv * 1000
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            pass
    return int(time.time() * 1000)

def quoted(s: str) -> str:
    return requests.utils.quote(s, safe="")

# -----------------------------------------------------------------------------
# ThingsBoard API helpers
# -----------------------------------------------------------------------------
def get_device_id(device_name: str, account_id: str) -> Optional[str]:
    cache_key = f"{account_id}:{device_name}"
    if cache_key in device_cache:
        return device_cache[cache_key]

    base = ACCOUNTS.get(account_id)
    if not base:
        logger.error(f"[DEVICE_LOOKUP] Unknown account_id={account_id}")
        return None

    jwt = get_admin_jwt(account_id, base)
    if not jwt:
        logger.error(f"[DEVICE_LOOKUP] No JWT for account={account_id}")
        return None

    url = f"{base}/api/tenant/devices?deviceName={quoted(device_name)}"
    try:
        res = requests.get(url, headers={"X-Authorization": f"Bearer {jwt}"}, timeout=HTTP_TIMEOUT)
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, dict) and data.get("id", {}).get("id"):
                device_id = data["id"]["id"]
                device_cache[cache_key] = device_id
                return device_id
        logger.error(f"[DEVICE_LOOKUP] Failed {res.status_code} | {res.text}")
    except Exception as e:
        logger.error(f"[DEVICE_LOOKUP] Exception: {e}")
    return None

def get_floor_boundaries(device_id: str, account_id: str) -> Optional[List[float]]:
    base = ACCOUNTS.get(account_id)
    if not base:
        return None
    jwt = get_admin_jwt(account_id, base)
    if not jwt:
        logger.warning("[ATTRIBUTES] No JWT; cannot fetch floor_boundaries")
        return None

    url = f"{base}/api/plugins/telemetry/DEVICE/{device_id}/values/attributes/SERVER_SCOPE"
    try:
        res = requests.get(url, headers={"X-Authorization": f"Bearer {jwt}"}, timeout=HTTP_TIMEOUT)
        if res.status_code != 200:
            logger.error(f"[ATTRIBUTES] Failed {res.status_code} | {res.text}")
            return None

        for attr in res.json() or []:
            if attr.get("key") == "floor_boundaries":
                val = attr.get("value")
                # accept list, JSON string, or comma string
                if isinstance(val, list):
                    return [float(x) for x in val]
                if isinstance(val, str):
                    try:
                        j = json.loads(val)
                        if isinstance(j, list):
                            return [float(x) for x in j]
                    except Exception:
                        pass
                    # fallback: comma string
                    return [float(x.strip()) for x in val.split(",") if x.strip()]
        return None
    except Exception as e:
        logger.error(f"[ATTRIBUTES] Exception: {e}")
        return None

def create_alarm_on_tb(device_name: str, alarm_type: str, ts_ms: int, severity: str, details: dict, account_id: str):
    base = ACCOUNTS.get(account_id)
    if not base:
        logger.warning(f"[ALARM] Unknown account {account_id}")
        return

    device_id = get_device_id(device_name, account_id)
    if not device_id:
        logger.warning(f"[ALARM] Could not resolve device ID for {device_name}")
        return

    jwt = get_admin_jwt(account_id, base)
    if not jwt:
        logger.warning(f"[ALARM] No JWT for account {account_id}")
        return

    alarm_payload = {
        "originator": {"entityType": "DEVICE", "id": device_id},
        "type": alarm_type,
        "severity": severity,  # WARNING/MINOR/MAJOR/CRITICAL
        "status": "ACTIVE_UNACK",
        "details": details or {},
        "startTs": ts_ms,
    }
    try:
        resp = requests.post(
            f"{base}/api/alarm",
            headers={"X-Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
            json=alarm_payload,
            timeout=HTTP_TIMEOUT,
        )
        if 200 <= resp.status_code < 300:
            logger.info(f"[ALARM] Created {alarm_type} for {device_name}")
        else:
            logger.error(f"[ALARM] Failed {resp.status_code} | {resp.text}")
    except Exception as e:
        logger.error(f"[ALARM] Exception: {e}")

# -----------------------------------------------------------------------------
# Alarm logic helpers
# -----------------------------------------------------------------------------
def check_bucket_and_trigger(
    device: str,
    key: str,
    value: float,
    height: Optional[float],
    ts_ms: int,
    floor: str,
    account_id: str,
):
    """Count threshold breaches within a +/- ZONE_MM window around height; fire at count>=3."""
    if height is None:
        # Cannot bucket by height without a height reading
        return

    if device not in bucket_counts:
        bucket_counts[device] = {}
    if key not in bucket_counts[device]:
        bucket_counts[device][key] = []

    buckets = bucket_counts[device][key]
    for b in buckets:
        if abs(b["center"] - height) <= ZONE_MM:
            b["count"] += 1
            if b["count"] >= BUCKET_COUNT_THRESHOLD:
                create_alarm_on_tb(
                    device,
                    f"{key} Alarm",
                    ts_ms,
                    "MINOR",
                    {
                        "value": value,
                        "threshold": THRESHOLDS[key],
                        "floor": floor,
                        "height_zone": f"{b['center']-ZONE_MM:.1f} to {b['center']+ZONE_MM:.1f}",
                        "count": b["count"],
                    },
                    account_id,
                )
                buckets.remove(b)
            return

    # no matching bucket: create a new one
    buckets.append({"center": height, "count": 1})

def process_door_alarm(device_name: str, door_open_in: Optional[bool], floor: str, ts_ms: int, account_id: str):
    now = time.monotonic()
    door_open = door_open_in
    if door_open is None:
        door_open = device_door_state.get(device_name, False)
    else:
        device_door_state[device_name] = door_open

    if door_open:
        if device_name not in door_open_since:
            door_open_since[device_name] = now
        else:
            duration = now - door_open_since[device_name]
            if duration >= DOOR_OPEN_THRESHOLD_SEC:
                create_alarm_on_tb(
                    device_name,
                    "Door Open Too Long",
                    ts_ms,
                    "MAJOR",
                    {"duration_sec": int(duration), "floor": floor},
                    account_id,
                )
                # reset so it must exceed the window again
                door_open_since[device_name] = now
    else:
        door_open_since.pop(device_name, None)

def floor_mismatch_detected(height: Optional[float], current_floor_index: Optional[int], floor_boundaries: Optional[List[float]]) -> Tuple[bool, float, float]:
    if height is None or current_floor_index is None or floor_boundaries is None:
        return False, 0.0, 0.0

    try:
        if current_floor_index < 0 or current_floor_index >= len(floor_boundaries):
            return False, 0.0, 0.0  # out of range -> don't alarm here
        floor_center = float(floor_boundaries[current_floor_index])
        deviation = height - floor_center
        return abs(deviation) > TOLERANCE_MM, deviation, floor_center
    except Exception as e:
        logger.error(f"[FLOOR] floor mismatch calc failed: {e}")
        return False, 0.0, 0.0

# -----------------------------------------------------------------------------
# Endpoint
# -----------------------------------------------------------------------------
@router.post("/check_alarm/")
async def check_alarm(
    payload: TelemetryPayload,
    x_account_id: str = Header(...),
):
    logger.info("--- /check_alarm/ invoked ---")

    if x_account_id not in ACCOUNTS:
        raise HTTPException(status_code=400, detail="Invalid account ID")

    # Prefer device-provided timestamp if present
    ts_ms = epoch_ms_from_any(payload.timestamp)

    # Parse core fields safely
    height = parse_float(payload.height)
    cfi = parse_int(payload.current_floor_index)

    # Normalize door boolean
    door_bool = parse_bool(payload.door_open)
    if door_bool is None:
        # keep last known state if not provided
        door_bool = device_door_state.get(payload.deviceName, False)

    triggered: List[Dict[str, Any]] = []

    try:
        # Simple scalar alarms (no bucketing)
        for k in ("humidity", "temperature"):
            val = parse_float(getattr(payload, k))
            if val is not None and k in THRESHOLDS and val > THRESHOLDS[k]:
                triggered.append(
                    {
                        "type": f"{k.capitalize()} Alarm",
                        "value": val,
                        "threshold": THRESHOLDS[k],
                        "severity": "WARNING",
                    }
                )
                create_alarm_on_tb(
                    payload.deviceName,
                    f"{k.capitalize()} Alarm",
                    ts_ms,
                    "WARNING",
                    {"value": val, "threshold": THRESHOLDS[k], "floor": payload.floor},
                    x_account_id,
                )

        # Bucketed alarms: jerk, vibration, and sound (if provided)
        for key in ("x_jerk", "y_jerk", "z_jerk", "x_vibe", "y_vibe", "z_vibe", "sound_db"):
            val = parse_float(getattr(payload, key))
            thr = THRESHOLDS.get(key)
            if val is not None and thr is not None and val > thr:
                check_bucket_and_trigger(payload.deviceName, key, val, height, ts_ms, payload.floor, x_account_id)

        # Floor mismatch check only when door is open and we have a floor index
        if cfi is not None and door_bool:
            device_id = get_device_id(payload.deviceName, x_account_id)
            if device_id:
                floor_boundaries = get_floor_boundaries(device_id, x_account_id)
                mismatch, deviation, floor_center = floor_mismatch_detected(height, cfi, floor_boundaries)
                if mismatch:
                    position = "above" if deviation > 0 else "below"
                    triggered.append(
                        {
                            "type": "Floor Mismatch Alarm",
                            "value": height,
                            "severity": "CRITICAL",
                            "position": position,
                        }
                    )
                    create_alarm_on_tb(
                        payload.deviceName,
                        "Floor Mismatch Alarm",
                        ts_ms,
                        "CRITICAL",
                        {
                            "reported_index": cfi,
                            "height": height,
                            "floor_center": floor_center,
                            "deviation_mm": abs(deviation),
                            "position": position,
                        },
                        x_account_id,
                    )

        # Door-open-duration alarm (monotonic clock to prevent ts skew)
        process_door_alarm(payload.deviceName, door_bool, payload.floor, ts_ms, x_account_id)

        logger.info(f"[RESULT] {payload.deviceName} alarms_triggered={len(triggered)}")
        return {"status": "processed", "alarms_triggered": triggered}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[ERROR] Exception during alarm processing: {e}")
        raise HTTPException(status_code=500, detail="Alarm processing failed")
