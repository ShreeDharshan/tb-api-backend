import logging
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional
import time
import os
import requests

router = APIRouter()
logger = logging.getLogger("calculated_telemetry")

# === Multi-account credentials ===
THINGSBOARD_ACCOUNTS = {
    "account1": {
        "host": os.getenv("TB1_HOST"),
        "username": os.getenv("TB1_USERNAME"),
        "password": os.getenv("TB1_PASSWORD")
    },
    "account2": {
        "host": os.getenv("TB2_HOST"),
        "username": os.getenv("TB2_USERNAME"),
        "password": os.getenv("TB2_PASSWORD")
    }
}

def get_account_creds(account_key: str):
    creds = THINGSBOARD_ACCOUNTS.get(account_key)
    if not creds:
        raise HTTPException(status_code=400, detail=f"Invalid account key: {account_key}")
    return creds

def get_admin_token(account_key: str) -> str:
    creds = get_account_creds(account_key)
    response = requests.post(
        f"{creds['host']}/api/auth/login",
        json={"username": creds['username'], "password": creds['password']}
    )
    if response.status_code != 200:
        logger.error(f"Failed to login for account {account_key}: {response.text}")
        raise HTTPException(status_code=response.status_code, detail="Failed to login to ThingsBoard")
    return response.json().get("token")

# === State storage ===
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
async def calculate_telemetry(payload: TelemetryPayload, request: Request):
    """
    Processes incoming telemetry and calculates derived metrics.
    Supports multiple ThingsBoard accounts via X-TB-Account header.
    """
    logger.info("--- /calculated-telemetry/ invoked ---")
    logger.info(f"Payload: {payload}")

    account_key = request.headers.get("X-TB-Account", "account1")
    creds = get_account_creds(account_key)
    token = get_admin_token(account_key)
    host = creds['host']

    ts = payload.ts or int(time.time() * 1000)
    current_time = ts // 1000
    device = payload.device_token
    floor = int(payload.current_floor_index)

    # === Initialize state ===
    if device not in device_state:
        device_state[device] = {
            "last_floor": floor,
            "last_status": payload.lift_status,
            "last_door_state": payload.door_open,
            "last_timestamp": current_time
        }
        floor_door_counts[device] = {}
        floor_door_durations[device] = {}
    state = device_state[device]

    # === Door open counts ===
    if payload.door_open:
        floor_door_counts[device][floor] = floor_door_counts[device].get(floor, 0) + 1

        last_ts = state.get("last_timestamp", current_time)
        duration = current_time - last_ts
        floor_door_durations[device][floor] = floor_door_durations[device].get(floor, 0) + duration

    # Update state
    state.update({
        "last_floor": floor,
        "last_status": payload.lift_status,
        "last_door_state": payload.door_open,
        "last_timestamp": current_time
    })

    # === Construct calculated telemetry payload ===
    calculated = {
        "door_open_count_per_floor": floor_door_counts[device],
        "door_open_duration_per_floor": floor_door_durations[device]
    }

    url = f"{host}/api/v1/{device}/telemetry"
    headers = {"Content-Type": "application/json", "X-Authorization": f"Bearer {token}"}
    response = requests.post(url, headers=headers, json={"calculated": calculated})

    if response.status_code != 200:
        logger.error(f"Failed to send calculated telemetry: {response.text}")
        raise HTTPException(status_code=response.status_code, detail="Failed to send calculated telemetry")

    return {"status": "processed", "calculated": calculated}
