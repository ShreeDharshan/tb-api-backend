# Architecture

## Runtime

1. Render starts `uvicorn src.main:app`.
2. `src/main.py` wires middleware, routes, and startup/shutdown tasks.
3. Scheduler uses `src/services/aggregation_service.py`.

## Layering

- API: `src/api/routes/*`
- Services: `src/services/*`
- Models: `src/models/*`
- Core infra: `src/core/*`
- Tasks: `src/tasks/*`

## Migration Status

- Legacy top-level logic files have been removed.
- Active business logic lives in `src/services`.

## Next Hardening Steps

1. Add full unit tests for alarm and telemetry services.
2. Replace in-memory state with Redis/Postgres for multi-instance consistency.
3. Replace placeholder report rows with real ThingsBoard query and transform.
