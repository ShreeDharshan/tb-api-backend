from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel
import requests
import pandas as pd
import datetime
import os
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.utils import get_column_letter
import logging


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()


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

class ReportRequest(BaseModel):
    device_name: str
    data_types: list[str]
    include_alarms: bool
    start_date: str
    end_date: str

@router.post("/generate-report/")
async def generate_report(req: ReportRequest, request: Request):
    """
    Generates a telemetry report for a device between given dates.
    Supports multiple ThingsBoard accounts via X-TB-Account header.
    """
    logger.info("--- /generate-report/ invoked ---")
    account_key = request.headers.get("X-TB-Account", "account1")
    creds = get_account_creds(account_key)
    token = get_admin_token(account_key)
    host = creds['host']

    # === Prepare API call ===
    start_ts = int(datetime.datetime.strptime(req.start_date, "%Y-%m-%d").timestamp() * 1000)
    end_ts = int(datetime.datetime.strptime(req.end_date, "%Y-%m-%d").timestamp() * 1000)

    # Get telemetry data
    url = f"{host}/api/plugins/telemetry/DEVICE/{req.device_name}/values/timeseries"
    params = {
        "keys": ",".join(req.data_types),
        "startTs": start_ts,
        "endTs": end_ts
    }
    headers = {"X-Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers, params=params)

    if response.status_code != 200:
        logger.error(f"Failed to fetch telemetry: {response.text}")
        raise HTTPException(status_code=response.status_code, detail="Failed to fetch telemetry data")

    telemetry_data = response.json()

    # Optionally get alarms
    alarms = []
    if req.include_alarms:
        alarms_url = f"{host}/api/alarm/DEVICE/{req.device_name}"
        alarms_response = requests.get(alarms_url, headers=headers)
        if alarms_response.status_code == 200:
            alarms = alarms_response.json()
        else:
            logger.warning("Failed to fetch alarms, continuing without alarms.")

    # Convert to DataFrame
    df = pd.DataFrame()
    for key, values in telemetry_data.items():
        if not values:
            continue
        series = pd.DataFrame(values)
        series['key'] = key
        df = pd.concat([df, series], ignore_index=True)

    if df.empty:
        return {"status": "success", "message": "No data available for the selected timeframe"}

    # Convert timestamps
    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    df.rename(columns={'ts': 'Timestamp', 'value': 'Value', 'key': 'Telemetry Key'}, inplace=True)

    # Save to Excel
    file_path = f"/tmp/report_{req.device_name}_{account_key}.xlsx"
    with pd.ExcelWriter(file_path, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name="Telemetry Data")
        ws = writer.sheets["Telemetry Data"]

        # Create table
        table = Table(displayName="TelemetryTable", ref=f"A1:{get_column_letter(df.shape[1])}{len(df)+1}")
        style = TableStyleInfo(name="TableStyleMedium9", showFirstColumn=False,
                               showLastColumn=False, showRowStripes=True, showColumnStripes=True)
        table.tableStyleInfo = style
        ws.add_table(table)

        # Optionally include alarms in another sheet
        if alarms:
            alarms_df = pd.DataFrame(alarms)
            alarms_df.to_excel(writer, index=False, sheet_name="Alarms")

    return {"status": "success", "report_path": file_path}
