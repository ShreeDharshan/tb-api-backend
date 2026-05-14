from typing import Optional, Union

from pydantic import BaseModel, Field


class CalculatedTelemetryPayload(BaseModel):
    deviceName: str = Field(...)
    device_token: str = Field(...)
    current_floor_index: int = Field(...)
    home_floor: Optional[Union[int, str]] = Field(default=None)
    lift_status: str = Field(...)
    door_open: Optional[bool] = Field(default=False)
    ts: Optional[int] = Field(default=None)
