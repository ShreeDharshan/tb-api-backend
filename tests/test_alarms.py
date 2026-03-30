import src.services.alarm_service as alarm_service


def _reset_alarm_state() -> None:
    alarm_service._device_cache.clear()
    alarm_service._bucket_counts.clear()
    alarm_service._device_door_state.clear()
    alarm_service._door_open_since.clear()
    alarm_service._floor_boundaries_cache.clear()


def test_check_alarm_accepts_new_payload_and_triggers_sound_humidity(client, monkeypatch):
    monkeypatch.setenv("TB_ACCOUNTS", '{"account1":"https://thingsboard.cloud"}')
    _reset_alarm_state()

    created = []

    def fake_create(device_name, alarm_type, ts_ms, severity, details, account_id):
        created.append(
            {
                "device_name": device_name,
                "alarm_type": alarm_type,
                "ts_ms": ts_ms,
                "severity": severity,
                "details": details,
                "account_id": account_id,
            }
        )

    monkeypatch.setattr(alarm_service, "_create_alarm_on_tb", fake_create)

    response = client.post(
        "/check_alarm/",
        headers={"X-Account-ID": "account1"},
        json={
            "deviceName": "N_B1_L07",
            "floor": "Ground",
            "timestamp": 1736055123000,
            "height": 15000,
            "current_floor_index": 1,
            "x_vibe": 0.182,
            "y_vibe": 0.091,
            "z_vibe": 1.287,
            "temperature": 42.6,
            "humidity": 78.3,
            "door_open": False,
            "sound_db": 91.8,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    triggered_types = {entry["type"] for entry in payload["alarms_triggered"]}
    assert "Humidity Alarm" in triggered_types
    assert "Sound Alarm" in triggered_types

    created_types = {entry["alarm_type"] for entry in created}
    assert "Humidity Alarm" in created_types
    assert "Sound Alarm" in created_types


def test_check_alarm_triggers_battery_low_alarm(client, monkeypatch):
    monkeypatch.setenv("TB_ACCOUNTS", '{"account1":"https://thingsboard.cloud"}')
    _reset_alarm_state()

    created = []

    def fake_create(device_name, alarm_type, ts_ms, severity, details, account_id):
        created.append(
            {
                "device_name": device_name,
                "alarm_type": alarm_type,
                "ts_ms": ts_ms,
                "severity": severity,
                "details": details,
                "account_id": account_id,
            }
        )

    monkeypatch.setattr(alarm_service, "_create_alarm_on_tb", fake_create)
    monkeypatch.setattr(alarm_service, "_get_device_id", lambda *_args, **_kwargs: None)

    response = client.post(
        "/check_alarm/",
        headers={"X-Account-ID": "account1"},
        json={
            "deviceName": "N_B1_L07",
            "floor": "Basement",
            "timestamp": 1736055123000,
            "height": 0,
            "current_floor_index": 0,
            "batterySoC": 18,
            "door_open": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    triggered_types = {entry["type"] for entry in payload["alarms_triggered"]}
    assert "Battery Low Alarm" in triggered_types

    battery_creates = [entry for entry in created if entry["alarm_type"] == "Battery Low Alarm"]
    assert len(battery_creates) == 1
    assert battery_creates[0]["severity"] == "MAJOR"
