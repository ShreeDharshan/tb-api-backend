from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
import os
import requests
import logging

from report_logic import router as report_router
from alarm_logic import router as alarm_router
from calculated_telemetry import router as calculated_router

# === Logging config ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === FastAPI setup ===
app = FastAPI()

# === CORS ===
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === Multi-account credentials ===
THINGSBOARD_ACCOUNTS = {
    "account1": {
        "host": os.getenv("TB1_HOST"),
        "username": os.getenv("TB1_USERNAME"),
        "password": os.getenv("TB1_PASSWORD")
    },
    "account2": {
        "host": os.getenv("TB2_HOST"),
        "username": os.getenv("TB2_USERNAME"),
        "password": os.getenv("TB2_PASSWORD")
    }
}

def get_account_creds(account_key: str):
    creds = THINGSBOARD_ACCOUNTS.get(account_key)
    if not creds:
        raise HTTPException(status_code=400, detail=f"Invalid account key: {account_key}")
    return creds

def get_admin_token(account_key: str) -> str:
    creds = get_account_creds(account_key)
    response = requests.post(
        f"{creds['host']}/api/auth/login",
        json={"username": creds['username'], "password": creds['password']}
    )
    if response.status_code != 200:
        logger.error(f"Failed to login for account {account_key}: {response.text}")
        raise HTTPException(status_code=response.status_code, detail="Failed to login to ThingsBoard")
    return response.json().get("token")

# === Middleware for account context ===
@app.middleware("http")
async def add_account_context(request: Request, call_next):
    account_key = request.headers.get("X-TB-Account", "account1")
    request.state.account_key = account_key
    request.state.tb_token = get_admin_token(account_key)
    response = await call_next(request)
    return response

# === Routers ===
app.include_router(report_router)
app.include_router(alarm_router)
app.include_router(calculated_router)

# === Exception handlers ===
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder({"detail": exc.errors(), "body": exc.body}),
    )

@app.get("/")
async def root():
    return {"status": "running", "message": "IoT Lift Monitoring API with multi-account support is active."}
