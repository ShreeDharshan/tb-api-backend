import time
import requests
from fastapi import APIRouter, Request
from typing import Dict, Any

router = APIRouter()

# Store temporary state in memory (per device token)
device_state = {}

# ThingsBoard Cloud base URL
THINGSBOARD_BASE_URL = "https://thingsboard.cloud"


def get_home_floor(device_token: str) -> int:
    """
    Fetch home_floor server-side attribute for a device using its access token.
    Supports both 'home_floor' and 'ss_home_floor' naming.
    """
    url = f"{THINGSBOARD_BASE_URL}/api/v1/{device_token}/attributes"
    headers = {"Content-Type": "application/json"}
    try:
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        attrs = response.json()
        server_attrs = attrs.get("server", {})

        # Check both variants
        if "home_floor" in server_attrs:
            return int(server_attrs["home_floor"])
        elif "ss_home_floor" in server_attrs:
            return int(server_attrs["ss_home_floor"])
        else:
            print(f"No home_floor attribute found for token {device_token}. Server attrs: {server_attrs}")
    except Exception as e:
        print(f"Error fetching home_floor for token {device_token}: {e}")
    return None


def push_telemetry_to_tb(device_token: str, telemetry: Dict[str, Any]) -> None:
    """
    Push calculated telemetry back to ThingsBoard Cloud using device token.
    """
    url = f"{THINGSBOARD_BASE_URL}/api/v1/{device_token}/telemetry"
    headers = {"Content-Type": "application/json"}
    try:
        response = requests.post(url, headers=headers, json=telemetry, timeout=5)
        response.raise_for_status()
    except Exception as e:
        print(f"Error pushing calculated telemetry for token {device_token}: {e}")


@router.post("/calculated-telemetry/")
async def calculated_telemetry(request: Request):
    """
    Calculate derived telemetry values such as idle time outside home floor
    and push them back to ThingsBoard Cloud as new telemetry keys.
    """
    data = await request.json()

    # Expected payload from rule chain
    device_token = data.get("device_token")
    current_floor_index = data.get("current_floor_index")
    lift_status = data.get("lift_status")
    ts = data.get("ts", int(time.time() * 1000))

    if not device_token:
        return {"status": "error", "msg": "device_token required"}

    # Fetch home_floor using device token (handles both attribute names)
    home_floor = get_home_floor(device_token)
    if home_floor is None:
        return {"status": "error", "msg": "home_floor attribute not found"}

    # Initialize state for this device if needed
    if device_token not in device_state:
        device_state[device_token] = {
            "last_idle_ts": None,
            "total_idle_outside": 0
        }

    state = device_state[device_token]
    current_time = ts // 1000  # convert to seconds

    # --- Idle logic ---
    if lift_status and lift_status.lower() == "idle" and int(current_floor_index) != home_floor:
        if state["last_idle_ts"] is None:
            state["last_idle_ts"] = current_time
        else:
            elapsed = current_time - state["last_idle_ts"]
            state["total_idle_outside"] += elapsed
            state["last_idle_ts"] = current_time
    else:
        state["last_idle_ts"] = None

    # --- Build calculated telemetry ---
    calculated_values = {
        "idle_outside_home_streak": (
            current_time - state["last_idle_ts"]
            if state["last_idle_ts"] else 0
        ),
        "total_idle_outside_home_seconds": state["total_idle_outside"]
    }

    # --- Push back to ThingsBoard Cloud ---
    push_telemetry_to_tb(device_token, calculated_values)

    return {"status": "success", "calculated": calculated_values}
