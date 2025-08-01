from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict
import requests
import os
import logging
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()

THINGSBOARD_HOST = "https://thingsboard.cloud"
admin_token_cache = {"token": None, "expiry": 0}

# Device state structure
device_state: Dict[str, Dict] = {}

class CalculatedTelemetryPayload(BaseModel):
    deviceName: str
    current_floor_index: int
    lift_status: Optional[str] = None
    door_open: Optional[bool] = None
    ts: Optional[int] = None

# --- Helpers ---
def get_admin_token():
    if admin_token_cache["token"] and admin_token_cache["expiry"] > time.time():
        return admin_token_cache["token"]
    
    url = f"{THINGSBOARD_HOST}/api/auth/login"
    credentials = {
        "username": os.getenv("TB_ADMIN_USER"),
        "password": os.getenv("TB_ADMIN_PASS")
    }
    resp = requests.post(url, json=credentials)
    if resp.status_code != 200:
        raise HTTPException(status_code=500, detail="Admin login failed")
    
    token = resp.json()["token"]
    expires_in = resp.json().get("refreshTokenExp", 3600)
    admin_token_cache["token"] = token
    admin_token_cache["expiry"] = time.time() + (expires_in / 1000) - 60
    return token

def get_device_id(device_name: str) -> Optional[str]:
    url = f"{THINGSBOARD_HOST}/api/tenant/devices?deviceName={device_name}"
    token = get_admin_token()
    res = requests.get(url, headers={"X-Authorization": f"Bearer {token}"})
    
    if res.status_code == 200:
        try:
            return res.json()["id"]["id"]
        except Exception:
            return None
    return None

def get_home_floor(device_id: str) -> Optional[int]:
    url = f"{THINGSBOARD_HOST}/api/plugins/telemetry/DEVICE/{device_id}/values/attributes/SERVER_SCOPE"
    token = get_admin_token()
    res = requests.get(url, headers={"X-Authorization": f"Bearer {token}"})
    
    if res.status_code == 200:
        try:
            for attr in res.json():
                if attr.get("key") == "home_floor":
                    return int(attr.get("value"))
                if attr.get("key") == "ss_home_floor":
                    return int(attr.get("value"))
        except Exception:
            return None
    return None

# --- API Endpoint ---
@router.post("/calculated-telemetry/")
async def calculated_telemetry(payload: CalculatedTelemetryPayload):
    logger.info("--- /calculated-telemetry/ invoked ---")
    logger.info(f"Payload: {payload}")

    ts = payload.ts or int(time.time() * 1000)
    device_id = get_device_id(payload.deviceName)
    if not device_id:
        return {"status": "error", "msg": f"No device ID for {payload.deviceName}"}

    home_floor = get_home_floor(device_id)
    if home_floor is None:
        return {"status": "error", "msg": "home_floor attribute not found"}

    # Initialize state if new device
    if payload.deviceName not in device_state:
        device_state[payload.deviceName] = {
            "last_idle_outside_ts": None,
            "total_idle_outside": 0,
            "last_idle_home_ts": None,
            "total_idle_home": 0,
            "door_open_count": {},
            "door_open_start": {},
            "door_open_duration": {}
        }

    state = device_state[payload.deviceName]
    current_time = ts // 1000
    floor = str(payload.current_floor_index)

    # --- Idle time calculation ---
    if payload.lift_status and payload.lift_status.lower() == "idle":
        if int(payload.current_floor_index) != home_floor:
            if state["last_idle_outside_ts"] is None:
                state["last_idle_outside_ts"] = current_time
            else:
                elapsed = current_time - state["last_idle_outside_ts"]
                state["total_idle_outside"] += elapsed
                state["last_idle_outside_ts"] = current_time
        else:
            if state["last_idle_home_ts"] is None:
                state["last_idle_home_ts"] = current_time
            else:
                elapsed = current_time - state["last_idle_home_ts"]
                state["total_idle_home"] += elapsed
                state["last_idle_home_ts"] = current_time
    else:
        state["last_idle_outside_ts"] = None
        state["last_idle_home_ts"] = None

    # --- Door open/close tracking ---
    if payload.door_open:
        if state["door_open_start"].get(floor) is None:
            state["door_open_start"][floor] = current_time
            state["door_open_count"][floor] = state["door_open_count"].get(floor, 0) + 1
    else:
        if state["door_open_start"].get(floor):
            elapsed = current_time - state["door_open_start"][floor]
            state["door_open_duration"][floor] = state["door_open_duration"].get(floor, 0) + elapsed
            state["door_open_start"][floor] = None

    return {
        "status": "success",
        "calculated": {
            "idle_outside_home_streak": (
                current_time - state["last_idle_outside_ts"] if state["last_idle_outside_ts"] else 0
            ),
            "total_idle_outside_home_seconds": state["total_idle_outside"],
            "idle_home_streak": (
                current_time - state["last_idle_home_ts"] if state["last_idle_home_ts"] else 0
            ),
            "total_idle_home_seconds": state["total_idle_home"],
            "door_open_count_per_floor": state["door_open_count"],
            "door_open_duration_per_floor": state["door_open_duration"]
        }
    }
