import os
import re
import time
import uuid
from datetime import datetime
from typing import Any, Dict

import pandas as pd

from src.config import get_settings
from src.models.report import ReportRequestModel


def ensure_report_dir() -> str:
    report_dir = get_settings().report_dir
    os.makedirs(report_dir, exist_ok=True)
    return report_dir


def safe_filename(base: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._-")
    return value or "report"


def make_filename(device_name: str, start_date, end_date) -> str:
    base = safe_filename(f"{device_name}_{start_date.isoformat()}_{end_date.isoformat()}")
    return f"{base}_{uuid.uuid4().hex[:8]}.xlsx"


def fake_rows_for_now(req: ReportRequestModel) -> pd.DataFrame:
    rows = []
    for item in req.data_types:
        rows.append(
            {
                "timestamp": int(
                    time.mktime(datetime.combine(req.start_date, datetime.min.time()).timetuple())
                )
                * 1000,
                "device": req.device_name,
                "key": item,
                "value": None,
                "note": "placeholder row - replace with real data",
            }
        )
        rows.append(
            {
                "timestamp": int(
                    time.mktime(datetime.combine(req.end_date, datetime.min.time()).timetuple())
                )
                * 1000,
                "device": req.device_name,
                "key": item,
                "value": None,
                "note": "placeholder row - replace with real data",
            }
        )
    return pd.DataFrame(rows)


def generate_report_file(body: ReportRequestModel, x_tb_account: str | None) -> Dict[str, Any]:
    if body.end_date < body.start_date:
        raise ValueError("end_date cannot be before start_date")

    data_df = fake_rows_for_now(body)
    metadata = {
        "device_name": body.device_name,
        "data_types": ",".join(body.data_types),
        "include_alarms": body.include_alarms,
        "start_date": body.start_date.isoformat(),
        "end_date": body.end_date.isoformat(),
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "account": x_tb_account or "",
    }
    metadata_df = pd.DataFrame([metadata])

    report_dir = ensure_report_dir()
    filename = make_filename(body.device_name, body.start_date, body.end_date)
    file_path = os.path.join(report_dir, filename)

    with pd.ExcelWriter(file_path, engine="openpyxl") as writer:
        data_df.to_excel(writer, index=False, sheet_name="data")
        metadata_df.to_excel(writer, index=False, sheet_name="meta")

    return {"filename": filename, "download_url": f"/download/{filename}"}


def resolve_report_path(filename: str) -> str:
    safe = safe_filename(filename)
    if safe != filename:
        raise ValueError("Invalid filename")

    report_dir = ensure_report_dir()
    file_path = os.path.join(report_dir, filename)
    if not os.path.exists(file_path):
        raise FileNotFoundError("File not found")
    return file_path
