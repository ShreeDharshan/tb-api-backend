import re
from datetime import date, datetime
from typing import Any, List

from pydantic import BaseModel, Field, field_validator

ALLOWED_TYPES = {
    "height",
    "direction",
    "lift_status",
    "current_floor_index",
    "current_floor_label",
    "x_vibe",
    "y_vibe",
    "z_vibe",
    "x_jerk",
    "y_jerk",
    "z_jerk",
}


def parse_any_date(value: Any) -> date:
    if value is None or value == "":
        raise ValueError("missing date")

    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        timestamp = int(value)
        if timestamp > 10_000_000_000:
            dt = datetime.utcfromtimestamp(timestamp / 1000.0)
        else:
            dt = datetime.utcfromtimestamp(timestamp)
        return dt.date()

    if isinstance(value, date) and not isinstance(value, datetime):
        return value

    raw = str(value).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return datetime.strptime(raw, "%Y-%m-%d").date()

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except Exception as exc:
        raise ValueError(f"unrecognized date format: {value!r}") from exc


class ReportRequestModel(BaseModel):
    device_name: str = Field(..., alias="deviceName")
    data_types: List[str] = Field(..., alias="dataTypes")
    include_alarms: bool = Field(True, alias="includeAlarms")
    start_date: Any = Field(..., alias="startDate")
    end_date: Any = Field(..., alias="endDate")

    model_config = {
        "populate_by_name": True,
        "extra": "ignore",
        "str_min_length": 1,
    }

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def coerce_dates(cls, value):
        return parse_any_date(value)

    @field_validator("data_types", mode="after")
    @classmethod
    def filter_types(cls, values: List[str]):
        if not values:
            raise ValueError("data_types cannot be empty")
        filtered = [item for item in values if item in ALLOWED_TYPES]
        if not filtered:
            raise ValueError("No valid data_types provided")

        seen = set()
        deduped = []
        for item in filtered:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped
