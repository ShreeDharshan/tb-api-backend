import logging

from fastapi import APIRouter, Header

from src.models.alarm import AlarmTelemetryPayload
from src.services.alarm_service import process_alarm_payload

logger = logging.getLogger("api.alarms")
router = APIRouter()


@router.post("/check_alarm/")
async def check_alarm(payload: AlarmTelemetryPayload, x_account_id: str = Header(...)):
    logger.info("--- /check_alarm/ invoked ---")
    return process_alarm_payload(payload, x_account_id)
