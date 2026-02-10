import logging
import os
import threading
from typing import Dict, List

import requests

from src.config import parse_tb_accounts
from src.core.auth import get_admin_jwt

SCAN_INTERVAL = int(os.getenv("TB_SCHEDULER_INTERVAL", "30"))

logger = logging.getLogger("services.aggregation")
_stop_event = threading.Event()


def get_all_assets(base_url: str, headers: Dict[str, str]) -> List[dict]:
    url = f"{base_url}/api/tenant/assets?pageSize=500&page=0"
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()
    return response.json().get("data", [])


def get_related_entities(base_url: str, entity_id: str, headers: Dict[str, str]) -> List[dict]:
    url = f"{base_url}/api/relations?fromId={entity_id}&fromType=ASSET"
    try:
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        logger.warning("[Relations] Failed for %s: %s", entity_id, exc)
        return []


def get_device_active_alarm_count(base_url: str, device_id: str, headers: Dict[str, str]) -> int:
    url = f"{base_url}/api/alarm/DEVICE/{device_id}"
    params = {"pageSize": 100, "page": 0, "searchStatus": "ACTIVE"}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        alarms = [
            alarm
            for alarm in data.get("data", [])
            if alarm.get("status") in ["ACTIVE_UNACK", "ACTIVE_ACK"]
        ]
        return len(alarms)
    except requests.RequestException as exc:
        logger.warning("[Alarms] Failed to get alarms for %s: %s", device_id, exc)
        return 0


def aggregate_alarm_count(base_url: str, entity_id: str, headers: Dict[str, str]) -> int:
    total = 0
    for child in get_related_entities(base_url, entity_id, headers):
        child_id = child["to"]["id"]
        entity_type = child["to"]["entityType"]

        if entity_type == "DEVICE":
            total += get_device_active_alarm_count(base_url, child_id, headers)
        elif entity_type == "ASSET":
            total += aggregate_alarm_count(base_url, child_id, headers)
    return total


def update_asset_alarm_count(base_url: str, asset_id: str, count: int, headers: Dict[str, str]) -> None:
    url = f"{base_url}/api/plugins/telemetry/ASSET/{asset_id}/SERVER_SCOPE"
    body = {"active_child_alarms": count, "has_critical_alarm": count > 0}
    try:
        response = requests.post(
            url,
            headers={**headers, "Content-Type": "application/json"},
            json=body,
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("[Update] Failed to update asset %s: %s", asset_id, exc)


def scheduler() -> None:
    logger.info("[Scheduler] Starting alarm aggregation loop...")
    while not _stop_event.is_set():
        try:
            for account_id, base_url in parse_tb_accounts().items():
                jwt_token = get_admin_jwt(account_id, base_url)
                if not jwt_token:
                    logger.error("[Scheduler] Failed to get admin JWT for %s, skipping...", account_id)
                    continue

                headers = {"X-Authorization": f"Bearer {jwt_token}"}
                for asset in get_all_assets(base_url, headers):
                    asset_id = asset["id"]["id"]
                    count = aggregate_alarm_count(base_url, asset_id, headers)
                    update_asset_alarm_count(base_url, asset_id, count, headers)
        except Exception as exc:
            logger.error("[Scheduler] Error during aggregation: %s", exc)

        _stop_event.wait(SCAN_INTERVAL)

    logger.info("[Scheduler] Stopped gracefully.")


def stop_scheduler() -> None:
    logger.info("[Scheduler] Stop signal received.")
    _stop_event.set()
