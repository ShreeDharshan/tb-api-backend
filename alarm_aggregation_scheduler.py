import time
import os
import requests
import logging
import threading
from thingsboard_auth import get_admin_jwt  # ✅ Shared JWT login


THINGSBOARD_URL = os.getenv("TB_BASE_URL", "https://thingsboard.cloud")
SCAN_INTERVAL = int(os.getenv("TB_SCHEDULER_INTERVAL", "30"))  # seconds

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("alarm_scheduler")

# === Control event for stopping the scheduler gracefully ===
stop_event = threading.Event()

def scheduler():
    logger.info("[Scheduler] Starting alarm aggregation loop...")
    while not stop_event.is_set():
        try:
            jwt_token = get_admin_jwt()
            if not jwt_token:
                logger.error("[Scheduler] Failed to get admin JWT, skipping this cycle.")
                time.sleep(SCAN_INTERVAL)
                continue

            headers = {"X-Authorization": f"Bearer {jwt_token}"}

            all_assets = get_all_assets(headers)
            for asset in all_assets:
                asset_id = asset['id']['id']
                count = aggregate_alarm_count(asset_id, headers)
                update_asset_alarm_count(asset_id, count, headers)

        except Exception as e:
            logger.error(f"[Scheduler] Error during aggregation: {e}")

        # Wait for interval or stop signal
        stop_event.wait(SCAN_INTERVAL)

    logger.info("[Scheduler] Stopped gracefully.")

# === Fetch all assets ===
def get_all_assets(headers):
    logger.info("[Assets] Fetching all assets...")
    url = f"{THINGSBOARD_URL}/api/tenant/assets?pageSize=500&page=0"
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json().get("data", [])

# === Aggregate alarms from devices recursively ===
def aggregate_alarm_count(entity_id, headers):
    total = 0
    children = get_related_entities(entity_id, headers)

    for child in children:
        child_id = child['id']['id']
        if child['entityType'] == 'DEVICE':
            count = get_active_alarm_count(child_id, headers)
            total += count
        elif child['entityType'] == 'ASSET':
            total += aggregate_alarm_count(child_id, headers)

    return total

# === Get related devices/assets (Contains relation) ===
def get_related_entities(entity_id, headers):
    url = f"{THINGSBOARD_URL}/api/relations/info?id={entity_id}&relationType=Contains&direction=FROM"
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.warning(f"[Relations] Failed for {entity_id}: {e}")
        return []

# === Count active alarms for a device ===
def get_active_alarm_count(device_id, headers):
    url = f"{THINGSBOARD_URL}/api/alarm?entityId={device_id}&status=ACTIVE"
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return len(data.get("data", []))
    except requests.RequestException as e:
        logger.warning(f"[Alarms] Failed to get alarms for device {device_id}: {e}")
        return 0

# === Update attribute for the asset ===
def update_asset_alarm_count(asset_id, count, headers):
    url = f"{THINGSBOARD_URL}/api/plugins/telemetry/ASSET/{asset_id}/SERVER_SCOPE"
    body = {
        "active_child_alarms": count,
        "has_critical_alarm": count > 0
    }
    try:
        resp = requests.post(url, headers={**headers, "Content-Type": "application/json"}, json=body)
        resp.raise_for_status()
        logger.info(f"[Update] Asset {asset_id} updated with count={count}")
    except requests.RequestException as e:
        logger.warning(f"[Update] Failed to update asset {asset_id}: {e}")

# === Stop function to call on FastAPI shutdown ===
def stop_scheduler():
    logger.info("[Scheduler] Stop signal received.")
    stop_event.set()
