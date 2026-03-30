from src.models.report import ReportRequestModel


def test_report_allows_new_rule_chain_keys():
    model = ReportRequestModel.model_validate(
        {
            "deviceName": "N_B1_L07",
            "dataTypes": [
                "temperature",
                "humidity",
                "door_open",
                "sound_db",
                "laser_distance",
                "microphone_peak_dB",
                "microphone_rms_dB",
                "proximity",
                "batterySoC",
                "BatteryAlert",
                "TemperatureAlert",
                "HumidityAlert",
                "SoundAlert",
                "VibrationAlert",
                "not_a_real_key",
            ],
            "includeAlarms": True,
            "startDate": "2026-01-01",
            "endDate": "2026-01-02",
        }
    )

    assert model.data_types == [
        "temperature",
        "humidity",
        "door_open",
        "sound_db",
        "laser_distance",
        "microphone_peak_dB",
        "microphone_rms_dB",
        "proximity",
        "batterySoC",
        "BatteryAlert",
        "TemperatureAlert",
        "HumidityAlert",
        "SoundAlert",
        "VibrationAlert",
    ]
