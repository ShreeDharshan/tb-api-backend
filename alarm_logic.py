from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import requests
import os
import logging
import time

# === Logging config ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()

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

# === Thresholds ===
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

TOLERANCE_MM = 10.0
DOOR_OPEN_THRESHOLD_SEC = 15

class AlarmPayload(BaseModel):
    deviceName: str
    floor: Optional[str] = None
    timestamp: Optional[str] = None
    height: Optional[float] = None
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

@router.post("/check_alarm/")
async def check_alarm(payload: AlarmPayload, request: Request):
    logger.info("--- /check_alarm/ invoked ---")
    logger.info(f"Payload received: {payload}")

    account_key = request.headers.get("X-TB-Account", "account1")
    token = get_admin_token(account_key)
    host = get_account_creds(account_key)['host']

    alarms_triggered = []

    # === Example Alarm Logic (floor mismatch) ===
    if payload.height and payload.current_floor_index is not None:
        floor_height = get_floor_height(payload.current_floor_index)  # hypothetical function
        if abs(payload.height - floor_height) > TOLERANCE_MM:
            alarms_triggered.append("floor_mismatch")

    # === Temperature & Humidity Alarms ===
    if payload.temperature and payload.temperature > THRESHOLDS["temperature"]:
        alarms_triggered.append("high_temperature")
    if payload.humidity and payload.humidity > THRESHOLDS["humidity"]:
        alarms_triggered.append("high_humidity")

    # === Vibration Alarms ===
    if payload.x_vibe and payload.x_vibe > THRESHOLDS["x_vibe"]:
        alarms_triggered.append("x_vibration")
    if payload.y_vibe and payload.y_vibe > THRESHOLDS["y_vibe"]:
        alarms_triggered.append("y_vibration")
    if payload.z_vibe and payload.z_vibe > THRESHOLDS["z_vibe"]:
        alarms_triggered.append("z_vibration")

    # === Jerk Alarms ===
    if payload.x_jerk and payload.x_jerk > THRESHOLDS["x_jerk"]:
        alarms_triggered.append("x_jerk")
    if payload.y_jerk and payload.y_jerk > THRESHOLDS["y_jerk"]:
        alarms_triggered.append("y_jerk")
    if payload.z_jerk and payload.z_jerk > THRESHOLDS["z_jerk"]:
        alarms_triggered.append("z_jerk")

    # === Door Open Alarm (placeholder logic) ===
    if payload.door_open:
        alarms_triggered.append("door_open_too_long")

    # === Send alarms to ThingsBoard ===
    if alarms_triggered:
        url = f"{host}/api/plugins/telemetry/DEVICE/{payload.deviceName}/POST_TELEMETRY"
        headers = {"Content-Type": "application/json", "X-Authorization": f"Bearer {token}"}
        response = requests.post(url, headers=headers, json={"alarms": alarms_triggered})
        if response.status_code != 200:
            logger.error(f"Failed to send alarms: {response.text}")
            raise HTTPException(status_code=response.status_code, detail="Failed to send alarms")

    return {"status": "processed", "alarms_triggered": alarms_triggered}

# === Helper Function for Floor Height ===
def get_floor_height(floor_index: int) -> float:
    # Replace with actual floor height logic (DB or static map)
    floor_map = {
        0: 0.0,
        1: 3000.0,
        2: 6000.0,
        3: 9000.0,
        4: 12000.0,
        5: 15000.0,
        6: 18000.0,
        7: 21000.0
    }
    return floor_map.get(floor_index, 0.0)
