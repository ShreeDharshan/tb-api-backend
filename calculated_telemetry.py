from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict
import requests
import os
import logging
import time

# === Logging config ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()

THINGSBOARD_HOST = "https://thingsboard.cloud"

# Cache admin token
admin_token_cache = {"token": None, "expiry": 0}

# In-memory state for idle calculation
device_state: Dict[str, Dict[str, Optional[int]]] = {}

class CalculatedTelemetryPayload(BaseModel):
    deviceName: str
    device_token: str
    current_floor_index: int
    lift_status: Optional[str] = None
    ts: Optional[int] = None

# --- Helpers (mirroring alarm_logic) ---

def get_admin_token():
    if admin_token_cache["token"] and admin_token_cache["expiry"] > time.time():
        return admin_token_cache["token"]
    
    url = f"{THINGSBOARD_HOST}/api/auth/login"
    credentials = {
        "username": os.getenv("TB_ADMIN_USER"),
        "password": os.getenv("TB_ADMIN_PASS")
    }
    logger.info("[ADMIN LOGIN] Logging in to ThingsBoard Cloud for calculated telemetry...")
    resp = requests.post(url, json=credentials)
    if resp.status_code != 200:
        logger.error(f"[ADMIN LOGIN] Failed: {resp.status_code} - {resp.text}")
        raise HTTPException(status_code=500, detail="Admin login failed")
    
    token = resp.json()["token"]
    expires_in = resp.json().get("refreshTokenExp", 3600)
    admin_token_cache["token"] = token
    admin_token_cache["expiry"] = time.time() + (expires_in / 1000) - 60
    logger.info("[ADMIN LOGIN] Admin token retrieved successfully")
    return token

def get_device_id(device_name: str) -> Optional[str]:
    url = f"{THINGSBOARD_HOST}/api/tenant/devices?deviceName={device_name}"
    token = get_admin_token()
    res = requests.get(url, headers={"X-Authorization": f"Bearer {token}"})
    logger.info(f"[DEVICE_LOOKUP] Fetching ID for {device_name} | Status: {res.status_code}")
    
    if res.status_code == 200:
        try:
            device_id = res.json()["id"]["id"]
            return device_id
        except Exception as e:
            logger.error(f"[DEVICE_LOOKUP] Failed to parse device ID: {e}")
            return None
    logger.error(f"[DEVICE_LOOKUP] Failed: {res.status_code} | {res.text}")
    return None

def get_home_floor(device_id: str) -> Optional[int]:
    url = f"{THINGSBOARD_HOST}/api/plugins/telemetry/DEVICE/{device_id}/values/attributes/SERVER_SCOPE"
    token = get_admin_token()
    res = requests.get(url, headers={"X-Authorization": f"Bearer {token}"})
    logger.info(f"[ATTRIBUTES] Fetching home_floor | Status: {res.status_code}")
    
    if res.status_code == 200:
        try:
            for attr in res.json():
                if attr.get("key") == "home_floor":
                    return int(attr.get("value"))
                if attr.get("key") == "ss_home_floor":
                    return int(attr.get("value"))
            logger.warning("[ATTRIBUTES] home_floor not found")
        except Exception as e:
            logger.error(f"[ATTRIBUTES] Failed to parse attributes: {e}")
    return None

def push_telemetry_to_tb(device_token: str, telemetry: Dict[str, int]) -> None:
    url = f"{THINGSBOARD_HOST}/api/v1/{device_token}/telemetry"

    # 🚨 Log everything pushed
    logger.warning(f"[TELEMETRY PUSH] Raw telemetry to push: {telemetry}")

    # ✅ Only allow calculated fields
    safe_telemetry = {
        k: v for k, v in telemetry.items()
        if k in ["idle_outside_home_streak", "total_idle_outside_home_seconds"]
    }

    if not safe_telemetry:
        logger.error("[TELEMETRY PUSH] Aborting - no valid calculated fields")
        return

    try:
        response = requests.post(
            url,
            json=safe_telemetry,
            headers={"Content-Type": "application/json"},
            timeout=5
        )
        if response.status_code != 200:
            logger.error(f"[TELEMETRY PUSH] Failed: {response.status_code} - {response.text}")
        else:
            logger.info("[TELEMETRY PUSH] Successfully pushed calculated telemetry")
    except Exception as e:
        logger.error(f"[TELEMETRY PUSH] Exception: {e}")

# --- API Endpoint ---

@router.post("/calculated-telemetry/")
async def calculated_telemetry(payload: CalculatedTelemetryPayload):
    logger.info("--- /calculated-telemetry/ invoked ---")
    logger.info(f"Payload received: {payload}")

    ts = payload.ts or int(time.time() * 1000)

    # Get device ID
    device_id = get_device_id(payload.deviceName)
    if not device_id:
        return {"status": "error", "msg": f"Could not find device ID for {payload.deviceName}"}

    # Get home_floor attribute
    home_floor = get_home_floor(device_id)
    if home_floor is None:
        return {"status": "error", "msg": "home_floor attribute not found"}

    # Initialize state if not present
    if payload.device_token not in device_state:
        device_state[payload.device_token] = {
            "last_idle_ts": None,
            "total_idle_outside": 0
        }

    state = device_state[payload.device_token]
    current_time = ts // 1000

    # --- Idle calculation ---
    if payload.lift_status and payload.lift_status.lower() == "idle" and int(payload.current_floor_index) != home_floor:
        if state["last_idle_ts"] is None:
            state["last_idle_ts"] = current_time
        else:
            elapsed = current_time - state["last_idle_ts"]
            state["total_idle_outside"] += elapsed
            state["last_idle_ts"] = current_time
    else:
        state["last_idle_ts"] = None

    calculated_values = {
        "idle_outside_home_streak": (
            current_time - state["last_idle_ts"] if state["last_idle_ts"] else 0
        ),
        "total_idle_outside_home_seconds": state["total_idle_outside"]
    }

    # Push telemetry back to TB (safe filtered)
    push_telemetry_to_tb(payload.device_token, calculated_values)

    return {"status": "success", "calculated": calculated_values}
