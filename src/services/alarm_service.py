import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from fastapi import HTTPException

from src.config import parse_tb_accounts
from src.core.auth import get_admin_jwt
from src.models.alarm import AlarmTelemetryPayload

logger = logging.getLogger("services.alarm")

THRESHOLDS: Dict[str, float] = {
    "humidity": 50.0,
    "temperature": 50.0,
    "x_jerk": 5.0,
    "y_jerk": 5.0,
    "z_jerk": 15.0,
    "x_vibe": 5.0,
    "y_vibe": 5.0,
    "z_vibe": 15.0,
    "sound_db": 80.0,
}

ZONE_MM = 2000.0
BUCKET_COUNT_THRESHOLD = 3
TOLERANCE_MM = 10.0
DOOR_OPEN_THRESHOLD_SEC = 15
HTTP_TIMEOUT = 12

_device_cache: Dict[str, str] = {}
_bucket_counts: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
_device_door_state: Dict[str, bool] = {}
_door_open_since: Dict[str, float] = {}
_floor_boundaries_cache: Dict[str, List[float]] = {}


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
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "open", "on"):
            return True
        if lowered in ("false", "0", "no", "closed", "off"):
            return False
    return None


def epoch_ms_from_any(ts: Optional[Union[int, str]]) -> int:
    if ts is None:
        return int(time.time() * 1000)
    if isinstance(ts, int):
        return ts if ts > 1_000_000_000_000 else ts * 1000
    if isinstance(ts, str):
        stripped = ts.strip()
        if stripped.isdigit():
            value = int(stripped)
            return value if value > 1_000_000_000_000 else value * 1000
        try:
            dt = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            pass
    return int(time.time() * 1000)


def _quoted(value: str) -> str:
    return requests.utils.quote(value, safe="")


def _accounts() -> Dict[str, str]:
    return parse_tb_accounts()


def _get_device_id(device_name: str, account_id: str) -> Optional[str]:
    cache_key = f"{account_id}:{device_name}"
    if cache_key in _device_cache:
        return _device_cache[cache_key]

    base = _accounts().get(account_id)
    if not base:
        logger.error("[DEVICE_LOOKUP] Unknown account_id=%s", account_id)
        return None

    jwt = get_admin_jwt(account_id, base)
    if not jwt:
        logger.error("[DEVICE_LOOKUP] No JWT for account=%s", account_id)
        return None

    url = f"{base}/api/tenant/devices?deviceName={_quoted(device_name)}"
    try:
        response = requests.get(url, headers={"X-Authorization": f"Bearer {jwt}"}, timeout=HTTP_TIMEOUT)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict) and data.get("id", {}).get("id"):
                device_id = data["id"]["id"]
                _device_cache[cache_key] = device_id
                return device_id
        logger.error("[DEVICE_LOOKUP] Failed %s | %s", response.status_code, response.text)
    except Exception as exc:
        logger.error("[DEVICE_LOOKUP] Exception: %s", exc)
    return None


def _get_floor_boundaries(device_id: str, account_id: str) -> Optional[List[float]]:
    cache_key = f"{account_id}:{device_id}"
    if cache_key in _floor_boundaries_cache:
        return _floor_boundaries_cache[cache_key]

    base = _accounts().get(account_id)
    if not base:
        return None

    jwt = get_admin_jwt(account_id, base)
    if not jwt:
        logger.warning("[ATTRIBUTES] No JWT; cannot fetch floor_boundaries")
        return None

    url = f"{base}/api/plugins/telemetry/DEVICE/{device_id}/values/attributes/SERVER_SCOPE"
    try:
        response = requests.get(url, headers={"X-Authorization": f"Bearer {jwt}"}, timeout=HTTP_TIMEOUT)
        if response.status_code != 200:
            logger.error("[ATTRIBUTES] Failed %s | %s", response.status_code, response.text)
            return None

        boundaries: Optional[List[float]] = None
        for attr in response.json() or []:
            if attr.get("key") == "floor_boundaries":
                value = attr.get("value")
                if isinstance(value, list):
                    boundaries = [float(item) for item in value]
                elif isinstance(value, str):
                    try:
                        parsed = json.loads(value)
                        if isinstance(parsed, list):
                            boundaries = [float(item) for item in parsed]
                    except Exception:
                        boundaries = [float(item.strip()) for item in value.split(",") if item.strip()]
                break

        if boundaries is not None:
            _floor_boundaries_cache[cache_key] = boundaries
        return boundaries
    except Exception as exc:
        logger.error("[ATTRIBUTES] Exception: %s", exc)
        return None


