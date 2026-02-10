# Deployment (Render)

## Render service settings

- Build command: `pip install --upgrade pip && pip install -r requirements.txt`
- Start command: `uvicorn src.main:app --host 0.0.0.0 --port $PORT`

## Why this works

Render runs the `src` app directly, so deploys stay simple and avoid legacy entrypoint indirection.
