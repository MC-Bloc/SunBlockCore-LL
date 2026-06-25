"""Auth models, JWT helpers, rate limiter, and FastAPI dependency functions."""

from datetime import datetime, timezone, timedelta
from typing import Optional

import bcrypt as _bcrypt
import pyotp
from fastapi import Cookie, Header, HTTPException, Request
from jose import JWTError, jwt
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter
from slowapi.util import get_remote_address

import config
from db import verify_api_token


# ── Pydantic models ───────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str

# The deployed battery bank is 12V (confirmed by simulator.py's real-deployment-
# derived BattVoltage baseline of 11.5-14.8V). VOLTAGE_MIN/MAX below are a broad,
# conservative safety envelope for ANY 12V lead-acid/AGM/gel system — they exist
# to reject clearly unsafe or mistyped values (e.g. 99V, a negative number), not
# to replace correct battery-specific configuration. Consult your Tracer-AN's
# manual for the exact recommended thresholds for your battery chemistry before
# relying on these bounds as your only check.
VOLTAGE_MIN = 8.0
VOLTAGE_MAX = 17.0

# Must match epevermodbus.driver.EpeverChargeController.battery_voltage_control_register_names
_VOLTAGE_CONTROL_KEYS = {
    "over_voltage_disconnect_voltage", "charging_limit_voltage", "over_voltage_reconnect_voltage",
    "equalize_charging_voltage", "boost_charging_voltage", "float_charging_voltage",
    "boost_reconnect_charging_voltage", "low_voltage_reconnect_voltage", "under_voltage_recover_voltage",
    "under_voltage_warning_voltage", "low_voltage_disconnect_voltage", "discharging_limit_voltage",
}

class ControllerParamsUpdate(BaseModel):
    battery_capacity: Optional[int]   = Field(default=None, ge=1, le=10_000)
    temperature_compensation_coefficient: Optional[float] = Field(default=None, ge=0, le=9)
    voltage_controls: Optional[dict]  = None

    @field_validator("voltage_controls")
    @classmethod
    def _validate_voltage_controls(cls, v):
        if v is None:
            return v
        if not v:
            raise ValueError("voltage_controls must not be empty")
        for key, val in v.items():
            if key not in _VOLTAGE_CONTROL_KEYS:
                raise ValueError(f"unknown voltage_controls key: {key!r}")
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise ValueError(f"{key} must be a number")
            if not (VOLTAGE_MIN <= val <= VOLTAGE_MAX):
                raise ValueError(
                    f"{key}={val} is outside the safe range "
                    f"[{VOLTAGE_MIN}, {VOLTAGE_MAX}]V for a 12V system"
                )
        # Only the two orderings that are unambiguous and chemistry-independent
        # across every charge controller (disconnect must be more extreme than
        # its matching reconnect point) — checked only when both keys are present
        # in this same request, since the underlying driver merges partial updates
        # with the controller's current values for any keys not included here.
        ovd, ovr = v.get("over_voltage_disconnect_voltage"), v.get("over_voltage_reconnect_voltage")
        if ovd is not None and ovr is not None and ovd < ovr:
            raise ValueError("over_voltage_disconnect_voltage must be >= over_voltage_reconnect_voltage")
        lvd, lvr = v.get("low_voltage_disconnect_voltage"), v.get("low_voltage_reconnect_voltage")
        if lvd is not None and lvr is not None and lvr < lvd:
            raise ValueError("low_voltage_reconnect_voltage must be >= low_voltage_disconnect_voltage")
        return v

class SettingsUpdate(BaseModel):
    data_directory:     Optional[str]   = None  # absolute path; required on first run
    read_interval:      Optional[int]   = None  # seconds, 1–3600
    data_man:           Optional[bool]  = None
    sim_mode:           Optional[bool]  = None
    token_expire_hours: Optional[int]   = None  # 1–720

class PasswordChange(BaseModel):
    current_password: str
    new_password: str

class TokenCreateRequest(BaseModel):
    name: str
    expires_in_hours: Optional[int] = None  # None = never expires

