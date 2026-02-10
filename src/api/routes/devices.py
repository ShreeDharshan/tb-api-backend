from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from src.api.dependencies import get_bearer_token
from src.services.thingsboard_service import (
    choose_base_url,
    normalize_device_rows,
    page_all,
    tb_get,
)

router = APIRouter()


@router.get("/my_devices/")
def get_my_devices(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_tb_account: Optional[str] = Header(None, alias="X-TB-Account"),
):
    jwt = get_bearer_token(authorization)
    base_url = choose_base_url(x_tb_account)

    me = tb_get(base_url, "/api/auth/user", jwt)
    if not isinstance(me, dict):
        raise HTTPException(status_code=500, detail="Unexpected /api/auth/user response")

    authority = str(me.get("authority", "")).upper()
    customer_obj = me.get("customerId") if isinstance(me.get("customerId"), dict) else None
    customer_id = (customer_obj or {}).get("id") if isinstance(customer_obj, dict) else None

    if authority == "TENANT_ADMIN":
        all_devices = page_all(
            lambda page=0, pageSize=100: tb_get(
                base_url,
                "/api/tenant/devices",
                jwt,
                params={"page": page, "pageSize": pageSize},
            )
        )
        return normalize_device_rows(all_devices)

    if customer_id:
        all_devices = page_all(
            lambda page=0, pageSize=100: tb_get(
                base_url,
                f"/api/customer/{customer_id}/devices",
                jwt,
                params={"page": page, "pageSize": pageSize},
            )
        )
        return normalize_device_rows(all_devices)

    all_devices = page_all(
        lambda page=0, pageSize=100: tb_get(
            base_url,
            "/api/user/devices",
            jwt,
            params={"page": page, "pageSize": pageSize},
        )
    )
    return normalize_device_rows(all_devices)
