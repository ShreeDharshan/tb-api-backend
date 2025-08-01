import logging
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
import time

router = APIRouter()
logger = logging.getLogger("calculated_telemetry")

# State storage
device_state = {}
floor_door_counts = {}
floor_door_durations = {}

class TelemetryPayload(BaseModel):
    deviceName: str
    device_token: str
    current_floor_index: int
    lift_status: str
    door_open: Optional[bool] = False
    ts: Optional[int] = None

@router.post("/calculated-telemetry/")
async def calculate_telemetry(payload: TelemetryPayload):
    logger.info("--- /calculated-telemetry/ invoked ---")
    logger.info(f"Payload: {payload}")

    ts = payload.ts or int(time.time() * 1000)
    current_time = ts // 1000
    device = payload.device_token
    floor = int(payload.current_floor_index)

    # Initialize state
    if device not in device_state:
        device_state[device] = {
            "last_idle_home_ts": None,
            "total_idle_home": 0,
            "last_idle_outside_ts": None,
            "total_idle_outside": 0,
            "last_status": None,
            "last_floor": floor
        }

    if device not in floor_door_counts:
        floor_door_counts[device] = {}

    if device not in floor_door_durations:
        floor_door_durations[device] = {}

    state = device_state[device]
    home_floor = 1  # This can be dynamically fetched if needed

    # ----- Idle calculation -----
    if payload.lift_status.lower() == "idle":
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

    # ----- Door tracking -----
    if floor not in floor_door_counts[device]:
        floor_door_counts[device][floor] = 0
    if floor not in floor_door_durations[device]:
        floor_door_durations[device][floor] = 0

    if payload.door_open:
        floor_door_counts[device][floor] += 1
        last_ts_key = f"last_open_ts_{floor}"
        if last_ts_key not in state:
            state[last_ts_key] = current_time
    else:
        last_ts_key = f"last_open_ts_{floor}"
        if last_ts_key in state:
            open_duration = current_time - state[last_ts_key]
            floor_door_durations[device][floor] += open_duration
            del state[last_ts_key]

    calculated_values = {
        "idle_home_streak": (
            current_time - state["last_idle_home_ts"] if state["last_idle_home_ts"] else 0
        ),
        "total_idle_home_seconds": state["total_idle_home"],
        "idle_outside_home_streak": (
            current_time - state["last_idle_outside_ts"] if state["last_idle_outside_ts"] else 0
        ),
        "total_idle_outside_home_seconds": state["total_idle_outside"],
        "door_open_count_per_floor": floor_door_counts[device],
        "door_open_duration_per_floor": floor_door_durations[device],
    }

    return {
        "status": "success",
        "calculated": calculated_values
    }
