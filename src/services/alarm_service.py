import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

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
    "sound_db": 60.0,
    "batterySoC_low": 20.0,
}

BUCKET_HALF_WIDTH_CM = 5.0
BUCKET_COUNT_THRESHOLD = 3
DOOR_OPEN_THRESHOLD_SEC = 15
HTTP_TIMEOUT = 12

_device_cache: Dict[str, str] = {}
_bucket_counts: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
_device_door_state: Dict[str, bool] = {}
_door_open_since: Dict[str, float] = {}


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
    height_cm: Optional[float],
    ts_ms: int,
    floor: str,
    account_id: str,
) -> Optional[Dict[str, Any]]:
    if height_cm is None:
        return None

    if device not in _bucket_counts:
        _bucket_counts[device] = {}
    if key not in _bucket_counts[device]:
        _bucket_counts[device][key] = []

    buckets = _bucket_counts[device][key]
    for bucket in buckets:
        if abs(bucket["center"] - height_cm) <= BUCKET_HALF_WIDTH_CM:
            bucket["count"] += 1
            if bucket["count"] >= BUCKET_COUNT_THRESHOLD:
                alarm_type = f"{key} Alarm"
                details = {
                    "value": value,
                    "threshold": THRESHOLDS[key],
                    "floor": floor,
                    "height_zone": (
                        f"{bucket['center'] - BUCKET_HALF_WIDTH_CM:.1f} to "
                        f"{bucket['center'] + BUCKET_HALF_WIDTH_CM:.1f} cm"
                    ),
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

    buckets.append({"center": height_cm, "count": 1})
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


def process_alarm_payload(payload: AlarmTelemetryPayload, account_id: str) -> Dict[str, Any]:
    accounts = _accounts()
    if account_id not in accounts:
        raise HTTPException(status_code=400, detail="Invalid account ID")

    ts_ms = epoch_ms_from_any(payload.timestamp)
    height_cm = parse_float(payload.height_cm)
    if height_cm is None:
        height_cm = parse_float(payload.height)

    door_bool = parse_bool(payload.door_open)
    if door_bool is None:
        door_bool = _device_door_state.get(payload.deviceName, False)

    triggered: List[Dict[str, Any]] = []

    try:
        for key in ("humidity", "temperature", "sound_db"):
            value = parse_float(getattr(payload, key))
            if value is not None and key in THRESHOLDS and value > THRESHOLDS[key]:
                alarm_name = "Sound Alarm" if key == "sound_db" else f"{key.capitalize()} Alarm"
                details = {
                    "value": value,
                    "threshold": THRESHOLDS[key],
                    "floor": payload.floor,
                }
                if key == "sound_db":
                    mic_peak = parse_float(payload.microphone_peak_dB)
                    mic_rms = parse_float(payload.microphone_rms_dB)
                    if mic_peak is not None:
                        details["microphone_peak_dB"] = mic_peak
                    if mic_rms is not None:
                        details["microphone_rms_dB"] = mic_rms

                summary = {
                    "type": alarm_name,
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
                    details,
                    account_id,
                )

        battery_soc = parse_float(payload.batterySoC)
        battery_threshold = THRESHOLDS.get("batterySoC_low")
        if battery_soc is not None and battery_threshold is not None and battery_soc < battery_threshold:
            summary = {
                "type": "Battery Low Alarm",
                "value": battery_soc,
                "threshold": battery_threshold,
                "severity": "MAJOR",
                "floor": payload.floor,
            }
            triggered.append(summary)
            _create_alarm_on_tb(
                payload.deviceName,
                summary["type"],
                ts_ms,
                "MAJOR",
                {
                    "batterySoC": battery_soc,
                    "threshold": battery_threshold,
                    "floor": payload.floor,
                },
                account_id,
            )

        for key in ("x_jerk", "y_jerk", "z_jerk", "x_vibe", "y_vibe", "z_vibe"):
            value = parse_float(getattr(payload, key))
            threshold = THRESHOLDS.get(key)
            if value is not None and threshold is not None and value > threshold:
                bucket_alarm = _check_bucket_and_trigger(
                    payload.deviceName,
                    key,
                    value,
                    height_cm,
                    ts_ms,
                    payload.floor,
                    account_id,
                )
                if bucket_alarm is not None:
                    triggered.append(bucket_alarm)

        _process_door_alarm(payload.deviceName, door_bool, payload.floor, ts_ms, account_id)

        logger.info("[RESULT] %s alarms_triggered=%s", payload.deviceName, len(triggered))
        return {"status": "processed", "alarms_triggered": triggered}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[ERROR] Exception during alarm processing: %s", exc)
        raise HTTPException(status_code=500, detail="Alarm processing failed")
