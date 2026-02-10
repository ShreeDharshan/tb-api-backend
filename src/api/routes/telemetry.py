import logging

from fastapi import APIRouter, Header

from src.models.telemetry import CalculatedTelemetryPayload
from src.services.telemetry_service import process_calculated_telemetry

logger = logging.getLogger("api.telemetry")
router = APIRouter()


@router.post("/calculated-telemetry/")
async def calculate_telemetry(payload: CalculatedTelemetryPayload, x_account_id: str = Header(...)):
    logger.info("--- /calculated-telemetry/ invoked ---")
    logger.info("Payload: %s", payload)
    return process_calculated_telemetry(payload, x_account_id)
