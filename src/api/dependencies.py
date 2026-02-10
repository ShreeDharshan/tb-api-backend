from typing import Optional

from fastapi import Header, HTTPException


def get_bearer_token(authorization: Optional[str] = Header(None, alias="Authorization")) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty JWT")
    return token
