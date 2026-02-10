from typing import Optional, Union

from pydantic import BaseModel, Field


class AlarmTelemetryPayload(BaseModel):
    deviceName: str = Field(...)
    floor: str = Field(...)
    timestamp: Optional[Union[int, str]] = Field(default=None)

    height: Optional[Union[float, str]] = Field(default=None)
    current_floor_index: Optional[Union[int, str]] = Field(default=None)

    x_vibe: Optional[Union[float, str]] = Field(default=None)
    y_vibe: Optional[Union[float, str]] = Field(default=None)
    z_vibe: Optional[Union[float, str]] = Field(default=None)

    x_jerk: Optional[Union[float, str]] = Field(default=None)
    y_jerk: Optional[Union[float, str]] = Field(default=None)
    z_jerk: Optional[Union[float, str]] = Field(default=None)

    temperature: Optional[Union[float, str]] = Field(default=None)
    humidity: Optional[Union[float, str]] = Field(default=None)

    door_open: Optional[Union[bool, str, int]] = Field(default=None)
    sound_db: Optional[Union[float, str]] = Field(default=None)
