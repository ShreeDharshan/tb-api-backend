from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
import requests
import pandas as pd
import datetime
import os
import json
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.utils import get_column_letter
from thingsboard_auth import get_admin_jwt  # ✅ Import shared auth

# === Load Multi-Account Configuration ===
try:
    ACCOUNTS = json.loads(os.getenv("TB_ACCOUNTS", '{}'))
    if not isinstance(ACCOUNTS, dict):
        raise ValueError("TB_ACCOUNTS must be a JSON object")
except json.JSONDecodeError:
    raise RuntimeError("Invalid JSON format for TB_ACCOUNTS environment variable")

router = APIRouter()

from pydantic import BaseModel, Field

class ReportRequest(BaseModel):
    device_name: str = Field(...)
    data_types: list[str] = Field(...)
    include_alarms: bool = Field(...)
    start_date: str = Field(...)
    end_date: str = Field(...)


VIBE_KEY_MAP = {
    'x_vibration': 'x_vibe',
    'y_vibration': 'y_vibe',
    'z_vibration': 'z_vibe'
}

def extract_jwt_user_info(jwt_token: str, account_id: str):
    host = ACCOUNTS[account_id]
    url = f"{host}/api/auth/user"
    headers = {"X-Authorization": f"Bearer {jwt_token}"}
    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid token")
    return resp.json()

def get_permitted_device_id(jwt_token: str, device_name: str, authority: str, account_id: str, customer_id: str = None):
    host = ACCOUNTS[account_id]
    headers = {"X-Authorization": f"Bearer {jwt_token}"}
    if authority == "TENANT_ADMIN":
        url = f"{host}/api/tenant/devices?pageSize=1000&page=0"
    elif authority == "CUSTOMER_USER":
        if not customer_id:
            raise HTTPException(status_code=403, detail="Customer ID required")
        url = f"{host}/api/customer/{customer_id}/deviceInfos?pageSize=1000&page=0"
    else:
        raise HTTPException(status_code=403, detail="Role not supported")

    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    for d in resp.json().get("data", []):
        if d["name"] == device_name:
            return d["id"]["id"]
    raise HTTPException(status_code=403, detail=f"No access to device '{device_name}'")

def fetch_telemetry(jwt_token: str, device_id: str, keys: list[str],
                    start_ts: datetime.datetime, end_ts: datetime.datetime, account_id: str):
    host = ACCOUNTS[account_id]
    headers = {"X-Authorization": f"Bearer {jwt_token}"}
    params = {
        "keys": ",".join(keys),
        "startTs": int(start_ts.timestamp() * 1000),
        "endTs": int(end_ts.timestamp() * 1000),
        "interval": 1000,
        "limit": 10000,
        "agg": "NONE"
    }
    url = f"{host}/api/plugins/telemetry/DEVICE/{device_id}/values/timeseries"
    resp = requests.get(url, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()

def fetch_alarms(jwt_token: str, device_id: str,
                 start_ts: datetime.datetime, end_ts: datetime.datetime, account_id: str):
    host = ACCOUNTS[account_id]
    headers = {"X-Authorization": f"Bearer {jwt_token}"}
    url = (
        f"{host}/api/alarm/DEVICE/{device_id}"
        f"?pageSize=100&page=0&fetchOriginator=true"
        f"&startTime={int(start_ts.timestamp()*1000)}"
        f"&endTime={int(end_ts.timestamp()*1000)}"
    )
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json().get("data", [])

def fetch_all_attributes(jwt_token: str, device_id: str, account_id: str) -> dict:
    host = ACCOUNTS[account_id]
    headers = {"X-Authorization": f"Bearer {jwt_token}"}
    combined = {}
    for scope in ("SERVER_SCOPE", "SHARED_SCOPE", "CLIENT_SCOPE"):
        url = f"{host}/api/plugins/telemetry/DEVICE/{device_id}/values/attributes/{scope}"
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        for entry in resp.json():
            combined[entry["key"]] = entry["value"]
    return combined

@router.post("/generate_report/")
def generate_report(
    request: ReportRequest,
    authorization: str = Header(...),
    x_account_id: str = Header(...)
):
    if x_account_id not in ACCOUNTS:
        raise HTTPException(status_code=400, detail="Invalid account ID")

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=400, detail="Missing Bearer token")

    jwt_token = authorization.split(" ", 1)[1]
    user = extract_jwt_user_info(jwt_token, x_account_id)
    authority = user.get("authority", "")
    customer_id = user.get("customerId", {}).get("id", None)
    device_id = get_permitted_device_id(jwt_token, request.device_name, authority, x_account_id, customer_id)

    start_ts = datetime.datetime.strptime(request.start_date, "%Y-%m-%d")
    end_ts = datetime.datetime.strptime(request.end_date, "%Y-%m-%d")

    attrs = fetch_all_attributes(jwt_token, device_id, x_account_id)
    normalized = [VIBE_KEY_MAP.get(k, k) for k in request.data_types]
    telemetry = fetch_telemetry(jwt_token, device_id, normalized, start_ts, end_ts, x_account_id)

    df = pd.concat([
        pd.DataFrame(entries)
          .assign(ts=lambda d: pd.to_datetime(d["ts"], unit="ms"))
          .set_index("ts")
          .rename(columns={"value": key})
        for key, entries in telemetry.items()
    ], axis=1)

    df = df.rename(columns={v: k for k, v in VIBE_KEY_MAP.items()})

    for friendly in request.data_types:
        if friendly not in df.columns:
            df[friendly] = pd.NA

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{request.device_name}_report_{timestamp}.xlsx"
    filepath = os.path.join(os.getcwd(), filename)

    with pd.ExcelWriter(filepath, engine="openpyxl") as writer:
        sheet = "Report"
        attr_df = pd.DataFrame(list(attrs.items()), columns=["Attribute", "Value"])
        attr_df.to_excel(writer, sheet_name=sheet, index=False, startrow=0)

        startrow = len(attr_df) + 3
        tele_df = df.reset_index()
        tele_df.to_excel(writer, sheet_name=sheet, index=False, startrow=startrow, na_rep="N")

        wb = writer.book
        ws = wb[sheet]

        end_attr_row = len(attr_df) + 1
        attr_table = Table(displayName="AttributesTable", ref=f"A1:B{end_attr_row}")
        attr_table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium9", showRowStripes=True)
        ws.add_table(attr_table)

        nrows, ncols = tele_df.shape
        last_col = get_column_letter(ncols)
        tele_ref = f"A{startrow+1}:{last_col}{startrow+nrows}"
        tele_table = Table(displayName="TelemetryTable", ref=tele_ref)
        tele_table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium9", showRowStripes=True)
        ws.add_table(tele_table)

        if request.include_alarms:
            alarms = fetch_alarms(jwt_token, device_id, start_ts, end_ts, x_account_id)
            if alarms:
                alarm_df = pd.DataFrame([{
                    "ts": datetime.datetime.fromtimestamp(a["createdTime"]/1000),
                    "type": a["type"],
                    "severity": a["severity"],
                    "status": a["status"]
                } for a in alarms])
            else:
                alarm_df = pd.DataFrame([{"Notice": "No alarms during selected period."}])
            alarm_df.to_excel(writer, sheet_name="Alarms", index=False)

    return {
        "status": "success",
        "filename": filename,
        "download_url": f"/download/{filename}",
        "sheets": [sheet] + (["Alarms"] if request.include_alarms else [])
    }
