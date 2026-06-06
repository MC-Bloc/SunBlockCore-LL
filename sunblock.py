'''
SunBlock — unified solar monitoring + API server

Written by M. Shahrom Ali (github.com/estineali)
for The SunBlock Project
under the TAG MC-Bloc, Milieux Institute, Concordia University, Montreal, Canada.

https://github.com/MC-Bloc/SunBlock

Setup:
  1. Copy sample.env → .env and fill in values.
  2. Generate a password hash:  python3 scripts/gen_password_hash.py
  3. Vendor frontend assets:    bash scripts/vendor.sh
  4. Run:  uvicorn sunblock:socket_app --host 0.0.0.0 --port ${PORT:-3000}

Simulator mode (no hardware):
  Set SIM_MODE=true in .env — live data is generated from real deployment baselines.

Source layout:
  config.py    — env vars + shared mutable state
  auth.py      — JWT, bcrypt, FastAPI deps, Pydantic models
  db.py        — SQLite helpers + application logging
  hardware.py  — controller polling, power profiles, parameter r/w
  simulator.py — hardware-free data generator
  sunblock.py  — app, lifespan, routes, Socket.IO   ← you are here

Dependencies:
  pip install fastapi uvicorn python-socketio epevermodbus python-dotenv \
              python-jose[cryptography] bcrypt slowapi jinja2
'''

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import bcrypt as _bcrypt
import socketio
from epevermodbus.driver import EpeverChargeController
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import config
from auth import (
    ControllerParamsUpdate, LoginRequest, SettingsUpdate, PasswordChange, limiter,
    check_session, create_token, verify_session,
    require_controller, require_real_controller,
)
from db import apply_data_directory, check_db, delete_setting, load_settings, query_history, save_setting, sunblock_log, write_db
from hardware import (
    apply_controller_params, check_power_profile,
    parse_data, read_controller_params,
    read_controller_stats, read_controller_status,
    set_power_profile,
)
from simulator import simulate_data


# ── Polling loop ──────────────────────────────────────────────────────────────

async def polling_loop():
    await sunblock_log("Waking Up...")
    if config.DATA_MAN:
        await sunblock_log("Data Management is " + str(config.DATA_MAN))
        await asyncio.to_thread(check_db)

    loop = asyncio.get_running_loop()

    while config.POLLING_ACTIVE:
        poll_fn = simulate_data if config.SIM_MODE else parse_data
        try:
            new_data = await loop.run_in_executor(None, poll_fn)
            config.SOLAR_DATA = new_data  # atomic reference swap
        except Exception as e:
            await sunblock_log("Hardware error, stopping poll: " + str(e))
            break

        if config.DATA_MAN and not config.SIM_MODE:
            try:
                await loop.run_in_executor(None, write_db)
            except Exception as e:
                await sunblock_log("DB write error (continuing): " + str(e))

        try:
            await sio.emit("solar_data", {**config.SOLAR_DATA, "ConnectedUsers": config.ACTIVE_USERS})
        except Exception as e:
            await sunblock_log("Socket emit error (continuing): " + str(e))

        await asyncio.sleep(config.READ_INTERVAL)

    await sunblock_log("Exiting polling loop.")


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(load_settings)
    await sunblock_log("Settings loaded from persistent store.")

    if config.DATA_DIRECTORY:
        os.makedirs(config.DATA_DIRECTORY, exist_ok=True)
    else:
        await sunblock_log(
            "WARNING: DATA_DIRECTORY is not set. "
            "Open the admin panel → Settings to configure it."
        )

    if not config.ADMIN_PASSWORD_HASH:
        await sunblock_log(
            "WARNING: ADMIN_PASSWORD_HASH not set. "
            "Run scripts/gen_password_hash.py and set it in .env. Login is disabled."
        )

    if not config.ADMIN_PATH:
        await sunblock_log(
            "WARNING: ADMIN_PATH is not set. "
            "The admin login page is unreachable. Set ADMIN_PATH to a secret slug in .env "
            "(e.g. ADMIN_PATH=xK9mP3qR7) then restart."
        )
    else:
        await sunblock_log(f"Admin login available at /{config.ADMIN_PATH.lstrip('/')}")

    if config.SIM_MODE:
        await sunblock_log("SIM_MODE=true — skipping hardware init, using simulator.")
    else:
        try:
            config.CONTROLLER = EpeverChargeController(config.CONTROLLER_PORT, config.CONTROLLER_SLAVE)
        except Exception as e:
            await sunblock_log("Failed to connect to controller: " + str(e))

    config.ACTIVE_USERS_LOCK = asyncio.Lock()

    if config.CONTROLLER is not None or config.SIM_MODE:
        if config.DATA_DIRECTORY:
            config.POLLING_ACTIVE = True
            config.POLLING_TASK = asyncio.create_task(polling_loop())
        else:
            await sunblock_log(
                "Polling deferred — DATA_DIRECTORY not set. "
                "Configure it in the admin panel to begin collecting data."
            )
    else:
        await sunblock_log("Controller unavailable — polling disabled.")

    yield

    if config.POLLING_TASK is not None:
        config.POLLING_ACTIVE = False
        config.POLLING_TASK.cancel()
        try:
            await config.POLLING_TASK
        except asyncio.CancelledError:
            pass
    if config.DB_CONNECTION:
        config.DB_CONNECTION.close()
    await sunblock_log("Server shutting down.")


