import os
from dotenv import load_dotenv

# load .env if present (local dev, not harmful on Render)
load_dotenv()

# ThingsBoard base URLs — used by scheduler AND by any code that imports config
TB_ACCOUNTS = {
    "account1": os.getenv("ACCOUNT1_BASE_URL", "https://thingsboard.cloud"),
    "account2": os.getenv("ACCOUNT2_BASE_URL", "https://thingsboard.cloud"),
    "account3": os.getenv("ACCOUNT3_BASE_URL", "https://thingsboard.cloud"),
}

# Admin creds per account (used by thingsboard_auth / schedulers)
ACCOUNT1_ADMIN_USER = os.getenv("ACCOUNT1_ADMIN_USER")
ACCOUNT1_ADMIN_PASS = os.getenv("ACCOUNT1_ADMIN_PASS")

ACCOUNT2_ADMIN_USER = os.getenv("ACCOUNT2_ADMIN_USER")
ACCOUNT2_ADMIN_PASS = os.getenv("ACCOUNT2_ADMIN_PASS")

ACCOUNT3_ADMIN_USER = os.getenv("ACCOUNT3_ADMIN_USER")
ACCOUNT3_ADMIN_PASS = os.getenv("ACCOUNT3_ADMIN_PASS")

# Idle flush interval for calculated telemetry (seconds)
# default: 6 hours = 21600 sec
IDLE_FLUSH_INTERVAL_SEC = int(os.getenv("IDLE_FLUSH_INTERVAL_SEC", "21600"))