def _create_alarm_on_tb(
    device_name: str,
    alarm_type: str,
    ts_ms: int,
    severity: str,
    details: Dict[str, Any],
    account_id: str,
) -> None:
    base = _accounts().get(account_id)
    if not base:
        logger.warning("[ALARM] Unknown account %s", account_id)
        return

    device_id = _get_device_id(device_name, account_id)
    if not device_id:
        logger.warning("[ALARM] Could not resolve device ID for %s", device_name)
        return

    jwt = get_admin_jwt(account_id, base)
    if not jwt:
        logger.warning("[ALARM] No JWT for account %s", account_id)
        return

    alarm_payload = {
        "originator": {"entityType": "DEVICE", "id": device_id},
        "type": alarm_type,
        "severity": severity,
        "status": "ACTIVE_UNACK",
        "details": details or {},
        "startTs": ts_ms,
    }

    try:
        response = requests.post(
            f"{base}/api/alarm",
            headers={
                "X-Authorization": f"Bearer {jwt}",
                "Content-Type": "application/json",
            },
            json=alarm_payload,
            timeout=HTTP_TIMEOUT,
        )
        if 200 <= response.status_code < 300:
            logger.info("[ALARM] Created %s for %s", alarm_type, device_name)
        else:
            logger.error("[ALARM] Failed %s | %s", response.status_code, response.text)
    except Exception as exc:
        logger.error("[ALARM] Exception: %s", exc)


def _check_bucket_and_trigger(
    device: str,
    key: str,
    value: float,
    height: Optional[float],
    ts_ms: int,
    floor: str,
    account_id: str,
) -> Optional[Dict[str, Any]]:
    if height is None:
        return None

    if device not in _bucket_counts:
        _bucket_counts[device] = {}
    if key not in _bucket_counts[device]:
        _bucket_counts[device][key] = []

    buckets = _bucket_counts[device][key]
    for bucket in buckets:
        if abs(bucket["center"] - height) <= ZONE_MM:
            bucket["count"] += 1
            if bucket["count"] >= BUCKET_COUNT_THRESHOLD:
                alarm_type = f"{key} Alarm"
                details = {
                    "value": value,
                    "threshold": THRESHOLDS[key],
                    "floor": floor,
                    "height_zone": f"{bucket['center'] - ZONE_MM:.1f} to {bucket['center'] + ZONE_MM:.1f}",
                    "count": bucket["count"],
                }
                _create_alarm_on_tb(device, alarm_type, ts_ms, "MINOR", details, account_id)
                buckets.remove(bucket)
                return {
                    "type": alarm_type,
                    "value": value,
                    "threshold": THRESHOLDS[key],
                    "severity": "MINOR",
                    "floor": floor,
                    "height_zone": details["height_zone"],
                    "count": bucket["count"],
                }
            return None

    buckets.append({"center": height, "count": 1})
    return None


def _process_door_alarm(
    device_name: str,
    door_open_input: Optional[bool],
    floor: str,
    ts_ms: int,
    account_id: str,
) -> None:
    now = time.monotonic()
    door_open = door_open_input
    if door_open is None:
        door_open = _device_door_state.get(device_name, False)
    else:
        _device_door_state[device_name] = door_open

    if door_open:
        if device_name not in _door_open_since:
            _door_open_since[device_name] = now
        else:
            duration = now - _door_open_since[device_name]
            if duration >= DOOR_OPEN_THRESHOLD_SEC:
                _create_alarm_on_tb(
                    device_name,
                    "Door Open Too Long",
                    ts_ms,
                    "MAJOR",
                    {"duration_sec": int(duration), "floor": floor},
                    account_id,
                )
                _door_open_since[device_name] = now
    else:
        _door_open_since.pop(device_name, None)


