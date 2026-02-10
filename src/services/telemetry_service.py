import logging
import time
from typing import Any, Dict

from fastapi import HTTPException

from src.config import parse_tb_accounts
from src.models.telemetry import CalculatedTelemetryPayload

logger = logging.getLogger("services.telemetry")

_device_state: Dict[str, Dict[str, Any]] = {}
_floor_door_counts: Dict[str, Dict[int, int]] = {}
_floor_door_durations: Dict[str, Dict[int, int]] = {}


def process_calculated_telemetry(payload: CalculatedTelemetryPayload, account_id: str) -> dict:
    accounts = parse_tb_accounts()
    if account_id not in accounts:
        raise HTTPException(status_code=400, detail="Invalid account ID")

    ts = payload.ts or int(time.time() * 1000)
    current_time = ts // 1000
    device_key = f"{account_id}:{payload.device_token}"
    floor = int(payload.current_floor_index)

    if device_key not in _device_state:
        _device_state[device_key] = {
            "last_idle_home_ts": None,
            "total_idle_home": 0,
            "last_idle_outside_ts": None,
            "total_idle_outside": 0,
            "last_status": None,
            "last_floor": floor,
        }

    if device_key not in _floor_door_counts:
        _floor_door_counts[device_key] = {}

    if device_key not in _floor_door_durations:
        _floor_door_durations[device_key] = {}

    state = _device_state[device_key]
    home_floor = 1

    is_idle = (payload.lift_status.lower() == "idle") or bool(payload.door_open)

    if is_idle:
        if floor == home_floor:
            if state["last_idle_home_ts"] is None:
                state["last_idle_home_ts"] = current_time
            else:
                elapsed = current_time - state["last_idle_home_ts"]
                state["total_idle_home"] += elapsed
                state["last_idle_home_ts"] = current_time
            state["last_idle_outside_ts"] = None
        else:
            if state["last_idle_outside_ts"] is None:
                state["last_idle_outside_ts"] = current_time
            else:
                elapsed = current_time - state["last_idle_outside_ts"]
                state["total_idle_outside"] += elapsed
                state["last_idle_outside_ts"] = current_time
            state["last_idle_home_ts"] = None
    else:
        state["last_idle_home_ts"] = None
        state["last_idle_outside_ts"] = None

    if floor not in _floor_door_counts[device_key]:
        _floor_door_counts[device_key][floor] = 0
    if floor not in _floor_door_durations[device_key]:
        _floor_door_durations[device_key][floor] = 0

    if payload.door_open:
        _floor_door_counts[device_key][floor] += 1
        last_ts_key = f"last_open_ts_{floor}"
        if last_ts_key not in state:
            state[last_ts_key] = current_time
    else:
        last_ts_key = f"last_open_ts_{floor}"
        if last_ts_key in state:
            open_duration = current_time - state[last_ts_key]
            _floor_door_durations[device_key][floor] += open_duration
            del state[last_ts_key]

    calculated_values = {
        "idle_home_streak": current_time - state["last_idle_home_ts"] if state["last_idle_home_ts"] else 0,
        "total_idle_home_seconds": state["total_idle_home"],
        "idle_outside_home_streak": current_time - state["last_idle_outside_ts"]
        if state["last_idle_outside_ts"]
        else 0,
        "total_idle_outside_home_seconds": state["total_idle_outside"],
        "door_open_count_per_floor": _floor_door_counts[device_key],
        "door_open_duration_per_floor": _floor_door_durations[device_key],
    }

    return {"status": "success", "calculated": calculated_values}
