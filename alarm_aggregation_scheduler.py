# alarm_aggregation_scheduler.py
"""
Single lightweight scheduler that can drive:
  1) Alarm aggregation/polling loop (interval: TB_SCHEDULER_INTERVAL)
  2) Lift-traffic stats writer (interval: TB_DAILY_STATS_INTERVAL_SEC, lookback: TB_DAILY_STATS_LOOKBACK_SEC)

Both intervals are ENV-configurable so you can test rapidly now and switch to end-of-day later.

This module supports TWO invocation styles:
  A) Old style (blocking): threading.Thread(target=alarm_aggregation_scheduler.scheduler, daemon=True).start()
  B) New style (managed):  alarm_aggregation_scheduler.start_scheduler(); ... stop_scheduler()

Only one loop will run at a time; double-starts are ignored.
"""

import os
import time
import threading
import logging

# Local import in tick to keep import-time light if daily counters are disabled in some envs
def _run_daily_counters_once():
    from daily_counters import run_once_over_window
    return run_once_over_window()

logger = logging.getLogger("alarm_scheduler")
logging.basicConfig(level=logging.INFO)

# ====== ENV knobs ======
ALARM_INTERVAL_SEC = int(os.getenv("TB_SCHEDULER_INTERVAL", "30"))            # existing poller interval
DAILY_INTERVAL_SEC = int(os.getenv("TB_DAILY_STATS_INTERVAL_SEC", "86400"))   # e.g. 900 for 15-min tests
# DAILY_STATS_LOOKBACK_SEC is read inside daily_counters.py

# ====== State ======
_stop_event = threading.Event()
_thread = None
_loop_started = False
_state_lock = threading.Lock()

def _alarm_tick():
    """
    Placeholder for your alarm aggregation work.
    Right now alarms are API-driven from /check_alarm, so this is a hook if you later aggregate/clear periodically.
    """
    logger.debug("[AlarmLoop] tick (interval=%ss)", ALARM_INTERVAL_SEC)
    # no-op for now

def _daily_stats_tick():
    """
    Run the per-device lift-traffic computation over the configured lookback window and write to telemetry.
    """
    logger.info("[DailyLoop] Computing & writing lift-traffic stats (interval=%ss)", DAILY_INTERVAL_SEC)
    _run_daily_counters_once()

def _run_loop():
    logger.info("[Scheduler] Starting; alarm every %ss, daily-stats every %ss",
                ALARM_INTERVAL_SEC, DAILY_INTERVAL_SEC)
    next_alarm = time.time() + ALARM_INTERVAL_SEC
    next_daily = time.time() + DAILY_INTERVAL_SEC

    while not _stop_event.is_set():
        now = time.time()

        # alarm loop
        if now >= next_alarm:
            try:
                _alarm_tick()
            except Exception as e:
                logger.exception("[AlarmLoop] error: %s", e)
            finally:
                next_alarm = now + ALARM_INTERVAL_SEC

        # daily stats loop
        if now >= next_daily:
            try:
                _daily_stats_tick()
            except Exception as e:
                logger.exception("[DailyLoop] error: %s", e)
            finally:
                next_daily = now + DAILY_INTERVAL_SEC

        # sleep a bit (interruptible)
        _stop_event.wait(0.5)

    logger.info("[Scheduler] Loop exited")

# ====== Public APIs ======

def scheduler():
    """
    BLOCKING loop variant for backwards compatibility:
    Use when you want: threading.Thread(target=scheduler, daemon=True).start()
    This function is idempotent; if the managed thread is already running, it returns immediately.
    """
    global _loop_started
    with _state_lock:
        if _loop_started:
            logger.info("[Scheduler] Already running (blocking call ignored)")
            return
        _loop_started = True
        _stop_event.clear()

    try:
        _run_loop()
    finally:
        with _state_lock:
            _loop_started = False
            _stop_event.set()

def start_scheduler():
    """
    Managed, non-blocking start. Safe to call multiple times.
    """
    global _thread, _loop_started
    with _state_lock:
        if _loop_started and _thread and _thread.is_alive():
            logger.info("[Scheduler] Already running")
            return
        logger.info("[Scheduler] Launching background thread")
        _stop_event.clear()
        _loop_started = True
        _thread = threading.Thread(target=_run_loop, name="tb-scheduler", daemon=True)
        _thread.start()

def stop_scheduler():
    """
    Stop the loop and join the background thread (if any).
    """
    global _thread, _loop_started
    with _state_lock:
        if not _loop_started:
            logger.info("[Scheduler] Not running")
            return
        logger.info("[Scheduler] Stopping...")
        _stop_event.set()
    if _thread:
        _thread.join(timeout=5)
    with _state_lock:
        _thread = None
        _loop_started = False
        logger.info("[Scheduler] Stopped")
