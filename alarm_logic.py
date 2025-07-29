from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import requests
import os
import threading
import logging
import time

# === Logging config ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()

THINGSBOARD_HOST = "https://thingsboard.cloud"

THRESHOLDS = {
    "humidity": 50.0,
    "temperature": 50.0,
    "x_jerk": 5.0,
    "y_jerk": 5.0,
    "z_jerk": 15.0,
    "x_vibe": 5.0,
    "y_vibe": 5.0,
    "z_vibe": 15.0
}

TOLERANCE_MM = 10.0  # landing tolerance for door mismatch

class TelemetryPayload(BaseModel):
    deviceName: str
    floor: str
    timestamp: str
    height: float
    current_floor_index: Optional[int] = None
    x_vibe: Optional[float] = None
    y_vibe: Optional[float] = None
    z_vibe: Optional[float] = None
    x_jerk: Optional[float] = None
    y_jerk: Optional[float] = None
    z_jerk: Optional[float] = None
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    door_open: Optional[bool] = None

device_cache = {}
bucket_counts = {}
door_open_timers = {}
device_door_state = {}

# === Admin token caching ===
admin_token_cache = {"token": None, "expiry": 0}

def get_admin_token():
    """Login as Tenant Admin and return a cached token."""
    if admin_token_cache["token"] and admin_token_cache["expiry"] > time.time():
        return admin_token_cache["token"]
    
    url = f"{THINGSBOARD_HOST}/api/auth/login"
    credentials = {
        "username": os.getenv("TB_ADMIN_USER"),
        "password": os.getenv("TB_ADMIN_PASS")
    }
    logger.info("[ADMIN LOGIN] Logging in to ThingsBoard Cloud...")
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
    """Fetch device ID using admin token."""
    if device_name in device_cache:
        return device_cache[device_name]
    
    token = get_admin_token()
    url = f"{THINGSBOARD_HOST}/api/tenant/devices?deviceName={device_name}"
    res = requests.get(url, headers={"X-Authorization": f"Bearer {token}"})
    logger.info(f"[DEVICE_LOOKUP] Fetching ID for {device_name} | Status: {res.status_code}")
    
    if res.status_code == 200:
        try:
            device_id = res.json()["id"]["id"]
            device_cache[device_name] = device_id
            return device_id
        except Exception as e:
            logger.error(f"[DEVICE_LOOKUP] Failed to parse device ID: {e}")
            return None
    logger.error(f"[DEVICE_LOOKUP] Failed: {res.status_code} | {res.text}")
    return None

def get_floor_boundaries(device_id: str) -> Optional[str]:
    """Fetch floor_boundaries from server-side attributes."""
    token = get_admin_token()
    url = f"{THINGSBOARD_HOST}/api/plugins/telemetry/DEVICE/{device_id}/values/attributes/SERVER_SCOPE"
    res = requests.get(url, headers={"X-Authorization": f"Bearer {token}"})
    logger.info(f"[ATTRIBUTES] Fetching floor boundaries | Status: {res.status_code}")
    
    if res.status_code == 200:
        try:
            logger.info(f"[ATTRIBUTES RAW] Response JSON: {res.text}")
            for attr in res.json():
                logger.info(f"[ATTRIBUTES] Key={attr.get('key')} Value={attr.get('value')}")
                if attr["key"] == "floor_boundaries":
                    logger.info(f"[ATTRIBUTES FOUND] floor_boundaries = {attr['value']}")
                    return attr["value"]
            logger.warning("[ATTRIBUTES] floor_boundaries not found in attributes")
        except Exception as e:
            logger.error(f"[ATTRIBUTES] Failed to parse attributes: {e}")
    return None

