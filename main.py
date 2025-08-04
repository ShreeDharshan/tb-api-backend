from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from starlette.requests import Request
from report_logic import router as report_router, extract_jwt_user_info
from alarm_logic import router as alarm_router
from calculated_telemetry import router as calculated_router  
import os
import requests
import logging


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


app = FastAPI()

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

def get_account_creds(account_key):
    creds = THINGSBOARD_ACCOUNTS.get(account_key)
    if not creds:
        raise HTTPException(status_code=400, detail="Invalid account key")
    return creds

def get_admin_token(account_key):
    creds = get_account_creds(account_key)
    response = requests.post(
        f"{creds['host']}/api/auth/login",
        json={"username": creds['username'], "password": creds['password']}
    )
    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail="Failed login")
    return response.json().get("token")


@app.middleware("http")
async def add_account_context(request: Request, call_next):
    account_key = request.headers.get("X-TB-Account", "account1")
    request.state.account_key = account_key
    request.state.tb_token = get_admin_token(account_key)
    response = await call_next(request)
    return response



app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_origin_regex="https?://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(report_router)
app.include_router(alarm_router)
app.include_router(calculated_router)  


TB_HOST = "https://thingsboard.cloud"


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.error(f"[VALIDATION ERROR] {exc}")
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder({
            "detail": exc.errors(),
            "body": exc.body
        }),
    )

# === Device list for widgets ===
@app.get("/my_devices/")
def get_my_devices(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=400, detail="Missing Bearer token")
    jwt_token = authorization.split(" ", 1)[1]

    user_info   = extract_jwt_user_info(jwt_token)
    authority   = user_info.get("authority", "")
    customer_id = user_info.get("customerId", {}).get("id", "")

    headers = {"X-Authorization": f"Bearer {jwt_token}"}
    if authority == "CUSTOMER_USER":
        url = f"{TB_HOST}/api/customer/{customer_id}/deviceInfos?pageSize=1000&page=0"
    elif authority == "TENANT_ADMIN":
        url = f"{TB_HOST}/api/tenant/devices?pageSize=1000&page=0"
    else:
        raise HTTPException(status_code=403, detail=f"Unsupported authority: {authority}")

    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    devices = resp.json().get("data", [])
    return [{"name": d["name"], "id": d["id"]["id"]} for d in devices]

# === XLS download endpoint ===
@app.get("/download/{filename}")
def download_csv(filename: str):
    file_path = os.path.join(os.getcwd(), filename)
    if os.path.exists(file_path):
        response = FileResponse(
            path=file_path,
            filename=filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response
    raise HTTPException(status_code=404, detail="File not found")

@app.get("/healthcheck")
def health_check():
    return {"status": "ok"}
