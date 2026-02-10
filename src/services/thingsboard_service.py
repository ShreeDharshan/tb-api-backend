from typing import Dict, List, Optional

import requests
from fastapi import HTTPException

from src.config import parse_tb_accounts

HTTP_TIMEOUT = 20


def choose_base_url(x_tb_account: Optional[str]) -> str:
    accounts = parse_tb_accounts()
    if x_tb_account and x_tb_account in accounts:
        return accounts[x_tb_account]
    if x_tb_account and x_tb_account.lower() in accounts:
        return accounts[x_tb_account.lower()]
    return next(iter(accounts.values()))


def tb_get(base_url: str, path: str, jwt: str, params: Optional[dict] = None) -> dict:
    url = f"{base_url.rstrip('/')}" + path
    headers = {"X-Authorization": f"Bearer {jwt}"}
    response = requests.get(url, headers=headers, params=params or {}, timeout=HTTP_TIMEOUT)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=f"TB GET {path} failed: {response.text}")
    return response.json()


def page_all(fetch_page, page_size: int = 100) -> List[dict]:
    results: List[dict] = []
    page = 0
    while True:
        data = fetch_page(page=page, pageSize=page_size)
        if not isinstance(data, dict):
            break
        chunk = data.get("data") or []
        if isinstance(chunk, list):
            results.extend(chunk)
        if not data.get("hasNext", False):
            break
        page += 1
    return results


def normalize_device_rows(items: List[dict]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        row_id = row.get("id") if isinstance(row.get("id"), dict) else {}
        device_id = row_id.get("id")
        name = row.get("name")
        if isinstance(device_id, str) and isinstance(name, str):
            out.append({"id": device_id, "name": name})
    return out