# ── App setup ─────────────────────────────────────────────────────────────────

sio       = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
app       = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.mount("/static", StaticFiles(directory="public"), name="static")

socket_app = socketio.ASGIApp(sio, app)


# ── Error handlers ────────────────────────────────────────────────────────────

@app.exception_handler(404)
async def not_found(request: Request, exc: HTTPException):
    # Return JSON for API paths, HTML for everything else
    if request.url.path.startswith("/api/") or request.url.path.startswith("/socket.io/"):
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    return templates.TemplateResponse("404.html", {"request": request}, status_code=404)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request":          request,
        "is_authenticated": check_session(request),
        "sim_mode":         config.SIM_MODE,
        "env_defaults":     config.ENV_DEFAULTS,
        "data_directory":   config.DATA_DIRECTORY or "",
        "admin_mode":       False,
    })

async def _admin_page(request: Request):
    """Dedicated admin entry-point — auto-opens login modal (or settings if already authed).
    Registered at the path set by ADMIN_PATH in .env; not exposed at any predictable URL."""
    return templates.TemplateResponse("index.html", {
        "request":          request,
        "is_authenticated": check_session(request),
        "sim_mode":         config.SIM_MODE,
        "env_defaults":     config.ENV_DEFAULTS,
        "data_directory":   config.DATA_DIRECTORY or "",
        "admin_mode":       True,
    })

# Register the admin route only if ADMIN_PATH is configured.
if config.ADMIN_PATH:
    _path = "/" + config.ADMIN_PATH.lstrip("/")
    app.add_api_route(_path, _admin_page, methods=["GET"], include_in_schema=False)

@app.get("/api/mode")
async def get_mode():
    return {"mode": "simulator" if config.SIM_MODE else "live"}


# Auth

@app.post("/api/login")
@limiter.limit("5/minute")
async def login(request: Request, body: LoginRequest, response: Response):
    if not config.ADMIN_PASSWORD_HASH:
        raise HTTPException(status_code=503, detail="Admin password not configured on server.")
    if body.username != config.ADMIN_USERNAME or \
       not _bcrypt.checkpw(body.password.encode(), config.ADMIN_PASSWORD_HASH.encode()):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    response.set_cookie(
        key="sb_session",
        value=create_token(body.username),
        httponly=True,
        secure=config.SECURE_COOKIES,
        samesite="strict",
        max_age=config.TOKEN_EXPIRE_HOURS * 3600,
    )
    return {"message": "Logged in"}

@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie("sb_session", samesite="strict")
    return {"message": "Logged out"}

@app.get("/api/auth/status")
async def auth_status(request: Request):
    return {"authenticated": check_session(request)}


# Live data

