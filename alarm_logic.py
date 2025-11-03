import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from config import TB_ACCOUNTS  # same source as calculated_telemetry
from thingsboard_auth import get_admin_jwt  # we keep this, we may want to push alarms to TB

logger = logging.getLogger("alarm_logic")
router = APIRouter()

# -----------------------------------------------------------------------------
# Pydantic model: accept BOTH old and new formats
# -----------------------------------------------------------------------------
class AlarmInput(BaseModel):
    # new JSON-based payload (what your current TB rule chain sends)
    deviceName: Optional[str] = Field(default=None)
    device_token: Optional[str] = Field(default=None)
    current_floor_index: Optional[int] = Field(default=None)
    lift_status: Optional[str] = Field(default=None)
    door_open: Optional[bool] = Field(default=None)
    ts: Optional[int] = Field(default=None)
    home_floor: Optional[int] = Field(default=None)

    # old / string-based / pack-based
    pack_raw: Optional[str] = Field(default=None)
    pack_out: Optional[str] = Field(default=None)
    raw: Optional[str] = Field(default=None)
    pack: Optional[str] = Field(default=None)

    # in case TB sends JSON as {"payload": {...}}
    payload: Optional[Dict[str, Any]] = Field(default=None)

    def normalize(self) -> "AlarmInput":
        """
        Make sure that if TB wrapped the real content in .payload we lift it up.
        Also let pack_raw come from aliases (raw/pack/pack_out).
        """
        # unwrap payload
        if self.payload and not self.deviceName:
            # copy keys from payload into self-like dict
            for k, v in self.payload.items():
                if getattr(self, k, None) is None:
                    setattr(self, k, v)

        # normalize pack-based
        if not self.pack_raw:
            if self.pack_out:
                self.pack_raw = self.pack_out
            elif self.raw:
                self.pack_raw = self.raw
            elif self.pack:
                self.pack_raw = self.pack

        return self


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------
def _resolve_account(x_account_id: Optional[str]) -> str:
    if x_account_id and x_account_id in TB_ACCOUNTS:
        return x_account_id
    # default to first configured account
    return next(iter(TB_ACCOUNTS.keys()))


def _create_alarm_in_tb(
    account_id: str,
    device_name: str,
    alarm_type: str,
    severity: str = "MAJOR",
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Optional: actually create an alarm in TB, using admin JWT.
    You can comment this out if you just want to RETURN alarms to TB rule chain.
    """
    try:
        base_url = TB_ACCOUNTS[account_id]
        jwt = get_admin_jwt(account_id, base_url)
        # fetch device id first
        dev_resp = requests.get(
            f"{base_url}/api/tenant/devices?deviceName={device_name}",
            headers={"X-Authorization": f"Bearer {jwt}"},
            timeout=10,
        )
        dev_resp.raise_for_status()
        dev_data = dev_resp.json()
        if not (isinstance(dev_data, dict) and "id" in dev_data and "id" in dev_data["id"]):
            logger.error(f"[TB alarm] device not found: {device_name}")
            return
        device_id = dev_data["id"]["id"]

        alarm_payload = {
            "type": alarm_type,
            "originator": {"entityType": "DEVICE", "id": device_id},
            "severity": severity,
            "status": "ACTIVE_UNACK",
            "details": details or {},
        }
        a_resp = requests.post(
            f"{base_url}/api/alarm",
            headers={
                "X-Authorization": f"Bearer {jwt}",
                "Content-Type": "application/json",
            },
            json=alarm_payload,
            timeout=10,
        )
        a_resp.raise_for_status()
        logger.info(f"[TB alarm] created {alarm_type} for {device_name}")
    except Exception as e:
        logger.error(f"[TB alarm] failed to create alarm: {e}")


# -----------------------------------------------------------------------------
# simple JSON-based alarm rules
# -----------------------------------------------------------------------------
def _evaluate_json_alarms(data: AlarmInput) -> List[Dict[str, Any]]:
    """
    This is for the CURRENT rule chain shape (JSON, not pack_raw).

    We'll just do very simple rules here — extend later:
    1. door stuck open on a floor
    2. undefined status
    """
    alarms: List[Dict[str, Any]] = []

    dev = data.deviceName or "UNKNOWN"
    floor = data.current_floor_index
    status = (data.lift_status or "").lower()
    door = bool(data.door_open)
    ts = data.ts or int(time.time() * 1000)

    # rule 1: door open while NOT moving
    if door and status in ("idle", "stopped", ""):
        alarms.append(
            {
                "type": "door_open_idle",
                "severity": "MAJOR",
                "ts": ts,
                "deviceName": dev,
                "details": {
                    "floor": floor,
                    "door_open": door,
                    "lift_status": status,
                },
            }
        )

    # rule 2: unknown status
    if status not in ("idle", "moving", "door_open", "stopped") and status != "":
        alarms.append(
            {
                "type": "lift_unknown_status",
                "severity": "MINOR",
                "ts": ts,
                "deviceName": dev,
                "details": {
                    "seen_status": status,
                },
            }
        )

    return alarms


# -----------------------------------------------------------------------------
# optional: old pack_raw-based evaluator (keep backward compatibility)
# -----------------------------------------------------------------------------
def _evaluate_pack_alarms(pack_str: str) -> List[Dict[str, Any]]:
    """
    Minimal placeholder evaluator for old pack-based telemetry.
    We'll just parse it as k=v|k=v... and make the same door-open rule.
    """
    alarms: List[Dict[str, Any]] = []
    parts = pack_str.split("|")
    data: Dict[str, str] = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            data[k.strip()] = v.strip()

    door_val = data.get("door_val") or data.get("door") or ""
    st = (data.get("st") or "").lower()
    fl = data.get("fl") or data.get("fi") or None
    ts = int(time.time() * 1000)

    door_open = door_val.upper() in ("OPEN", "1", "TRUE")

    if door_open and st in ("i", "idle", ""):
        alarms.append(
            {
                "type": "door_open_idle",
                "severity": "MAJOR",
                "ts": ts,
                "deviceName": data.get("deviceName", "UNKNOWN"),
                "details": {
                    "floor": fl,
                    "door_val": door_val,
                    "status": st,
                },
            }
        )

    return alarms


# -----------------------------------------------------------------------------
# endpoint
# -----------------------------------------------------------------------------
@router.post("/check_alarm/")
async def check_alarm(
    body: AlarmInput,
    x_account_id: Optional[str] = Header(None, alias="X-Account-ID"),
):
    """
    This endpoint now supports BOTH:
    - old pack_raw-based inputs
    - new JSON-based inputs coming from the old working rule chain
    """
    data = body.normalize()
    account_id = _resolve_account(x_account_id)

    # 1) if we DO have pack_* → use old path
    if data.pack_raw:
        alarms = _evaluate_pack_alarms(data.pack_raw)
        return {
            "status": "success",
            "source": "pack",
            "alarms": alarms,
        }

    # 2) else, try JSON-style (current situation)
    if data.deviceName:
        alarms = _evaluate_json_alarms(data)

        # if you want the backend to ALSO create in TB, uncomment:
        # for a in alarms:
        #     _create_alarm_in_tb(account_id, data.deviceName, a["type"], a["severity"], a["details"])

        return {
            "status": "success",
            "source": "json",
            "alarms": alarms,
        }

    # 3) if neither is present → real bad request
    logger.error("[/check_alarm] neither pack_raw nor JSON fields were present")
    raise HTTPException(
        status_code=400,
        detail="Missing telemetry: expected JSON (deviceName, current_floor_index, lift_status, door_open) "
               "or pack_raw/pack_out/raw/pack",
    )
