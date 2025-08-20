# live_counters.py (pure in-memory)
import os
import json
import time
import math
import logging
from typing import Dict, Optional, Tuple

import requests
from thingsboard_auth import get_admin_jwt

logger = logging.getLogger("live_counters")
logging.basicConfig(level=logging.INFO)

TB_BASE_URL = os.getenv("TB_BASE_URL", "https://thingsboard.cloud").rstrip("/")

# ---- Behavior knobs ----
LC_ENABLED = os.getenv("LC_ENABLED", "true").lower() in ("1", "true", "yes")
LC_TZ = os.getenv("LC_TZ", "UTC")  # e.g., "Asia/Kolkata"
LC_MOVEMENT_THRESHOLD_MM = float(os.getenv("LC_MOVEMENT_THRESHOLD_MM", "50"))

# ---- In-memory stores ----
# Timeseries-like hashes keyed by logical Redis-style keys (for easy future migration)
_inmem: Dict[str, Dict[str, int]] = {}
_state_inmem: Dict[str, Dict[str, str]] = {}  # last sample state per device_id


def _local_date_str(ts_ms: int) -> str:
    """
    Convert epoch ms to local date string YYYY-MM-DD using LC_TZ.
    Supports fixed offsets like '+05:30'; otherwise uses UTC.
    """
    tz = LC_TZ.strip()
    if tz.startswith(("+", "-")) and len(tz) >= 3 and ":" in tz:
        sign = 1 if tz[0] == "+" else -1
        hh, mm = tz[1:].split(":", 1)
        offset_sec = sign * (int(hh) * 3600 + int(mm) * 60)
        sec = (ts_ms // 1000) + offset_sec
        return time.strftime("%Y-%m-%d", time.gmtime(sec))
    return time.strftime("%Y-%m-%d", time.gmtime(ts_ms / 1000.0))


def _parse_pack_out(v: str) -> Tuple[Optional[str], float, Optional[bool]]:
    """
    Extract (floor_label, height_mm, door_open) from pack_out (JSON or k=v|k=v).
    """
    floor_label, height_mm, door_open = None, float("nan"), None
    if not v:
        return floor_label, height_mm, door_open
    # Try JSON first
    try:
        j = json.loads(v)
        floor_label = j.get("current_floor_label") or j.get("floor_label")
        h = j.get("height_mm") or j.get("height") or j.get("height_raw")
        if h is not None:
            height_mm = float(h)
        if "door_open" in j:
            door_open = bool(j["door_open"])
        elif "door_val" in j:
            door_open = (str(j["door_val"]).upper() == "OPEN")
        return floor_label, height_mm, door_open
    except Exception:
        pass
    # Fallback: k=v|k=v
    parts = {}
    for p in v.split("|"):
        if "=" in p:
            k, vv = p.split("=", 1)
            parts[k] = vv
    floor_label = parts.get("current_floor_label") or parts.get("floor_label")
    try:
        height_mm = float(parts.get("height_mm") or parts.get("height") or parts.get("height_raw") or "nan")
    except Exception:
        height_mm = float("nan")
    dv = (parts.get("door_open") or parts.get("door_val") or "").upper()
    if dv != "":
        door_open = True if dv in ("TRUE", "OPEN", "1") else False
    return floor_label, height_mm, door_open


def _movement(prev_h: float, h: float, thr: float) -> bool:
    if math.isnan(prev_h) or math.isnan(h):
        return False
    return abs(h - prev_h) > thr


# ---------- State & counters storage helpers (in-memory) ----------

def _state_key(device_id: str) -> str:
    return f"lc:state:{device_id}"

def _door_key(date_str: str, device_id: str) -> str:
    return f"lc:{date_str}:{device_id}:door"  # hash: floor -> count

def _idle_key(date_str: str, device_id: str) -> str:
    return f"lc:{date_str}:{device_id}:idle_ms"  # hash: floor -> ms

def _hinc(store_key: str, field: str, delta: int) -> None:
    h = _inmem.setdefault(store_key, {})
    h[field] = int(h.get(field, 0)) + int(delta)

def _hgetall(store_key: str) -> Dict[str, int]:
    return {k: int(v) for k, v in (_inmem.get(store_key, {}) or {}).items()}

def _state_get(device_id: str) -> Dict[str, str]:
    return _state_inmem.get(device_id, {}).copy()

def _state_set(device_id: str, d: Dict[str, str]) -> None:
    _state_inmem[device_id] = d.copy()


# ---------- Public API ----------

def process_pack_out_sample(device_id: str,
                            device_name: str,
                            ts_ms: int,
                            pack_out_str: str) -> None:
    """
    Incrementally update counters for this sample.
    Safe to call many times per second. Idempotent wrt ts_ms (we skip <= last_ts).
    """
    if not LC_ENABLED:
        return

    fl, h, dopen = _parse_pack_out(pack_out_str)
    if fl is None and math.isnan(h) and dopen is None:
        return  # nothing to do

    state = _state_get(device_id)
    last_ts = int(state.get("ts", "0") or "0")
    last_floor = state.get("floor")
    try:
        last_h = float(state.get("h")) if "h" in state else float("nan")
    except Exception:
        last_h = float("nan")
    last_door = state.get("door")
    last_door_bool = None if last_door is None else (last_door == "1")

    # dedupe / ordering guard
    if ts_ms <= last_ts:
        return

    # Compute date (by local tz) to bucket per-day counters
    date_str = _local_date_str(ts_ms)

    # Rising edge detection: CLOSED -> OPEN
    if last_door_bool in (False, 0) and dopen in (True, 1):
        _hinc(_door_key(date_str, device_id), fl or last_floor or "UNKNOWN", 1)

    # Idle accumulation between last_ts and ts_ms if both closed and not moving
    if (dopen in (False, 0) and last_door_bool in (False, 0)) and not _movement(last_h, h, LC_MOVEMENT_THRESHOLD_MM):
        dt = ts_ms - last_ts
        if dt > 0:
            _hinc(_idle_key(date_str, device_id), fl or last_floor or "UNKNOWN", dt)

    # Persist new state
    _state_set(device_id, {
        "ts": str(ts_ms),
        "floor": (fl or last_floor or "UNKNOWN"),
        "h": "nan" if math.isnan(h) else str(h),
        "door": "1" if (dopen in (True, 1)) else ("0" if dopen in (False, 0) else (last_door or ""))
    })


def flush_day_to_tb(date_str: str) -> int:
    """
    Push aggregated counters for the given date to ThingsBoard for all devices seen that day.
    Returns number of devices flushed.
    """
    # enumerate device_ids that have any counters for this date
    candidates = set()
    for key in list(_inmem.keys()):
        if key.startswith(f"lc:{date_str}:") and (key.endswith(":door") or key.endswith(":idle_ms")):
            parts = key.split(":")
            if len(parts) >= 4:
                candidates.add(parts[2])

    if not candidates:
        logger.info("[LiveCounters] No devices to flush for %s", date_str)
        return 0

    jwt = get_admin_jwt()
    flushed = 0
    write_ts_ms = int(time.time() * 1000) - 1

    for device_id in candidates:
        door_counts = _hgetall(_door_key(date_str, device_id))
        idle_ms = _hgetall(_idle_key(date_str, device_id))
        idle_sec = {k: int(round(v / 1000.0)) for k, v in idle_ms.items()}

        payload = {
            "daily_floor_door_opens": door_counts,
            "daily_floor_idle_sec": idle_sec,
            "daily_floor_summary": {"date": date_str, "door_opens": door_counts, "idle_sec": idle_sec}
        }
        # Save telemetry
        url = f"{TB_BASE_URL}/api/plugins/telemetry/DEVICE/{device_id}/timeseries/ANY"
        body = {"ts": write_ts_ms, "values": {}}
        for k, v in payload.items():
            if isinstance(v, (dict, list)):
                body["values"][k] = json.dumps(v, separators=(",", ":"))
            else:
                body["values"][k] = v
        r = requests.post(
            url, headers={"X-Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
            data=json.dumps(body), timeout=45
        )
        if r.status_code >= 400:
            logger.error("[LiveCounters] TB save_ts failed for %s (%s): %s", device_id, r.status_code, r.text)
            continue

        flushed += 1

        # clear counters after flush
        _inmem.pop(_door_key(date_str, device_id), None)
        _inmem.pop(_idle_key(date_str, device_id), None)

    logger.info("[LiveCounters] Flushed %d device(s) for %s", flushed, date_str)
    return flushed
