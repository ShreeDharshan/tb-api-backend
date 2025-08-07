import os
import logging
import requests

logger = logging.getLogger("thingsboard_auth")

# DEBUG: Print values at startup
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
