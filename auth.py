"""Auth models, JWT helpers, rate limiter, and FastAPI dependency functions."""

from datetime import datetime, timezone, timedelta
from typing import Optional

import bcrypt as _bcrypt
from fastapi import Cookie, HTTPException, Request
from jose import JWTError, jwt
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

import config


# ── Pydantic models ───────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str

class ControllerParamsUpdate(BaseModel):
    battery_capacity: Optional[int] = None
    temperature_compensation_coefficient: Optional[float] = None
    voltage_controls: Optional[dict] = None


# ── Rate limiter ──────────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address)


# ── JWT helpers ───────────────────────────────────────────────────────────────

def create_token(username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=config.TOKEN_EXPIRE_HOURS)
    return jwt.encode({"sub": username, "exp": expire}, config.SECRET_KEY, algorithm="HS256")


def check_session(request: Request) -> bool:
    session = request.cookies.get("sb_session")
    if not session:
        return False
    try:
        payload = jwt.decode(session, config.SECRET_KEY, algorithms=["HS256"])
        return payload.get("sub") == config.ADMIN_USERNAME
    except JWTError:
        return False


# ── FastAPI dependencies ──────────────────────────────────────────────────────

def verify_session(sb_session: Optional[str] = Cookie(default=None)) -> str:
    if not sb_session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(sb_session, config.SECRET_KEY, algorithms=["HS256"])
        username: str = payload.get("sub")
        if username != config.ADMIN_USERNAME:
            raise HTTPException(status_code=401, detail="Invalid session")
        return username
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired session")


def require_controller():
    """Dependency — fails if no controller and not in sim mode."""
    if config.CONTROLLER is None:
        detail = "Simulator mode — hardware endpoint unavailable" if config.SIM_MODE \
                 else "Controller not connected"
        raise HTTPException(status_code=503, detail=detail)


def require_real_controller():
    """Dependency — always fails without physical hardware (blocks sim writes)."""
    if config.CONTROLLER is None:
        detail = "Simulator mode — write operations require physical hardware" if config.SIM_MODE \
                 else "Controller not connected"
        raise HTTPException(status_code=503, detail=detail)
