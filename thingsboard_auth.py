import os
import requests
import logging
from pydantic import BaseModel, Field


logger = logging.getLogger("thingsboard_auth")

# === Environment Variables ===
THINGSBOARD_URL = os.getenv("TB_BASE_URL", "https://thingsboard.cloud")
ADMIN_USER = os.getenv("TB_ADMIN_USER", "")
ADMIN_PASS = os.getenv("TB_ADMIN_PASS", "")

if not ADMIN_USER or not ADMIN_PASS:
    logger.warning("[Auth] TB_ADMIN_USER or TB_ADMIN_PASS not set in environment variables.")

def get_admin_jwt():
    """
    Retrieves an admin JWT token from ThingsBoard for API authentication.
    Uses credentials from environment variables.
    """
    url = f"{THINGSBOARD_URL}/api/auth/login"
    payload = {
        "username": ADMIN_USER,
        "password": ADMIN_PASS
    }
    try:
        resp = requests.post(url, json=payload, timeout=5)
        resp.raise_for_status()
        token = resp.json().get("token")
        if token:
            logger.info("[Auth] Admin JWT retrieved successfully.")
            return token
        else:
            logger.error("[Auth] No token found in response.")
            return None
    except requests.RequestException as e:
        logger.error(f"[Auth] Failed to retrieve JWT: {e}")
        return None