def _floor_mismatch_detected(
    height: Optional[float],
    current_floor_index: Optional[int],
    floor_boundaries: Optional[List[float]],
) -> Tuple[bool, float, float]:
    if height is None or current_floor_index is None or floor_boundaries is None:
        return False, 0.0, 0.0

    try:
        if current_floor_index < 0 or current_floor_index >= len(floor_boundaries):
            return False, 0.0, 0.0
        floor_center = float(floor_boundaries[current_floor_index])
        deviation = height - floor_center
        return abs(deviation) > TOLERANCE_MM, deviation, floor_center
    except Exception as exc:
        logger.error("[FLOOR] floor mismatch calc failed: %s", exc)
        return False, 0.0, 0.0


def process_alarm_payload(payload: AlarmTelemetryPayload, account_id: str) -> Dict[str, Any]:
    accounts = _accounts()
    if account_id not in accounts:
        raise HTTPException(status_code=400, detail="Invalid account ID")

    ts_ms = epoch_ms_from_any(payload.timestamp)
    height = parse_float(payload.height)
    current_floor_index = parse_int(payload.current_floor_index)

    door_bool = parse_bool(payload.door_open)
    if door_bool is None:
        door_bool = _device_door_state.get(payload.deviceName, False)

    triggered: List[Dict[str, Any]] = []

    try:
        for key in ("humidity", "temperature"):
            value = parse_float(getattr(payload, key))
            if value is not None and key in THRESHOLDS and value > THRESHOLDS[key]:
                summary = {
                    "type": f"{key.capitalize()} Alarm",
                    "value": value,
                    "threshold": THRESHOLDS[key],
                    "severity": "WARNING",
                    "floor": payload.floor,
                }
                triggered.append(summary)
                _create_alarm_on_tb(
                    payload.deviceName,
                    summary["type"],
                    ts_ms,
                    "WARNING",
                    {
                        "value": value,
                        "threshold": THRESHOLDS[key],
                        "floor": payload.floor,
                    },
                    account_id,
                )

        for key in ("x_jerk", "y_jerk", "z_jerk", "x_vibe", "y_vibe", "z_vibe", "sound_db"):
            value = parse_float(getattr(payload, key))
            threshold = THRESHOLDS.get(key)
            if value is not None and threshold is not None and value > threshold:
                bucket_alarm = _check_bucket_and_trigger(
                    payload.deviceName,
                    key,
                    value,
                    height,
                    ts_ms,
                    payload.floor,
                    account_id,
                )
                if bucket_alarm is not None:
                    triggered.append(bucket_alarm)

        if current_floor_index is not None and door_bool:
            device_id = _get_device_id(payload.deviceName, account_id)
            if device_id:
                floor_boundaries = _get_floor_boundaries(device_id, account_id)
                mismatch, deviation, floor_center = _floor_mismatch_detected(
                    height,
                    current_floor_index,
                    floor_boundaries,
                )
                if mismatch:
                    position = "above" if deviation > 0 else "below"
                    summary = {
                        "type": "Floor Mismatch Alarm",
                        "value": height,
                        "severity": "CRITICAL",
                        "position": position,
                        "floor_index": current_floor_index,
                    }
                    triggered.append(summary)
                    _create_alarm_on_tb(
                        payload.deviceName,
                        "Floor Mismatch Alarm",
                        ts_ms,
                        "CRITICAL",
                        {
                            "reported_index": current_floor_index,
                            "height": height,
                            "floor_center": floor_center,
                            "deviation_mm": abs(deviation),
                            "position": position,
                        },
                        account_id,
                    )

        _process_door_alarm(payload.deviceName, door_bool, payload.floor, ts_ms, account_id)

        logger.info("[RESULT] %s alarms_triggered=%s", payload.deviceName, len(triggered))
        return {"status": "processed", "alarms_triggered": triggered}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[ERROR] Exception during alarm processing: %s", exc)
        raise HTTPException(status_code=500, detail="Alarm processing failed")
