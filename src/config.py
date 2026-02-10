import json
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List

from dotenv import load_dotenv

load_dotenv()

DEFAULT_TB_BASE_URL = "https://thingsboard.cloud"


def parse_tb_accounts() -> Dict[str, str]:
    raw = os.getenv("TB_ACCOUNTS", "").strip()
    if raw:
        try:
            value = json.loads(raw)
            if isinstance(value, dict) and value:
                return {
                    str(key).strip(): str(base_url).strip().rstrip("/")
                    for key, base_url in value.items()
                }
        except json.JSONDecodeError:
            pass

    fallback = os.getenv("TB_BASE_URL", DEFAULT_TB_BASE_URL).strip().rstrip("/")
    return {"default": fallback}


def _split_csv(raw: str) -> List[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    app_name: str
    app_version: str
    app_debug: bool
    log_level: str
    cors_allow_origins: List[str]
    enable_alarm_scheduler: bool
    tb_scheduler_interval: int
    report_dir: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    cors_raw = os.getenv("CORS_ALLOW_ORIGINS", "*").strip()
    return Settings(
        app_name=os.getenv("APP_NAME", "TB API Backend"),
        app_version=os.getenv("APP_VERSION", "1.1.0"),
        app_debug=os.getenv("APP_DEBUG", "false").lower() in {"1", "true", "yes", "on"},
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        cors_allow_origins=["*"] if cors_raw == "*" else _split_csv(cors_raw),
        enable_alarm_scheduler=os.getenv("ENABLE_ALARM_SCHEDULER", "true").lower()
        in {"1", "true", "yes", "on"},
        tb_scheduler_interval=int(os.getenv("TB_SCHEDULER_INTERVAL", "30")),
        report_dir=os.getenv("REPORT_DIR", "/tmp"),
    )