def create_alarm_on_tb(device_name: str, alarm_type: str, ts: int, severity: str, details: dict):
    """Create an alarm using admin token."""
    device_id = get_device_id(device_name)
    if not device_id:
        logger.warning(f"[ALARM] Could not fetch device ID for {device_name}")
        return
    
    token = get_admin_token()
    alarm_payload = {
        "originator": {
            "entityType": "DEVICE",
            "id": device_id
        },
        "type": alarm_type,
        "severity": severity,
        "status": "ACTIVE_UNACK",
        "details": details
    }
    response = requests.post(
        f"{THINGSBOARD_HOST}/api/alarm",
        headers={"X-Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=alarm_payload
    )
    if 200 <= response.status_code < 300:
        logger.info(f"[ALARM] Created: {alarm_payload}")
    else:
        logger.error(f"[ALARM] Failed: {response.status_code} - {response.text}")

def check_bucket_and_trigger(device: str, key: str, value: float, height: float, ts: int, floor: str):
    if device not in bucket_counts:
        bucket_counts[device] = {}
    if key not in bucket_counts[device]:
        bucket_counts[device][key] = []

    buckets = bucket_counts[device][key]
    matched = False

    for b in buckets:
        if abs(b["center"] - height) <= 50:
            b["count"] += 1
            matched = True
            if b["count"] >= 3:
                create_alarm_on_tb(device, f"{key} Alarm", ts, "MINOR", {
                    "value": value,
                    "threshold": THRESHOLDS[key],
                    "floor": floor,
                    "height_zone": f"{b['center']-50:.1f} to {b['center']+50:.1f}"
                })
                buckets.remove(b)
            break

    if not matched:
        buckets.append({"center": height, "count": 1})

def schedule_door_alarm(device_name: str, floor: str, ts: int):
    """Schedules a door open too long alarm."""
    def fire_alarm():
        create_alarm_on_tb(device_name, "Door Open Too Long", ts + 15000, "MAJOR", {
            "duration_sec": 15,
            "floor": floor
        })
        logger.info(f"[DOOR] Door open too long alarm fired for {device_name}")
    
    if device_name in door_open_timers:
        door_open_timers[device_name].cancel()
    
    timer = threading.Timer(15, fire_alarm)
    door_open_timers[device_name] = timer
    timer.start()

def cancel_door_alarm(device_name: str):
    """Cancels any scheduled door alarm for a device."""
    if device_name in door_open_timers:
        door_open_timers[device_name].cancel()
        del door_open_timers[device_name]

def floor_mismatch_detected(height: float, current_floor_index: int, floor_boundaries_str: str) -> bool:
    try:
        if height is None or current_floor_index is None:
            logger.info("[DEBUG] Height or current_floor_index is None, skipping mismatch check.")
            return False
        
        logger.info(f"[DEBUG] Raw boundaries string: {floor_boundaries_str}")
        floor_boundaries = [float(x.strip()) for x in floor_boundaries_str.split(",") if x.strip()]
        logger.info(f"[DEBUG] Parsed floor boundaries list: {floor_boundaries}")

        if current_floor_index >= len(floor_boundaries):
            logger.warning("[DEBUG] current_floor_index exceeds boundaries length")
            return True  # Invalid index is a mismatch

        # Landing tolerance check
        floor_center = floor_boundaries[current_floor_index]
        deviation = abs(height - floor_center)
        mismatch = deviation > TOLERANCE_MM

        logger.info(f"[DEBUG] Floor center: {floor_center}, Height: {height}, "
                    f"Deviation: {deviation}, Tolerance: {TOLERANCE_MM}, "
                    f"Mismatch: {mismatch}")
        return mismatch

    except Exception as e:
        logger.error(f"[ERROR] Floor mismatch logic failed: {e}")
        return False

@router.post("/check_alarm/")
async def check_alarm(payload: TelemetryPayload, authorization: Optional[str] = Header(None)):
    logger.info("--- /check_alarm/ invoked ---")
    logger.info(f"Payload received: {payload}")

    ts = int(datetime.utcnow().timestamp() * 1000)
    triggered = []

    try:
        # Humidity & Temperature
        for k in ["humidity", "temperature"]:
            val = getattr(payload, k)
            if val is not None and val > THRESHOLDS[k]:
                triggered.append({
                    "type": f"{k.capitalize()} Alarm",
                    "value": val,
                    "threshold": THRESHOLDS[k],
                    "severity": "WARNING"
                })
                create_alarm_on_tb(payload.deviceName, f"{k.capitalize()} Alarm", ts, "WARNING", {
                    "value": val,
                    "threshold": THRESHOLDS[k],
                    "floor": payload.floor
                })

        # Jerks and Vibrations
        for key in ["x_jerk", "y_jerk", "z_jerk", "x_vibe", "y_vibe", "z_vibe"]:
            val = getattr(payload, key)
            if val is not None and val > THRESHOLDS[key]:
                check_bucket_and_trigger(payload.deviceName, key, val, payload.height, ts, payload.floor)

        # Floor Mismatch
        if payload.current_floor_index is not None:
            device_id = get_device_id(payload.deviceName)
            if device_id:
                floor_boundaries = get_floor_boundaries(device_id)
                if floor_boundaries:
                    logger.info(f"[DEBUG] Attempting mismatch detection with boundaries: {floor_boundaries}")
                    if floor_mismatch_detected(payload.height, int(payload.current_floor_index), floor_boundaries):
                        triggered.append({
                            "type": "Floor Mismatch Alarm",
                            "value": payload.height,
                            "severity": "CRITICAL"
                        })
                        create_alarm_on_tb(payload.deviceName, "Floor Mismatch Alarm", ts, "CRITICAL", {
                            "reported_index": payload.current_floor_index,
                            "height": payload.height,
                            "boundaries": floor_boundaries
                        })
                else:
                    logger.warning("[DEBUG] floor_boundaries is None or empty, skipping mismatch detection")

        # Door alarms
        if payload.door_open is not None:
            device_door_state[payload.deviceName] = bool(payload.door_open)

        if device_door_state.get(payload.deviceName, False):
            schedule_door_alarm(payload.deviceName, payload.floor, ts)
        else:
            cancel_door_alarm(payload.deviceName)

        logger.info(f"Triggered alarms: {triggered}")
        return {"status": "processed", "alarms_triggered": triggered}

    except Exception as e:
        logger.error(f"[ERROR] Exception during alarm processing: {e}")
        raise HTTPException(status_code=500, detail="Alarm processing failed")