class TwoFACodeRequest(BaseModel):
    code: str

class TwoFADisableRequest(BaseModel):
    password: str
    code: str


# ── Rate limiter ──────────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address)


# ── JWT helpers ───────────────────────────────────────────────────────────────

def create_token(username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=config.TOKEN_EXPIRE_HOURS)
    return jwt.encode({"sub": username, "exp": expire}, config.SECRET_KEY, algorithm="HS256")


# ── 2FA helpers ───────────────────────────────────────────────────────────────
#
# Login is a two-step process when 2FA is enabled: a correct password alone
# does not issue a full `sb_session`. Instead it issues a short-lived "pending"
# token carrying `purpose: "2fa_pending"` — distinct from real sessions so it
# can never be mistaken for one by `verify_session` (which only accepts tokens
# without a `purpose` claim matching this marker, see below). The pending token
# must be exchanged for a real session within PENDING_2FA_MINUTES by presenting
# a valid TOTP code or backup code via POST /api/login/verify-2fa.

PENDING_2FA_MINUTES = 5


def create_pending_2fa_token(username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=PENDING_2FA_MINUTES)
    return jwt.encode(
        {"sub": username, "purpose": "2fa_pending", "exp": expire},
        config.SECRET_KEY,
        algorithm="HS256",
    )


def verify_pending_2fa_token(token: Optional[str]) -> Optional[str]:
    """Return the username if `token` is a valid, unexpired pending-2FA token."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, config.SECRET_KEY, algorithms=["HS256"])
        if payload.get("purpose") == "2fa_pending" and payload.get("sub") == config.ADMIN_USERNAME:
            return payload["sub"]
    except JWTError:
        pass
    return None


def verify_totp_code(code: str) -> bool:
    """Validate a 6-digit TOTP code against the active secret (±1 time-step skew)."""
    if not config.TOTP_ENABLED or not config.TOTP_SECRET:
        return False
    code = code.strip().replace(" ", "")
    if not code:
        return False
    try:
        return pyotp.TOTP(config.TOTP_SECRET).verify(code, valid_window=1)
    except Exception:
        return False


def check_session(request: Request) -> bool:
    session = request.cookies.get("sb_session")
    if not session:
        return False
    try:
        payload = jwt.decode(session, config.SECRET_KEY, algorithms=["HS256"])
        return payload.get("sub") == config.ADMIN_USERNAME and "purpose" not in payload
    except JWTError:
        return False


# ── FastAPI dependencies ──────────────────────────────────────────────────────

def verify_session(sb_session: Optional[str] = Cookie(default=None)) -> str:
    if not sb_session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(sb_session, config.SECRET_KEY, algorithms=["HS256"])
        username: str = payload.get("sub")
        # Reject "purpose"-tagged tokens (e.g. 2fa_pending) here — only a fully
        # authenticated session (password + 2FA, when enabled) may pass this
        # dependency. Without this check, a leaked pending-2FA token could be
        # replayed as `sb_session` and skip the second factor entirely.
        if username != config.ADMIN_USERNAME or "purpose" in payload:
            raise HTTPException(status_code=401, detail="Invalid session")
        return username
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired session")


def verify_session_or_token(
    sb_session:    Optional[str] = Cookie(default=None),
    authorization: Optional[str] = Header(default=None),
) -> str:
    """
    Accepts either a browser session cookie OR an `Authorization: Bearer <token>`
    header — the latter is how external scripts/services authenticate to the API
    without ever holding a session cookie (and thus without CSRF exposure).
    """
    if sb_session:
        try:
            payload = jwt.decode(sb_session, config.SECRET_KEY, algorithms=["HS256"])
            if payload.get("sub") == config.ADMIN_USERNAME and "purpose" not in payload:
                return payload["sub"]
        except JWTError:
            pass

    if authorization and authorization.lower().startswith("bearer "):
        raw_token = authorization[7:].strip()
        username = verify_api_token(raw_token)
        if username:
            return username

    raise HTTPException(status_code=401, detail="Not authenticated")


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
