# ThingBoard API Backend

FastAPI backend for ThingsBoard rule-chain extensions (advanced calculations, alarms, and report generation).

## Architecture

Production code now runs from `src/`:

- `src/api/routes`: endpoint layer
- `src/services`: business/domain logic
- `src/models`: Pydantic request/response models
- `src/core`: auth, logging, shared exceptions
- `src/tasks`: scheduler/background orchestration
- `src/utils`: shared helpers

## Run Locally

1. `cp .env.example .env`
2. `pip install -r requirements.txt`
3. `uvicorn src.main:app --reload --host 0.0.0.0 --port 10000`

## Render Deployment

Use existing Render setup.

- Start command: `uvicorn src.main:app --host 0.0.0.0 --port $PORT`

This keeps GitHub push + Render redeploy behavior unchanged.

## Endpoint Compatibility

- `GET /healthz`
- `GET /my_devices/`
- `POST /check_alarm/`
- `POST /calculated-telemetry/`
- `POST /generate_report/`
- `GET /download/{filename}`
