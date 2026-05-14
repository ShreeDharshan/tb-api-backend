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


def _parse_home_floor(value: Any) -> int:
    try:
        if value is None or value == "":
            return 1
        return int(value)
    except (TypeError, ValueError):
        logger.warning("Invalid home_floor=%r; falling back to 1", value)
        return 1


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
            "idle_home_start_ts": None,
            "total_idle_home": 0,
            "idle_outside_start_ts": None,
            "total_idle_outside": 0,
            "last_update_ts": current_time,
            "prev_door_open": False,
            "door_open_start_ts": None,
            "door_open_start_floor": None,
            "last_floor": floor,
        }

    if device_key not in _floor_door_counts:
        _floor_door_counts[device_key] = {}

    if device_key not in _floor_door_durations:
        _floor_door_durations[device_key] = {}

    state = _device_state[device_key]
    home_floor = _parse_home_floor(payload.home_floor)

    is_idle = (payload.lift_status.lower() == "idle") or bool(payload.door_open)

    elapsed = max(0, current_time - int(state.get("last_update_ts", current_time)))
    if state.get("idle_home_start_ts") is not None:
        state["total_idle_home"] += elapsed
    if state.get("idle_outside_start_ts") is not None:
        state["total_idle_outside"] += elapsed

    if is_idle:
        if floor == home_floor:
            if state["idle_home_start_ts"] is None:
                state["idle_home_start_ts"] = current_time
            state["idle_outside_start_ts"] = None
        else:
            if state["idle_outside_start_ts"] is None:
                state["idle_outside_start_ts"] = current_time
            state["idle_home_start_ts"] = None
    else:
        state["idle_home_start_ts"] = None
        state["idle_outside_start_ts"] = None

    if floor not in _floor_door_counts[device_key]:
        _floor_door_counts[device_key][floor] = 0
    if floor not in _floor_door_durations[device_key]:
        _floor_door_durations[device_key][floor] = 0

    prev_door_open = bool(state.get("prev_door_open", False))
    curr_door_open = bool(payload.door_open)

    if curr_door_open and not prev_door_open:
        _floor_door_counts[device_key][floor] += 1
        state["door_open_start_ts"] = current_time
        state["door_open_start_floor"] = floor
    elif not curr_door_open and prev_door_open:
        open_start_ts = state.get("door_open_start_ts")
        open_start_floor = state.get("door_open_start_floor")
        if open_start_ts is not None:
            duration = max(0, current_time - int(open_start_ts))
            duration_floor = floor if open_start_floor is None else int(open_start_floor)
            if duration_floor not in _floor_door_durations[device_key]:
                _floor_door_durations[device_key][duration_floor] = 0
            _floor_door_durations[device_key][duration_floor] += duration
        state["door_open_start_ts"] = None
        state["door_open_start_floor"] = None

    state["prev_door_open"] = curr_door_open
    state["last_update_ts"] = current_time

    calculated_values = {
        "idle_home_streak": current_time - state["idle_home_start_ts"] if state["idle_home_start_ts"] else 0,
        "total_idle_home_seconds": state["total_idle_home"],
        "idle_outside_home_streak": current_time - state["idle_outside_start_ts"]
        if state["idle_outside_start_ts"]
        else 0,
        "total_idle_outside_home_seconds": state["total_idle_outside"],
        "door_open_count_per_floor": _floor_door_counts[device_key],
        "door_open_duration_per_floor": _floor_door_durations[device_key],
    }

    return {"status": "success", "calculated": calculated_values}
