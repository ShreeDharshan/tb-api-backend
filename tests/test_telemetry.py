from src.models.telemetry import CalculatedTelemetryPayload
from src.services import telemetry_service


def _reset_telemetry_state() -> None:
    telemetry_service._device_state.clear()
    telemetry_service._floor_door_counts.clear()
    telemetry_service._floor_door_durations.clear()


def test_calculated_telemetry_counts_door_open_on_transitions_only(monkeypatch):
    monkeypatch.setenv("TB_ACCOUNTS", '{"account1":"https://thingsboard.cloud"}')
    _reset_telemetry_state()

    base = {
        "deviceName": "N_B1_L07",
        "device_token": "token-1",
        "current_floor_index": 1,
        "lift_status": "Idle",
    }

    telemetry_service.process_calculated_telemetry(
        CalculatedTelemetryPayload(**(base | {"door_open": False, "ts": 100_000})),
        "account1",
    )
    telemetry_service.process_calculated_telemetry(
        CalculatedTelemetryPayload(**(base | {"door_open": True, "ts": 101_000})),
        "account1",
    )
    telemetry_service.process_calculated_telemetry(
        CalculatedTelemetryPayload(**(base | {"door_open": True, "ts": 103_000})),
        "account1",
    )
    final = telemetry_service.process_calculated_telemetry(
        CalculatedTelemetryPayload(**(base | {"door_open": False, "ts": 106_000})),
        "account1",
    )

    calculated = final["calculated"]
    assert calculated["door_open_count_per_floor"][1] == 1
    assert calculated["door_open_duration_per_floor"][1] == 5


def test_calculated_telemetry_keeps_idle_streak_start_stable(monkeypatch):
    monkeypatch.setenv("TB_ACCOUNTS", '{"account1":"https://thingsboard.cloud"}')
    _reset_telemetry_state()

    base = {
        "deviceName": "N_B1_L07",
        "device_token": "token-2",
        "current_floor_index": 1,
        "lift_status": "Idle",
        "door_open": False,
    }

    telemetry_service.process_calculated_telemetry(
        CalculatedTelemetryPayload(**(base | {"ts": 200_000})),
        "account1",
    )
    telemetry_service.process_calculated_telemetry(
        CalculatedTelemetryPayload(**(base | {"ts": 203_000})),
        "account1",
    )
    final = telemetry_service.process_calculated_telemetry(
        CalculatedTelemetryPayload(**(base | {"ts": 206_000})),
        "account1",
    )

    calculated = final["calculated"]
    assert calculated["idle_home_streak"] == 6
    assert calculated["total_idle_home_seconds"] == 6
