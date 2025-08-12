import os
import io
import json
import logging
from typing import List, Dict, Any

import pandas as pd
import requests
from fastapi import APIRouter, Header, HTTPException, Form
from fastapi.responses import JSONResponse

router = APIRouter()
logger = logging.getLogger("report_logic")

TB_HOST = os.getenv("TB_BASE_URL", "https://thingsboard.cloud")


def _bail_400(msg: str):
    raise HTTPException(status_code=400, detail=msg)


@router.post("/generate_report/")
def generate_report(
    # form-data coming from the widget
    startTs: str = Form(...),
    endTs: str = Form(...),
    deviceIds: str = Form(...),  # CSV of device IDs
    fields: str = Form(...),     # JSON array in string form from the widget
    groupBy: str = Form("device"),
    agg: str = Form("avg"),
    # headers
    authorization: str = Header(...),
    account_id: str | None = Header(default=None, alias="X-Account-ID"),
):
    """
    Pull timeseries from ThingsBoard for the requested devices and fields,
    write an .xlsx, and return { download_url, filename } JSON for the widget.
    """
    if not authorization.startswith("Bearer "):
        _bail_400("Missing Bearer token")
    jwt_token = authorization.split(" ", 1)[1]

    # account id is optional now; default if absent to avoid 422
    if not account_id:
        account_id = "account1"

    # parse and validate inputs
    try:
        start_ts = int(startTs)
        end_ts = int(endTs)
    except Exception:
        _bail_400("startTs/endTs must be integers (ms)")

    if end_ts <= start_ts:
        _bail_400("endTs must be greater than startTs")

    try:
        field_list: List[str] = json.loads(fields) if fields else []
        if not isinstance(field_list, list):
            _bail_400("fields must be a JSON array")
        field_list = [f for f in field_list if isinstance(f, str) and f.strip()]
    except Exception:
        _bail_400("fields must be a JSON array")

    dev_ids = [d.strip() for d in deviceIds.split(",") if d.strip()]
    if not dev_ids:
        _bail_400("deviceIds must be a non-empty CSV")

    headers = {"X-Authorization": f"Bearer {jwt_token}"}

    # interval for aggregation: choose a sane default (1 minute) if agg != NONE
    # TB supports agg in [MIN, MAX, AVG, SUM, COUNT, NONE] (case-insensitive)
    agg_map = {
        "min": "MIN", "max": "MAX", "avg": "AVG", "sum": "SUM",
        "count": "COUNT", "none": "NONE"
    }
    agg_final = agg_map.get(str(agg).lower(), "AVG")
    interval_ms = 60_000 if agg_final != "NONE" else 0

    # Pull telemetry
    rows: List[Dict[str, Any]] = []
    keys_param = ",".join(field_list) if field_list else ""

    for dev_id in dev_ids:
        # Device name lookup (so the spreadsheet is friendlier)
        try:
            name_resp = requests.get(f"{TB_HOST}/api/device/{dev_id}", headers=headers)
            name_resp.raise_for_status()
            device_name = name_resp.json().get("name", dev_id)
        except Exception:
            device_name = dev_id

        # Timeseries fetch
        params = {
            "startTs": str(start_ts),
            "endTs": str(end_ts),
            "limit": "100000"
        }
        if keys_param:
            params["keys"] = keys_param
        if agg_final:
            params["agg"] = agg_final
        if interval_ms:
            params["interval"] = str(interval_ms)

        url = f"{TB_HOST}/api/plugins/telemetry/DEVICE/{dev_id}/values/timeseries"
        try:
            resp = requests.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json() or {}
        except requests.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"TB fetch failed for {dev_id}: {e}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error fetching data for {dev_id}: {e}")

        # data is a dict like {"keyA":[{ts:...,value:"..."},...], "keyB":[...]}
        for key, series in data.items():
            if not isinstance(series, list):
                continue
            for point in series:
                ts = point.get("ts")
                value = point.get("value")
                rows.append({
                    "deviceId": dev_id,
                    "deviceName": device_name,
                    "key": key,
                    "ts": ts,
                    "value": value
                })

    # Build DataFrame and pivot/group depending on groupBy
    if rows:
        df = pd.DataFrame(rows)
        # Cast numeric if possible
        with pd.option_context("mode.chained_assignment", None):
            df["value_num"] = pd.to_numeric(df["value"], errors="coerce")

        # Optional: a simple pivot for (deviceName, ts) by key
        try:
            pivot = df.pivot_table(
                index=["deviceName", "deviceId", "ts"],
                columns="key",
                values="value_num",
                aggfunc="mean" if agg_final in ("NONE", "AVG") else "first",
            ).reset_index()
        except Exception:
            # Fallback: just dump raw rows
            pivot = df[["deviceName", "deviceId", "ts", "key", "value"]]
    else:
        pivot = pd.DataFrame(columns=["deviceName", "deviceId", "ts"] + field_list)

    # Write Excel
    filename = f"report_{account_id}_{start_ts}_{end_ts}.xlsx"
    out_path = os.path.join(os.getcwd(), filename)
    try:
        with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
            pivot.to_excel(writer, sheet_name="Data", index=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write Excel: {e}")

    # Respond for widget
    return JSONResponse({
        "download_url": f"/download/{filename}",
        "filename": filename
    })
