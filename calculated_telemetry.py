import logging
import os
import json
import time
from typing import Optional, Dict, Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from config import TB_ACCOUNTS, IDLE_FLUSH_INTERVAL_SEC
from thingsboard_auth import get_admin_jwt  # kept for future TB calls / parity

router = APIRouter()
logger = logging.getLogger("calculated_telemetry")

# ---------------------------------------------------------------------------
# Runtime in-memory state
# ---------------------------------------------------------------------------
# per-device global info
device_state: Dict[str, Dict[str, Any]] = {}
# per-device per-floor door open count
floor_door_counts: Dict[str, Dict[int, int]] = {}
# per-device per-floor door open duration
floor_door_durations: Dict[str, Dict[int, int]] = {}
# per-device per-floor idle seconds (the heavy one we throttle)
floor_idle_seconds: Dict[str, Dict[int, int]] = {}
# per-device last time we sent the heavy map to TB
idle_last_flush_ts: Dict[str, int] = {}


class TelemetryPayload(BaseModel):
    # this is what TB sends from “Build Post Processing Payload”
    deviceName: str = Field(...)
    device_token: str = Field(...)
    current_floor_index: int = Field(...)
    lift_status: str = Field(...)
    door_open: Optional[bool] = Field(default=False)
    ts: Optional[int] = Field(default=None)
    # TB can pass home_floor (we will prefer this)
    home_floor: Optional[int] = Field(default=None)


@router.post("/calculated-telemetry/")
async def calculated_telemetry(
    payload: TelemetryPayload,
    x_account_id: str = Header(..., alias="X-Account-ID"),
):
    """
    Old JSON-style endpoint (not pack_raw / not pack_out).
    We always update in-memory counters on every call.
    We only SEND BACK idle_seconds_per_floor every IDLE_FLUSH_INTERVAL_SEC seconds.
    """
    # -------- 1. validate account --------
    if x_account_id not in TB_ACCOUNTS:
        raise HTTPException(status_code=400, detail="Invalid account ID")

    # -------- 2. timestamps --------
    ts_ms = payload.ts or int(time.time() * 1000)
    now_sec = ts_ms // 1000

    # -------- 3. device key (per TB + per device) --------
    device_key = f"{x_account_id}:{payload.device_token}"
    floor = int(payload.current_floor_index)

    # -------- 4. ensure dicts exist --------
    if device_key not in device_state:
        device_state[device_key] = {
            "last_idle_home_ts": None,
            "total_idle_home": 0,
            "last_idle_outside_ts": None,
            "total_idle_outside": 0,
            # for generic per-floor idle
            "last_idle_ts": None,
            "last_idle_floor": None,
        }
    if device_key not in floor_door_counts:
        floor_door_counts[device_key] = {}
    if device_key not in floor_door_durations:
        floor_door_durations[device_key] = {}
    if device_key not in floor_idle_seconds:
        floor_idle_seconds[device_key] = {}
    if device_key not in idle_last_flush_ts:
        # 0 means “first message can flush if we want”
        idle_last_flush_ts[device_key] = 0

    state = device_state[device_key]

    # -------- 5. pick home floor --------
    home_floor = payload.home_floor if payload.home_floor is not None else 1

    # -------- 6. detect idle --------
    is_idle = (payload.lift_status.lower() == "idle") or bool(payload.door_open)

    # === A) keep old home vs outside-home counters ===
    if is_idle:
        if floor == home_floor:
            if state["last_idle_home_ts"] is None:
                state["last_idle_home_ts"] = now_sec
            else:
                elapsed = now_sec - state["last_idle_home_ts"]
                if elapsed > 0:
                    state["total_idle_home"] += elapsed
                state["last_idle_home_ts"] = now_sec
            # break outside streak
            state["last_idle_outside_ts"] = None
        else:
            if state["last_idle_outside_ts"] is None:
                state["last_idle_outside_ts"] = now_sec
            else:
                elapsed = now_sec - state["last_idle_outside_ts"]
                if elapsed > 0:
                    state["total_idle_outside"] += elapsed
                state["last_idle_outside_ts"] = now_sec
            # break home streak
            state["last_idle_home_ts"] = None
    else:
        # movement breaks both streaks
        state["last_idle_home_ts"] = None
        state["last_idle_outside_ts"] = None

    # === B) per-floor idle seconds (new) ===
    # rule: time between 2 idle samples belongs to the *previous* floor
    if is_idle:
        last_idle_ts = state.get("last_idle_ts")
        last_idle_floor = state.get("last_idle_floor")
        if last_idle_ts is not None and last_idle_floor == floor:
            delta = now_sec - int(last_idle_ts)
            if delta > 0:
                floor_idle_seconds[device_key][floor] = (
                    floor_idle_seconds[device_key].get(floor, 0) + delta
                )
            # refresh
            state["last_idle_ts"] = now_sec
        else:
            # just started idling here
            state["last_idle_ts"] = now_sec
            state["last_idle_floor"] = floor
    else:
        state["last_idle_ts"] = None
        state["last_idle_floor"] = None

    # === C) door counts & durations (existing logic) ===
    if floor not in floor_door_counts[device_key]:
        floor_door_counts[device_key][floor] = 0
    if floor not in floor_door_durations[device_key]:
        floor_door_durations[device_key][floor] = 0

    if payload.door_open:
        # count
        floor_door_counts[device_key][floor] += 1
        last_ts_key = f"last_open_ts_{floor}"
        if last_ts_key not in state:
            state[last_ts_key] = now_sec
    else:
        last_ts_key = f"last_open_ts_{floor}"
        if last_ts_key in state:
            open_duration = now_sec - int(state[last_ts_key])
            if open_duration > 0:
                floor_door_durations[device_key][floor] += open_duration
            del state[last_ts_key]

    # -------- 7. should we send heavy map now? --------
    last_flush = idle_last_flush_ts.get(device_key, 0)
    should_flush_idle = (now_sec - last_flush) >= IDLE_FLUSH_INTERVAL_SEC
    if should_flush_idle:
        idle_last_flush_ts[device_key] = now_sec

    # -------- 8. build response --------
    calculated_values: Dict[str, Any] = {
        # old fields
        "idle_home_streak": (
            now_sec - state["last_idle_home_ts"] if state["last_idle_home_ts"] else 0
        ),
        "total_idle_home_seconds": state["total_idle_home"],
        "idle_outside_home_streak": (
            now_sec - state["last_idle_outside_ts"] if state["last_idle_outside_ts"] else 0
        ),
        "total_idle_outside_home_seconds": state["total_idle_outside"],
        "door_open_count_per_floor": floor_door_counts[device_key],
        "door_open_duration_per_floor": floor_door_durations[device_key],
        "home_floor": home_floor,
        "is_idle": is_idle,
        "last_idle_floor": state.get("last_idle_floor"),
        # flush info
        "idle_flush": "full" if should_flush_idle else "skipped",
        "idle_flush_interval_sec": IDLE_FLUSH_INTERVAL_SEC,
    }

    # only sometimes send the heavy one
    if should_flush_idle:
        calculated_values["idle_seconds_per_floor"] = floor_idle_seconds[device_key]

    return {
        "status": "success",
        "calculated": calculated_values,
    }
