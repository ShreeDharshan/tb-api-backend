from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from starlette.requests import Request
from report_logic import router as report_router, extract_jwt_user_info
from alarm_logic import router as alarm_router
import os
import requests
import logging

# === Logging config ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === FastAPI setup ===
app = FastAPI()

# === CORS for ThingsBoard dashboard ===
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_origin_regex="https?://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === API Routers ===
app.include_router(report_router)
app.include_router(alarm_router)

# === Cloud ThingsBoard host ===
TB_HOST = "https://thingsboard.cloud"

# === Global handler for FastAPI validation errors (422) ===
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
