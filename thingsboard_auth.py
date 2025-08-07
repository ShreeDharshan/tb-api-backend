import os
import requests
import logging

logger = logging.getLogger("thingsboard_auth")


print("DEBUG: ACCOUNT1_ADMIN_USER =", os.environ.get("ACCOUNT1_ADMIN_USER"))
print("DEBUG: ACCOUNT1_ADMIN_PASS =", os.environ.get("ACCOUNT1_ADMIN_PASS"))

def login_to_thingsboard(base_url: str, username: str, password: str):
    url = f"{base_url}/api/auth/login"
    payload = {"username": username, "password": password}
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
        jwt_token = response.json().get("token")
        return jwt_token
    except requests.exceptions.HTTPError as e:
        logger.error(f"[Auth] Failed to retrieve JWT: {e}")
        return None


def get_admin_jwt(account_id: str, base_url: str) -> str | None:
    """
    Shared function used by multiple files to get JWT token
    """
    username = os.getenv(f"{account_id.upper()}_ADMIN_USER")
    password = os.getenv(f"{account_id.upper()}_ADMIN_PASS")

    if not username or not password:
        logger.warning("[Auth] Missing admin credentials in environment variables.")
        return None

    return login_to_thingsboard(base_url, username, password)
