import logging
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from typing import Optional
import time
import os
import json
import requests

router = APIRouter()
logger = logging.getLogger("calculated_telemetry")

# === Load Multi-Account Configuration ===
try:
    ACCOUNTS = json.loads(os.getenv("TB_ACCOUNTS", '{}'))
    if not isinstance(ACCOUNTS, dict):
        raise ValueError("TB_ACCOUNTS must be a JSON object")
except json.JSONDecodeError:
    raise RuntimeError("Invalid JSON format for TB_ACCOUNTS environment variable")

logger.info(f"[INIT] Loaded ThingsBoard accounts: {list(ACCOUNTS.keys())}")

# === State storage (per account) ===
device_state = {}  # { "account:device_token": {...} }
floor_door_counts = {}  # { "account:device_token": {...} }
floor_door_durations = {}  # { "account:device_token": {...} }

class TelemetryPayload(BaseModel):
    deviceName: str
    device_token: str
    current_floor_index: int
    lift_status: str
    door_open: Optional[bool] = False
    ts: Optional[int] = None
    alarm_count: Optional[int] = 0
    asset_id: Optional[str] = None  # Asset ID for updating attribute

def update_alarm_flag(account_id: str, asset_id: str, alarm_count: int):
    """Updates the has_critical_alarm attribute for the building asset."""
    if not asset_id:
        logger.warning("[update_alarm_flag] Missing asset_id, skipping update.")
        return

    base_url = ACCOUNTS[account_id]
    url = f"{base_url}/api/plugins/telemetry/ASSET/{asset_id}/SERVER_SCOPE"
    headers = {
        "Content-Type": "application/json",
        # Use a backend token from environment or service account
        "X-Authorization": f"Bearer {os.getenv('TB_BACKEND_TOKEN', '')}"
    }
    data = {"has_critical_alarm": alarm_count > 0}

    try:
        resp = requests.post(url, json=data, headers=headers, timeout=5)
        resp.raise_for_status()
        logger.info(f"[update_alarm_flag] Updated has_critical_alarm={data['has_critical_alarm']} for asset {asset_id}")
    except requests.RequestException as e:
        logger.error(f"[update_alarm_flag] Failed to update attribute: {e}")

@router.post("/calculated-telemetry/")
async def calculate_telemetry(
    payload: TelemetryPayload,
    x_account_id: str = Header(...)
):
    logger.info("--- /calculated-telemetry/ invoked ---")
    logger.info(f"Payload: {payload}")

    if x_account_id not in ACCOUNTS:
        raise HTTPException(status_code=400, detail="Invalid account ID")

    ts = payload.ts or int(time.time() * 1000)
    current_time = ts // 1000
    device_key = f"{x_account_id}:{payload.device_token}"
    floor = int(payload.current_floor_index)

    # Initialize state
    if device_key not in device_state:
        device_state[device_key] = {
            "last_idle_home_ts": None,
            "total_idle_home": 0,
            "last_idle_outside_ts": None,
            "total_idle_outside": 0,
            "last_status": None,
            "last_floor": floor
        }

    if device_key not in floor_door_counts:
        floor_door_counts[device_key] = {}

    if device_key not in floor_door_durations:
        floor_door_durations[device_key] = {}

    state = device_state[device_key]
    home_floor = 1  # TODO: Fetch dynamically if required

    # Treat lift as idle if status is "Idle" OR door is open
    is_idle = (payload.lift_status.lower() == "idle") or payload.door_open

    # ----- Idle calculation -----
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

    # ----- Door tracking -----
    if floor not in floor_door_counts[device_key]:
        floor_door_counts[device_key][floor] = 0
    if floor not in floor_door_durations[device_key]:
        floor_door_durations[device_key][floor] = 0

    if payload.door_open:
        floor_door_counts[device_key][floor] += 1
        last_ts_key = f"last_open_ts_{floor}"
        if last_ts_key not in state:
            state[last_ts_key] = current_time
    else:
        last_ts_key = f"last_open_ts_{floor}"
        if last_ts_key in state:
            open_duration = current_time - state[last_ts_key]
            floor_door_durations[device_key][floor] += open_duration
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
        "door_open_count_per_floor": floor_door_counts[device_key],
        "door_open_duration_per_floor": floor_door_durations[device_key],
    }

    update_alarm_flag(
        account_id=x_account_id,
        asset_id=payload.asset_id,
        alarm_count=payload.alarm_count or 0
    )

    return {
        "status": "success",
        "calculated": calculated_values
    }
