import logging
import threading

from src.config import get_settings
from src.tasks.alarm_scheduler import scheduler

logger = logging.getLogger("tasks.runner")


def start_background_tasks() -> None:
    settings = get_settings()
    if not settings.enable_alarm_scheduler:
        logger.info("Alarm scheduler disabled by config")
        return

    thread = threading.Thread(target=scheduler, name="alarm_scheduler", daemon=True)
    thread.start()
    logger.info("Alarm scheduler started")
