import json
import logging
import math
import os
import time
from typing import Dict, Optional, Tuple

import requests

from src.core.auth import get_admin_jwt

logger = logging.getLogger("services.live_counters")

TB_BASE_URL = os.getenv("TB_BASE_URL", "https://thingsboard.cloud").rstrip("/")
LC_ENABLED = os.getenv("LC_ENABLED", "true").lower() in ("1", "true", "yes")
LC_TZ = os.getenv("LC_TZ", "UTC")
LC_MOVEMENT_THRESHOLD_MM = float(os.getenv("LC_MOVEMENT_THRESHOLD_MM", "50"))
LC_KEY_TTL_HOURS = int(os.getenv("LC_REDIS_KEY_TTL_HOURS", "48"))
LC_DEBUG = os.getenv("LC_DEBUG", "0") in ("1", "true", "TRUE", "yes", "on")

_inmem: Dict[str, Dict[str, int]] = {}
_state_inmem: Dict[str, Dict[str, str]] = {}


def _dbg(msg: str, *args) -> None:
    if LC_DEBUG:
        logger.info("[LC_DEBUG] " + msg, *args)


def _local_date_str(ts_ms: int) -> str:
    tz = LC_TZ.strip()
    if tz.startswith(("+", "-")) and len(tz) >= 3 and ":" in tz:
        sign = 1 if tz[0] == "+" else -1
        try:
            hh, mm = tz[1:].split(":", 1)
            offset_sec = sign * (int(hh) * 3600 + int(mm) * 60)
            sec = (ts_ms // 1000) + offset_sec
            return time.strftime("%Y-%m-%d", time.gmtime(sec))
        except Exception:
            pass
    return time.strftime("%Y-%m-%d", time.gmtime(ts_ms / 1000.0))


def _to_float(value) -> float:
    try:
        parsed = float(value)
        if parsed != parsed or parsed in (float("inf"), float("-inf")):
            return float("nan")
        return parsed
    except Exception:
        return float("nan")


def _parse_pack_out(value: str) -> Tuple[Optional[str], float, Optional[bool]]:
    floor_label, height_mm, door_open = None, float("nan"), None
    if not value:
        return floor_label, height_mm, door_open

    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            floor_label = parsed.get("floor_label") or parsed.get("fl")
            h_raw = parsed.get("height")
            if h_raw is None:
                h_raw = parsed.get("h")
            height_mm = _to_float(h_raw) if h_raw is not None else float("nan")

            if "door_open" in parsed:
                door_open = bool(parsed["door_open"])
            elif "door" in parsed:
                try:
                    door_open = bool(int(parsed["door"]))
                except Exception:
                    door_open = None
            elif "door_val" in parsed:
                door_open = str(parsed["door_val"]).strip().upper() == "OPEN"
            return floor_label, height_mm, door_open
    except Exception:
        pass

    parts: Dict[str, str] = {}
    for token in value.split("|"):
        if "=" in token:
            key, item_value = token.split("=", 1)
            parts[key] = item_value

    floor_label = parts.get("floor_label") or parts.get("fl")
    h_raw = parts.get("height") or parts.get("h")
    height_mm = _to_float(h_raw) if h_raw is not None else float("nan")

    if "door_open" in parts:
        try:
            door_open = bool(int(parts["door_open"]))
        except Exception:
            door_open = parts["door_open"].strip().lower() in ("true", "open", "1")
    elif "door" in parts:
        try:
            door_open = bool(int(parts["door"]))
        except Exception:
            door_open = None
    elif "door_val" in parts:
        door_open = parts["door_val"].strip().upper() == "OPEN"

    return floor_label, height_mm, door_open


def _movement(prev_h: float, h: float, threshold: float) -> bool:
    if math.isnan(prev_h) or math.isnan(h):
        return False
    return abs(h - prev_h) > threshold


def _door_key(date_str: str, device_id: str) -> str:
    return f"lc:{date_str}:{device_id}:door"


def _idle_key(date_str: str, device_id: str) -> str:
    return f"lc:{date_str}:{device_id}:idle_ms"


def _hinc(store_key: str, field: str, delta: int) -> None:
    bucket = _inmem.setdefault(store_key, {})
    bucket[field] = int(bucket.get(field, 0)) + int(delta)


def _hgetall(store_key: str) -> Dict[str, int]:
    return {key: int(value) for key, value in _inmem.get(store_key, {}).items()}


def _state_get(device_id: str) -> Dict[str, str]:
    return _state_inmem.get(device_id, {}).copy()


def _state_set(device_id: str, value: Dict[str, str]) -> None:
    _state_inmem[device_id] = value.copy()


def process_pack_out_sample(device_id: str, device_name: str, ts_ms: int, pack_out_str: str) -> None:
    if not LC_ENABLED:
        return

    floor_label, height, door_open = _parse_pack_out(pack_out_str)
    if floor_label is None and math.isnan(height) and door_open is None:
        return

    state = _state_get(device_id)
    last_ts = int(state.get("ts", "0") or "0")
    last_floor = state.get("floor")
    try:
        last_h = float(state.get("h")) if "h" in state else float("nan")
    except Exception:
        last_h = float("nan")
    last_door = state.get("door")
    last_door_bool = None if last_door is None or last_door == "" else (last_door == "1")

    if ts_ms <= last_ts:
        return

    date_str = _local_date_str(ts_ms)
    floor_for_bucket = floor_label or last_floor or "UNKNOWN"

    if last_door_bool in (False, 0) and door_open in (True, 1):
        _hinc(_door_key(date_str, device_id), floor_for_bucket, 1)
        _dbg("Door OPEN edge on %s floor=%s", device_name, floor_for_bucket)

    if not _movement(last_h, height, LC_MOVEMENT_THRESHOLD_MM):
        dt = ts_ms - last_ts
        if dt > 0 and last_ts > 0:
            _hinc(_idle_key(date_str, device_id), floor_for_bucket, dt)
            _dbg(
                "Idle +%dms on %s floor=%s (h_prev=%.1f h_now=%.1f thr=%.1f)",
                dt,
                device_name,
                floor_for_bucket,
                last_h,
                height,
                LC_MOVEMENT_THRESHOLD_MM,
            )

    _state_set(
        device_id,
        {
            "ts": str(ts_ms),
            "floor": floor_for_bucket,
            "h": "nan" if math.isnan(height) else str(height),
            "door": "1"
            if door_open in (True, 1)
            else ("0" if door_open in (False, 0) else (last_door or "")),
        },
    )


def flush_day_to_tb(date_str: str) -> int:
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
        idle_sec = {key: int(round(value / 1000.0)) for key, value in idle_ms.items()}

        payload = {
            "daily_floor_door_opens": door_counts,
            "daily_floor_idle_sec": idle_sec,
            "daily_floor_summary": {
                "date": date_str,
                "door_opens": door_counts,
                "idle_sec": idle_sec,
            },
        }

        url = f"{TB_BASE_URL}/api/plugins/telemetry/DEVICE/{device_id}/timeseries/ANY"
        body = {"ts": write_ts_ms, "values": {}}
        for key, value in payload.items():
            body["values"][key] = json.dumps(value, separators=(",", ":")) if isinstance(value, (dict, list)) else value

        response = requests.post(
            url,
            headers={"X-Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
            data=json.dumps(body),
            timeout=45,
        )
        if response.status_code >= 400:
            logger.error(
                "[LiveCounters] TB save_ts failed for %s (%s): %s",
                device_id,
                response.status_code,
                response.text,
            )
            continue

        flushed += 1
        _inmem.pop(_door_key(date_str, device_id), None)
        _inmem.pop(_idle_key(date_str, device_id), None)

    logger.info("[LiveCounters] Flushed %d device(s) for %s", flushed, date_str)
    return flushed
