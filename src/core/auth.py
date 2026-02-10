import logging
import os
from typing import Optional

import requests

logger = logging.getLogger("core.auth")


def login_to_thingsboard(base_url: str, username: str, password: str) -> Optional[str]:
    url = f"{base_url.rstrip('/')}/api/auth/login"
    payload = {"username": username, "password": password}
    try:
        response = requests.post(url, json=payload, timeout=20)
        response.raise_for_status()
        token = response.json().get("token")
        if not token:
            logger.error("Login succeeded but no token returned")
            return None
        return token
    except requests.RequestException as exc:
        logger.error("Failed to retrieve JWT: %s", exc)
        return None


def get_admin_jwt(account_id: Optional[str] = None, base_url: Optional[str] = None) -> Optional[str]:
    account = (account_id or "ACCOUNT1").upper()
    tb_base = (base_url or os.getenv("TB_BASE_URL", "https://thingsboard.cloud")).rstrip("/")

    user_env = f"{account}_ADMIN_USER"
    pass_env = f"{account}_ADMIN_PASS"
    username = os.getenv(user_env)
    password = os.getenv(pass_env)

    if not username or not password:
        logger.warning("Missing admin credentials in env: %s/%s", user_env, pass_env)
        return None

    return login_to_thingsboard(tb_base, username, password)
