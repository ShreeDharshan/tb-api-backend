from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import requests
import os
import threading
import logging

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

class TelemetryPayload(BaseModel):
    deviceName: str
    floor: str
    timestamp: str
    height: float
    current_floor_index: Optional[int] = None
    ss_floor_boundaries: Optional[str] = None
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

def get_device_id(device_name: str, jwt_token: str) -> Optional[str]:
    if device_name in device_cache:
        return device_cache[device_name]
    url = f"{THINGSBOARD_HOST}/api/tenant/devices?deviceName={device_name}"
    res = requests.get(url, headers={"X-Authorization": f"Bearer {jwt_token}"})
    if res.status_code == 200:
        try:
            device_id = res.json()["id"]["id"]
            device_cache[device_name] = device_id
            return device_id
        except Exception:
            return None
    return None

def create_alarm_on_tb(device_name: str, alarm_type: str, ts: int, severity: str, details: dict, jwt_token: str):
    device_id = get_device_id(device_name, jwt_token)
    if not device_id:
        logger.warning(f"[ALARM] Could not fetch device ID for {device_name}")
        return
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
        headers={"X-Authorization": f"Bearer {jwt_token}", "Content-Type": "application/json"},
        json=alarm_payload
    )
    if 200 <= response.status_code < 300:
        logger.info(f"[ALARM] Created: {alarm_payload}")
    else:
        logger.error(f"[ALARM] Failed: {response.status_code} - {response.text}")

def check_bucket_and_trigger(device: str, key: str, value: float, height: float, ts: int, floor: str, jwt_token: str):
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
                }, jwt_token)
                buckets.remove(b)
            break

    if not matched:
        buckets.append({"center": height, "count": 1})

def floor_mismatch_detected(height: float, current_floor_index: int, floor_boundaries_str: str) -> bool:
    try:
        if height is None or current_floor_index is None:
            return False
        floor_boundaries = [float(x.strip()) for x in floor_boundaries_str.split(",") if x.strip()]
        if not floor_boundaries or len(floor_boundaries) < 2:
            return False
        detected_floor = -1
        for i in range(len(floor_boundaries) - 1):
            if floor_boundaries[i] <= height < floor_boundaries[i + 1]:
                detected_floor = i
                break
        return detected_floor != -1 and detected_floor != current_floor_index
    except Exception as e:
        logger.error(f"[ERROR] Floor mismatch logic: {e}")
        return False

def schedule_door_alarm(device_name: str, floor: str, ts: int, jwt_token: str):
    def fire_alarm():
        create_alarm_on_tb(device_name, "Door Open Too Long", ts + 15000, "MAJOR", {
            "duration_sec": 15,
            "floor": floor
        }, jwt_token)
        logger.info(f"[DOOR] Door open too long alarm fired for {device_name}")
    if device_name in door_open_timers:
        door_open_timers[device_name].cancel()
    timer = threading.Timer(15, fire_alarm)
    door_open_timers[device_name] = timer
    timer.start()

def cancel_door_alarm(device_name: str):
    if device_name in door_open_timers:
        door_open_timers[device_name].cancel()
        del door_open_timers[device_name]

@router.post("/check_alarm/")
async def check_alarm(payload: TelemetryPayload, authorization: str = Header(...)):
    logger.info("--- /check_alarm/ invoked ---")
    logger.info(f"Authorization: {authorization}")
    logger.info(f"Payload received: {payload}")

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=400, detail="Missing Bearer token")
    
    jwt_token = authorization.split(" ", 1)[1]
    ts = int(datetime.utcnow().timestamp() * 1000)
    triggered = []

    try:
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
                }, jwt_token)

        for key in ["x_jerk", "y_jerk", "z_jerk", "x_vibe", "y_vibe", "z_vibe"]:
            val = getattr(payload, key)
            if val is not None and val > THRESHOLDS[key]:
                check_bucket_and_trigger(payload.deviceName, key, val, payload.height, ts, payload.floor, jwt_token)

        if payload.ss_floor_boundaries and payload.current_floor_index is not None:
            if floor_mismatch_detected(payload.height, payload.current_floor_index, payload.ss_floor_boundaries):
                triggered.append({
                    "type": "Floor Mismatch Alarm",
                    "value": payload.height,
                    "severity": "CRITICAL"
                })
                create_alarm_on_tb(payload.deviceName, "Floor Mismatch Alarm", ts, "CRITICAL", {
                    "reported_index": payload.current_floor_index,
                    "height": payload.height,
                    "boundaries": payload.ss_floor_boundaries
                }, jwt_token)

        if payload.door_open is not None:
            device_door_state[payload.deviceName] = bool(payload.door_open)

        if device_door_state.get(payload.deviceName, False):
            schedule_door_alarm(payload.deviceName, payload.floor, ts, jwt_token)
        else:
            cancel_door_alarm(payload.deviceName)

        return {"status": "processed", "alarms_triggered": triggered}

    except Exception as e:
        logger.error(f"[ERROR] Exception during alarm processing: {e}")
        raise HTTPException(status_code=500, detail="Alarm processing failed")
