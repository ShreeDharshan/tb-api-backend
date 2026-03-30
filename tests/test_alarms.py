import src.services.alarm_service as alarm_service


def _reset_alarm_state() -> None:
    alarm_service._device_cache.clear()
    alarm_service._bucket_counts.clear()
    alarm_service._device_door_state.clear()
    alarm_service._door_open_since.clear()


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
            "height_cm": 149.2,
            "current_floor_index": 1,
            "x_vibe": 0.182,
            "y_vibe": 0.091,
            "z_vibe": 1.287,
            "temperature": 42.6,
            "humidity": 78.3,
            "door_open": False,
            "sound_db": 91.8,
            "microphone_peak_dB": 91.8,
            "microphone_rms_dB": 67.2,
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
            "height_cm": 0,
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


def test_check_alarm_height_fallback_field_treated_as_cm(client, monkeypatch):
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

    base_payload = {
        "deviceName": "N_B1_L07",
        "floor": "2",
        "timestamp": 1736055123000,
        "height": 123.4,
        "current_floor_index": 3,
        "x_vibe": 6.0,
    }

    first = client.post("/check_alarm/", headers={"X-Account-ID": "account1"}, json=base_payload)
    second = client.post(
        "/check_alarm/",
        headers={"X-Account-ID": "account1"},
        json={**base_payload, "height": 127.9},
    )
    third = client.post(
        "/check_alarm/",
        headers={"X-Account-ID": "account1"},
        json={**base_payload, "height": 119.5},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 200

    payload = third.json()
    bucket_alarms = [entry for entry in payload["alarms_triggered"] if entry["type"] == "x_vibe Alarm"]
    assert len(bucket_alarms) == 1
    assert bucket_alarms[0]["height_zone"].endswith("cm")
