# daily_counters.py
import os
import time
import json
import math
import logging
from typing import Dict, Any, List, Optional, Tuple

import requests
from thingsboard_auth import get_admin_jwt  # must be configured via env

logger = logging.getLogger("daily_counters")
logging.basicConfig(level=logging.INFO)

TB_BASE_URL = os.getenv("TB_BASE_URL", "https://thingsboard.cloud").rstrip("/")

# ====== ENV knobs ======
DAILY_STATS_LOOKBACK_SEC = int(os.getenv("TB_DAILY_STATS_LOOKBACK_SEC", "86400"))
MOVEMENT_THRESHOLD_MM = float(os.getenv("TB_MOVEMENT_THRESHOLD_MM", "50"))

# ====== TB REST helpers ======

def _auth_headers(jwt: str) -> Dict[str, str]:
    return {"X-Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}

def _tb_get(url: str, jwt: str, params: Optional[dict] = None) -> requests.Response:
    return requests.get(url, headers=_auth_headers(jwt), params=params or {}, timeout=45)

def tb_list_devices(jwt: str, page: int, page_size: int = 100) -> Dict[str, Any]:
    """
    Try tenant-wide listing; if forbidden, fall back to /api/user/devices (visible to the JWT).
    """
    # First, try tenant devices
    url = f"{TB_BASE_URL}/api/tenant/devices"
    r = _tb_get(url, jwt, params={"pageSize": page_size, "page": page, "sortProperty": "createdTime", "sortOrder": "DESC"})
    if r.status_code == 200:
        return r.json()
    if r.status_code == 403:
        # Fall back to user devices (paged)
        url2 = f"{TB_BASE_URL}/api/user/devices"
        r2 = _tb_get(url2, jwt, params={"pageSize": page_size, "page": page})
        r2.raise_for_status()
        return r2.json()
    r.raise_for_status()
    return r.json()

def tb_timeseries(jwt: str, device_id: str, keys: List[str], start_ms: int, end_ms: int, limit: int = 200000) -> Dict[str, Any]:
    """
    Fetch raw telemetry (no aggregation). DO NOT send 'agg' without 'interval'—TB Cloud will 400.
    """
    url = f"{TB_BASE_URL}/api/plugins/telemetry/DEVICE/{device_id}/values/timeseries"
    params = {
        "keys": ",".join(keys),
        "startTs": start_ms,
        "endTs": end_ms,
        "limit": limit
        # no 'agg' here
    }
    r = requests.get(url, headers=_auth_headers(jwt), params=params, timeout=45)
    if r.status_code == 400:
        # Some TB plans 400 on extra/unknown params; try the bare-minimum retry
        logger.warning("TB 400 on timeseries; retrying without optional params. url=%s resp=%s", r.url, r.text)
        params_retry = {"keys": ",".join(keys), "startTs": start_ms, "endTs": end_ms}
        r = requests.get(url, headers=_auth_headers(jwt), params=params_retry, timeout=45)
    if r.status_code >= 400:
        # Bubble up with context
        msg = f"TB timeseries {device_id} 400+ ({r.status_code}): {r.text}"
        logger.error(msg)
        r.raise_for_status()
    return r.json()

def tb_save_ts(jwt: str, device_id: str, kv: Dict[str, Any], ts_ms: Optional[int] = None) -> None:
    """
    Write telemetry directly (server-side). Values can be numbers/strings or JSON-serializable.
    """
    url = f"{TB_BASE_URL}/api/plugins/telemetry/DEVICE/{device_id}/timeseries/ANY"
    body = {"ts": ts_ms or int(time.time() * 1000), "values": {}}
    for k, v in kv.items():
        if isinstance(v, (dict, list)):
            body["values"][k] = json.dumps(v, separators=(",", ":"))
        else:
            body["values"][k] = v
    r = requests.post(url, headers=_auth_headers(jwt), data=json.dumps(body), timeout=45)
    if r.status_code >= 400:
        logger.error("TB save_ts failed (%s): %s", r.status_code, r.text)
        r.raise_for_status()

# ====== parsing helpers ======

def _parse_pack_kv(s: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in (s or "").split("|"):
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out

def _safe_float(x: Any, default: float = float("nan")) -> float:
    try:
        v = float(x)
        if math.isfinite(v):
            return v
    except Exception:
        pass
    return default

def _movement_detected(prev_h: float, h: float, threshold_mm: float) -> bool:
    if math.isnan(prev_h) or math.isnan(h):
        return False
    return abs(h - prev_h) > threshold_mm

def _extract_from_pack_out(v: str) -> Tuple[Optional[str], float, Optional[bool]]:
    """
    Pull (floor_label, height_mm, door_open) from pack_out which may be JSON or k=v string.
    """
    floor_label, height_mm, door_open = None, float("nan"), None
    if not v:
        return floor_label, height_mm, door_open
    # try JSON
    try:
        j = json.loads(v)
        floor_label = j.get("current_floor_label") or j.get("floor_label")
        h = j.get("height_mm") or j.get("height") or j.get("height_raw")
        if h is not None:
            height_mm = _safe_float(h)
        if "door_open" in j:
            door_open = bool(j["door_open"])
        elif "door_val" in j:
            door_open = (str(j["door_val"]).upper() == "OPEN")
        return floor_label, height_mm, door_open
    except Exception:
        pass
    # try k=v|k=v
    kv = _parse_pack_kv(v)
    floor_label = kv.get("current_floor_label") or kv.get("floor_label")
    height_mm = _safe_float(kv.get("height_mm") or kv.get("height") or kv.get("height_raw"))
    dv = (kv.get("door_open") or kv.get("door_val") or "").upper()
    if dv != "":
        door_open = True if dv in ("TRUE", "OPEN", "1") else False
    return floor_label, height_mm, door_open

# ====== core compute & write ======

def compute_window_stats_for_device(jwt: str,
                                    device: Dict[str, Any],
                                    start_ms: int,
                                    end_ms: int,
                                    movement_mm_threshold: float = MOVEMENT_THRESHOLD_MM) -> Optional[Dict[str, Any]]:
    """
    Compute from pack_out ONLY over [start_ms, end_ms).
    Returns {'date': 'YYYY-MM-DD', 'door_opens': {...}, 'idle_sec': {...}} or None.
    """
    device_id = device.get("id", {}).get("id")
    device_name = device.get("name")
    if not device_id:
        return None

    try:
        data = tb_timeseries(jwt, device_id, ["pack_out"], start_ms, end_ms)
    except Exception as e:
        logger.error("Timeseries fetch failed for %s (%s): %s", device_name, device_id, e)
        raise

    series = data.get("pack_out") or []
    if len(series) < 2:
        # Not enough data in window
        return None

    samples: List[Tuple[int, Optional[str], float, Optional[bool]]] = []
    for item in series:
        ts = int(item["ts"])
        fl, h, d = _extract_from_pack_out(item.get("value", ""))
        samples.append((ts, fl, h, d))
    samples.sort(key=lambda x: x[0])

    door_opens: Dict[str, int] = {}
    idle_ms: Dict[str, int] = {}

    prev_ts, prev_floor, prev_h, prev_door_open = samples[0]
    for i in range(1, len(samples)):
        ts, fl, h, dopen = samples[i]
        dt = ts - prev_ts
        floor = (fl or prev_floor or "UNKNOWN")

        # rising edge: door CLOSED -> OPEN
        if prev_door_open in (False, 0) and dopen in (True, 1):
            door_opens[floor] = door_opens.get(floor, 0) + 1

        # idle accumulation: doors closed and not moving
        if (dopen in (False, 0) and prev_door_open in (False, 0)) and not _movement_detected(prev_h, h, movement_mm_threshold):
            idle_ms[floor] = idle_ms.get(floor, 0) + dt

        prev_ts, prev_floor, prev_h, prev_door_open = ts, floor, h, dopen

    # Label by end-of-window date (locality not applied here)
    end_date = time.strftime("%Y-%m-%d", time.gmtime((end_ms - 1) / 1000.0))
    door_opens = {str(k): int(v) for k, v in door_opens.items()}
    idle_sec = {str(k): round(ms / 1000.0) for k, ms in idle_ms.items()}

    return {"date": end_date, "door_opens": door_opens, "idle_sec": idle_sec}

def write_stats(jwt: str, device_id: str, date_str: str, stats: Dict[str, Any], write_ts_ms: int) -> None:
    payload = {
        "daily_floor_door_opens": stats.get("door_opens", {}),
        "daily_floor_idle_sec": stats.get("idle_sec", {}),
        "daily_floor_summary": {"date": date_str, **stats}
    }
    tb_save_ts(jwt, device_id, payload, ts_ms=write_ts_ms)

def run_once_over_window(now_ms: Optional[int] = None) -> Dict[str, Any]:
    """
    Run the computation over the last TB_DAILY_STATS_LOOKBACK_SEC seconds and write results for all devices.
    """
    jwt = get_admin_jwt()
    now_ms = now_ms or int(time.time() * 1000)
    start_ms = now_ms - (DAILY_STATS_LOOKBACK_SEC * 1000)
    end_ms = now_ms
    date_str = time.strftime("%Y-%m-%d", time.gmtime((end_ms - 1) / 1000.0))
    write_ts_ms = end_ms - 1  # stamp at the end of the window

    logger.info("DailyCounters(window): start=%d end=%d (len=%ds) date=%s",
                start_ms, end_ms, DAILY_STATS_LOOKBACK_SEC, date_str)

    page = 0
    total_devices = 0
    processed = 0
    skipped_no_data = 0
    results = []

    # page through devices (tenant or user-visible)
    while True:
        page_data = tb_list_devices(jwt, page)
        devices = page_data.get("data") or []
        if not devices:
            break
        for dev in devices:
            total_devices += 1
            device_id = (dev.get("id") or {}).get("id")
            device_name = dev.get("name")
            try:
                stats = compute_window_stats_for_device(jwt, dev, start_ms, end_ms)
                if not stats:
                    skipped_no_data += 1
                    continue
                write_stats(jwt, device_id, date_str, stats, write_ts_ms)
                processed += 1
                results.append({
                    "deviceName": device_name,
                    "deviceId": device_id,
                    "date": date_str,
                    "door_opens": stats["door_opens"],
                    "idle_sec": stats["idle_sec"]
                })
            except Exception as e:
                logger.error("DailyCounters: device %s (%s) failed: %s", device_name, device_id, e)

        if page_data.get("hasNext") is True:
            page += 1
        else:
            break

    summary = {
        "date": date_str,
        "lookback_sec": DAILY_STATS_LOOKBACK_SEC,
        "interval_sec": int(os.getenv("TB_DAILY_STATS_INTERVAL_SEC", "86400")),
        "total_devices": total_devices,
        "processed": processed,
        "skipped_no_data": skipped_no_data
    }
    logger.info("DailyCounters(window) done: %s", summary)
    return {"summary": summary, "results": results}