@app.get("/api/data")
async def get_data():
    return JSONResponse(content=config.SOLAR_DATA, status_code=200)


@app.get("/api/data/history")
async def get_data_history(
    limit:   int                = 100,
    offset:  int                = 0,
    from_ts: Optional[str]      = Query(default=None, alias="from"),
    to_ts:   Optional[str]      = Query(default=None, alias="to"),
    order:   str                = "desc",
):
    """
    Paginated history of solar readings from the SQLite database.

    Query params:
      limit   — rows per page (1–1000, default 100)
      offset  — row index to start from (default 0)
      from    — optional ISO date/datetime lower bound (e.g. 2025-05-11)
      to      — optional ISO date/datetime upper bound (e.g. 2025-05-15)
      order   — "desc" (newest first, default) or "asc" (oldest first)

    Returns {"rows": [...], "total": N, "limit": N, "offset": N}.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, query_history, limit, offset, from_ts, to_ts, order
    )


@app.get("/api/data/download")
async def download_db(user: str = Depends(verify_session)):
    """Download the raw SQLite telemetry database file (auth required)."""
    if not config.DB_NAME or not os.path.isfile(config.DB_NAME):
        raise HTTPException(
            status_code=404,
            detail="Database file not found. DATA_DIRECTORY may not be configured or data collection may be disabled.",
        )
    return FileResponse(
        path=config.DB_NAME,
        media_type="application/octet-stream",
        filename="SunBlockCore-LL.db",
    )


# Settings

def _settings_snapshot() -> dict:
    return {
        "data_directory":     config.DATA_DIRECTORY or "",
        "read_interval":      config.READ_INTERVAL,
        "data_man":           config.DATA_MAN,
        "sim_mode":           config.SIM_MODE,
        "token_expire_hours": config.TOKEN_EXPIRE_HOURS,
    }

@app.get("/api/settings")
async def get_settings(user: str = Depends(verify_session)):
    return _settings_snapshot()

@app.patch("/api/settings")
async def update_settings(body: SettingsUpdate, user: str = Depends(verify_session)):
    if body.data_directory is not None:
        d = body.data_directory.strip()
        if not d:
            raise HTTPException(status_code=400, detail="data_directory cannot be empty")
        try:
            await asyncio.to_thread(apply_data_directory, d)
        except OSError as e:
            raise HTTPException(status_code=400, detail=f"Cannot create directory: {e}")
        await asyncio.to_thread(save_setting, "data_directory", config.DATA_DIRECTORY)
        # Initialise telemetry DB now that the directory exists.
        if config.DATA_MAN:
            await asyncio.to_thread(check_db)
        # Start polling if it was held back only because DATA_DIRECTORY was missing.
        if not config.POLLING_ACTIVE and (config.CONTROLLER is not None or config.SIM_MODE):
            config.POLLING_ACTIVE = True
            config.POLLING_TASK = asyncio.create_task(polling_loop())

    if body.read_interval is not None:
        if not 1 <= body.read_interval <= 3600:
            raise HTTPException(status_code=400, detail="read_interval must be 1–3600")
        config.READ_INTERVAL = body.read_interval
        await asyncio.to_thread(save_setting, "read_interval", body.read_interval)

    if body.data_man is not None:
        config.DATA_MAN = body.data_man
        await asyncio.to_thread(save_setting, "data_man", body.data_man)

    if body.sim_mode is not None:
        if not body.sim_mode and config.CONTROLLER is None:
            raise HTTPException(status_code=400, detail="Cannot disable simulator — no hardware controller is connected")
        config.SIM_MODE = body.sim_mode
        await asyncio.to_thread(save_setting, "sim_mode", body.sim_mode)

    if body.token_expire_hours is not None:
        if not 1 <= body.token_expire_hours <= 720:
            raise HTTPException(status_code=400, detail="token_expire_hours must be 1–720")
        config.TOKEN_EXPIRE_HOURS = body.token_expire_hours
        await asyncio.to_thread(save_setting, "token_expire_hours", body.token_expire_hours)

    return _settings_snapshot()

@app.delete("/api/settings/{key}")
async def reset_setting(key: str, user: str = Depends(verify_session)):
    _restorable = {"read_interval", "data_man", "sim_mode", "token_expire_hours"}
    if key not in _restorable:
        raise HTTPException(status_code=404, detail=f"Unknown or non-resettable setting: {key}")

    env_val = config.ENV_DEFAULTS[key]

    if key == "sim_mode" and not env_val and config.CONTROLLER is None:
        raise HTTPException(status_code=400, detail="Cannot disable simulator — no hardware controller is connected")

    await asyncio.to_thread(delete_setting, key)

    # Restore live config to the original env value
    _restore = {
        "read_interval":      lambda v: setattr(config, "READ_INTERVAL",      v),
        "data_man":           lambda v: setattr(config, "DATA_MAN",           v),
        "sim_mode":           lambda v: setattr(config, "SIM_MODE",           v),
        "token_expire_hours": lambda v: setattr(config, "TOKEN_EXPIRE_HOURS", v),
    }
    _restore[key](env_val)

    return _settings_snapshot()


@app.post("/api/settings/password")
async def change_password(body: PasswordChange, user: str = Depends(verify_session)):
    if not config.ADMIN_PASSWORD_HASH:
        raise HTTPException(status_code=503, detail="No password configured on server")
    if not _bcrypt.checkpw(body.current_password.encode(), config.ADMIN_PASSWORD_HASH.encode()):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    new_hash = _bcrypt.hashpw(body.new_password.encode(), _bcrypt.gensalt()).decode()
    config.ADMIN_PASSWORD_HASH = new_hash
    await asyncio.to_thread(save_setting, "admin_password_hash", new_hash)
    return {"message": "Password updated"}


# Power profile

@app.get("/api/power-profile")
async def get_power_profile(_=Depends(require_controller)):
    loop = asyncio.get_running_loop()
    return {"profile": await loop.run_in_executor(None, check_power_profile)}

@app.post("/api/performance-mode")
async def set_performance(user: str = Depends(verify_session), _=Depends(require_real_controller)):
    loop = asyncio.get_running_loop()
    return {"profile": await loop.run_in_executor(None, set_power_profile, "performance")}

@app.post("/api/power-saver-mode")
async def set_power_saver(user: str = Depends(verify_session), _=Depends(require_real_controller)):
    loop = asyncio.get_running_loop()
    return {"profile": await loop.run_in_executor(None, set_power_profile, "power-saver")}

@app.post("/api/balanced")
async def set_balanced(user: str = Depends(verify_session), _=Depends(require_real_controller)):
    loop = asyncio.get_running_loop()
    return {"profile": await loop.run_in_executor(None, set_power_profile, "balanced")}


# Controller parameters

@app.get("/api/controller/parameters")
async def get_controller_params(_=Depends(require_controller)):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, read_controller_params)

@app.put("/api/controller/parameters")
async def update_controller_params(
    body: ControllerParamsUpdate,
    user: str = Depends(verify_session),
    _=Depends(require_real_controller),
):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, apply_controller_params, body.model_dump())
    return await loop.run_in_executor(None, read_controller_params)

@app.get("/api/controller/stats")
async def get_controller_stats(_=Depends(require_controller)):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, read_controller_stats)

@app.get("/api/controller/status")
async def get_controller_status(_=Depends(require_controller)):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, read_controller_status)

@app.post("/api/controller/rtc/sync")
async def sync_rtc(user: str = Depends(verify_session), _=Depends(require_real_controller)):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: config.CONTROLLER.set_rtc(datetime.now()))
    return {"message": "RTC synced to server time"}


# ── Socket.IO events ──────────────────────────────────────────────────────────

@sio.event
async def connect(sid, environ):
    async with config.ACTIVE_USERS_LOCK:
        config.ACTIVE_USERS += 1
    print("Client connected:", sid)

@sio.event
async def disconnect(sid):
    async with config.ACTIVE_USERS_LOCK:
        config.ACTIVE_USERS -= 1
    print("Client disconnected:", sid)
