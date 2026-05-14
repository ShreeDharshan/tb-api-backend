# API

## Endpoints

- `GET /healthz`
- `GET /my_devices/`
- `POST /check_alarm/`
- `POST /calculated-telemetry/`
- `POST /generate_report/`
- `GET /download/{filename}`

All existing endpoint contracts are preserved.

## Current ThingsBoard Alarm Payload

`POST /check_alarm/` accepts the current rule-chain vibration fields:

- `acc_total_ms2`
- `acc_total_g`
- `prev_acc_total_g`
- `vibration_delta_g`
- `vibration_level`
- `is_vibrating`

The canonical vibration alarm value is `vibration_delta_g`.

- `> 0.08g` triggers `Vibration Strong Alarm`
- `> 0.15g` triggers `Vibration Shock Alarm`

Raw axis fields such as `x_vibe`, `y_vibe`, and `z_vibe` are retained for diagnostics and
reports, but they are no longer the main vibration alarm trigger.
