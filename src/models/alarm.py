from typing import Optional, Union

from pydantic import BaseModel, Field


class AlarmTelemetryPayload(BaseModel):
    deviceName: str = Field(...)
    floor: str = Field(...)
    timestamp: Optional[Union[int, str]] = Field(default=None)

    height_cm: Optional[Union[float, str]] = Field(default=None)
    height: Optional[Union[float, str]] = Field(default=None)
    current_floor_index: Optional[Union[int, str]] = Field(default=None)
    current_floor_label: Optional[str] = Field(default=None)
    direction: Optional[str] = Field(default=None)
    lift_status: Optional[str] = Field(default=None)

    accX: Optional[Union[float, str]] = Field(default=None)
    accY: Optional[Union[float, str]] = Field(default=None)
    accZ: Optional[Union[float, str]] = Field(default=None)
    x_vibe: Optional[Union[float, str]] = Field(default=None)
    y_vibe: Optional[Union[float, str]] = Field(default=None)
    z_vibe: Optional[Union[float, str]] = Field(default=None)

    acc_total_ms2: Optional[Union[float, str]] = Field(default=None)
    acc_total_g: Optional[Union[float, str]] = Field(default=None)
    prev_acc_total_g: Optional[Union[float, str]] = Field(default=None)
    vibration_delta_g: Optional[Union[float, str]] = Field(default=None)
    vibration_level: Optional[str] = Field(default=None)
    is_vibrating: Optional[Union[bool, int, str]] = Field(default=None)

    # Legacy fields retained for older rule-chain payloads. The current rule chain uses
    # vibration_delta_g as the canonical vibration signal.
    x_jerk: Optional[Union[float, str]] = Field(default=None)
    y_jerk: Optional[Union[float, str]] = Field(default=None)
    z_jerk: Optional[Union[float, str]] = Field(default=None)

    temperature: Optional[Union[float, str]] = Field(default=None)
    humidity: Optional[Union[float, str]] = Field(default=None)

    door_open: Optional[Union[bool, str, int]] = Field(default=None)
    sound_db: Optional[Union[float, str]] = Field(default=None)
    batterySoC: Optional[Union[float, str]] = Field(default=None)
    laser_distance: Optional[Union[float, str]] = Field(default=None)

    # Raw passthrough fields retained for compatibility/debug from rule-chain output.
    microphone_peak_dB: Optional[Union[float, str]] = Field(default=None)
    microphone_rms_dB: Optional[Union[float, str]] = Field(default=None)
    proximity: Optional[Union[float, str, int, bool]] = Field(default=None)

    BatteryAlert: Optional[Union[bool, int, str]] = Field(default=None)
    TemperatureAlert: Optional[Union[bool, int, str]] = Field(default=None)
    HumidityAlert: Optional[Union[bool, int, str]] = Field(default=None)
    SoundAlert: Optional[Union[bool, int, str]] = Field(default=None)
    VibrationAlert: Optional[Union[bool, int, str]] = Field(default=None)

    model_config = {"extra": "ignore"}
