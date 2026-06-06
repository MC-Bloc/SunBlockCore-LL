'''
SunBlock — unified data collection + API server

Written by M. Shahrom Ali (github.com/estineali)
for The SunBlock Project
under the TAG MC-Bloc, Milieux Institute, Concordia University, Montreal, Canada.

Check it out at https://github.com/MC-Bloc/SunBlock

Setup notes:
1. Update path constants in .env to match your server.
2. Controller address is /dev/ttyACM0 — find yours with `sudo dmesg` after plugging in RS485.
3. All data is stored in DATA_DIRECTORY (default ~/SunblockData).
4. Server user needs passwordless sudo — see `sudo visudo`.
5. Generate a password hash before first run:
       python3 scripts/gen_password_hash.py
   Then set ADMIN_PASSWORD_HASH in .env.
6. Vendor frontend dependencies before first run:
       bash scripts/vendor.sh

Run with:
    uvicorn sunblock:socket_app --host 0.0.0.0 --port ${PORT:-3000}

Install dependencies:
    pip install fastapi uvicorn python-socketio epevermodbus python-dotenv \
                python-jose[cryptography] bcrypt slowapi jinja2

Requires Python 3.9+ (uses asyncio.to_thread).
'''

import asyncio
import os
import random
import sqlite3
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional

from dotenv import load_dotenv
from jose import JWTError, jwt
import bcrypt as _bcrypt
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
import socketio
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from epevermodbus.driver import EpeverChargeController

load_dotenv()

# --- Config ---

CONTROLLER_PORT   = os.getenv("CONTROLLER_PORT",   "/dev/ttyACM0")
CONTROLLER_SLAVE  = int(os.getenv("CONTROLLER_SLAVE", 1))
DATA_DIRECTORY    = os.getenv("DATA_DIRECTORY",    "/home/pc/SunblockData/")
POWER_DRAW_SCRIPT = os.getenv("POWER_DRAW_SCRIPT_ADDR", "/home/pc/power_scripts/powerdraw.sh")
POWER_LOGS_FILE   = os.path.join(DATA_DIRECTORY, "SunBlockCoreLogs.txt")

DATA_MAN          = os.getenv("DATA_MAN",    "true").lower() == "true"
DB_NAME           = os.path.join(DATA_DIRECTORY, "SunBlockCore-LL.db")
DB_TABLE_NAME     = "solardata"
READ_INTERVAL     = int(os.getenv("READ_INTERVAL", 1))   # seconds

ADMIN_USERNAME    = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
SECRET_KEY        = os.getenv("SECRET_KEY",  "changeme-secret-key")
TOKEN_EXPIRE_HOURS = int(os.getenv("TOKEN_EXPIRE_HOURS", 24))
SECURE_COOKIES    = os.getenv("SECURE_COOKIES", "false").lower() == "true"

SIM_MODE          = os.getenv("SIM_MODE", "false").lower() == "true"


# --- State ---
# CONTROLLER, ACTIVE_USERS_LOCK, POLLING_TASK initialised in lifespan

CONTROLLER    = None
ACTIVE_USERS  = 0
ACTIVE_USERS_LOCK = None
POLLING_ACTIVE = False
POLLING_TASK  = None
DB_CONNECTION = None
DB_CURSOR     = None

SOLAR_DATA = {
    "Timestamp":        "",
    "PVVoltage":        0,
    "PVCurrent":        0,
    "PVPower":          0,
    "BattVoltage":      0,
    "BattTemperature":  0,
    "BattChargePower":  0,
    "LoadPower":        0,
    "BattPercentage":   0,
    "BattOverallCurrent": 0,
    "CPUPowerDraw":     0,
    "PowerProfile":     "",
}


# --- Auth ---

class LoginRequest(BaseModel):
    username: str
    password: str

class ControllerParamsUpdate(BaseModel):
    battery_capacity: Optional[int] = None
    temperature_compensation_coefficient: Optional[float] = None
    voltage_controls: Optional[dict] = None

limiter = Limiter(key_func=get_remote_address)

def _create_token(username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS)
    return jwt.encode({"sub": username, "exp": expire}, SECRET_KEY, algorithm="HS256")

def _check_session(request: Request) -> bool:
    session = request.cookies.get("sb_session")
    if not session:
        return False
    try:
        payload = jwt.decode(session, SECRET_KEY, algorithms=["HS256"])
        return payload.get("sub") == ADMIN_USERNAME
    except JWTError:
        return False

def verify_session(sb_session: Optional[str] = Cookie(default=None)) -> str:
    if not sb_session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(sb_session, SECRET_KEY, algorithms=["HS256"])
        username: str = payload.get("sub")
        if username != ADMIN_USERNAME:
            raise HTTPException(status_code=401, detail="Invalid session")
        return username
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

def require_controller():
    if CONTROLLER is None:
        detail = "Simulator mode — hardware endpoint unavailable" if SIM_MODE else "Controller not connected"
        raise HTTPException(status_code=503, detail=detail)

def require_real_controller():
    """Use for write endpoints that must not run against simulated state."""
    if CONTROLLER is None:
        detail = "Simulator mode — write operations require physical hardware" if SIM_MODE else "Controller not connected"
        raise HTTPException(status_code=503, detail=detail)


# --- Logging ---

async def sunblock_log(message: str):
    line = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + ": " + message + "\n"
    await asyncio.to_thread(_write_log, line)

def _write_log(line: str):
    with open(POWER_LOGS_FILE, 'a') as f:
        f.write(line)


# --- Database ---

def check_db():
    global DB_CONNECTION, DB_CURSOR
    if DB_CONNECTION is not None and DB_CURSOR is not None:
        return
    create_table = not os.path.isfile(DB_NAME)
    DB_CONNECTION = sqlite3.connect(DB_NAME, check_same_thread=False)
    DB_CURSOR = DB_CONNECTION.cursor()
    if create_table:
        DB_CURSOR.execute(
            "CREATE TABLE solardata("
            "Timestamp text, PVVoltage real, PVCurrent real, PVPower real, "
            "BattVoltage real, BattTemperature real, BattChargePower real, "
            "LoadPower real, BattPercentage int, BattOverallCurrent real, "
            "CPUPowerDraw real, PowerProfile text)"
        )

def write_db():
    placeholders = ", ".join(["?"] * len(SOLAR_DATA))
    DB_CURSOR.execute(
        f"INSERT INTO {DB_TABLE_NAME} VALUES ({placeholders})",
        list(SOLAR_DATA.values())
    )
    DB_CONNECTION.commit()


# --- Hardware polling ---

def check_power_profile() -> str:
    result = subprocess.run(["sudo", "powerprofilesctl", "get"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"powerprofilesctl get failed with code {result.returncode}")
    return result.stdout.strip()

def set_power_profile(profile: str) -> str:
    result = subprocess.run(["sudo", "powerprofilesctl", "set", profile])
    if result.returncode != 0:
        raise RuntimeError(f"powerprofilesctl set {profile} failed with code {result.returncode}")
    return check_power_profile()

def parse_data() -> dict:
    data = {}
    data["Timestamp"]          = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["PVVoltage"]          = CONTROLLER.get_solar_voltage()
    data["PVCurrent"]          = CONTROLLER.get_solar_current()
    data["PVPower"]            = CONTROLLER.get_solar_power()
    data["BattVoltage"]        = CONTROLLER.get_battery_voltage()
    data["BattTemperature"]    = CONTROLLER.get_battery_temperature()
    data["BattChargePower"]    = CONTROLLER.get_battery_power()
    data["BattOverallCurrent"] = CONTROLLER.get_battery_current()
    data["BattPercentage"]     = CONTROLLER.get_battery_state_of_charge()
    data["LoadPower"]          = CONTROLLER.get_load_power()
    result = subprocess.run([POWER_DRAW_SCRIPT], capture_output=True)
    data["CPUPowerDraw"]  = result.stdout.decode().replace("W", "").strip()
    data["PowerProfile"]  = check_power_profile()
    return data


# --- Controller parameter read/write ---

def read_controller_params() -> dict:
    return {
        "battery_type":                         str(CONTROLLER.get_battery_type()),
        "battery_capacity":                     CONTROLLER.get_battery_capacity(),
        "battery_rated_voltage":                str(CONTROLLER.get_battery_rated_voltage()),
        "charging_mode":                        str(CONTROLLER.get_charging_mode()),
        "temperature_compensation_coefficient": CONTROLLER.get_temperature_compensation_coefficient(),
        "default_load_on_off":                  str(CONTROLLER.get_default_load_on_off_in_manual_mode()),
        "equalize_duration":                    CONTROLLER.get_equalize_duration(),
        "boost_duration":                       CONTROLLER.get_boost_duration(),
        "voltage_controls":                     CONTROLLER.get_battery_voltage_control_registers(),
    }

def read_controller_stats() -> dict:
    return {
        "pv_voltage_max_today":   CONTROLLER.get_maximum_pv_voltage_today(),
        "pv_voltage_min_today":   CONTROLLER.get_minimum_pv_voltage_today(),
        "batt_voltage_max_today": CONTROLLER.get_maximum_battery_voltage_today(),
        "batt_voltage_min_today": CONTROLLER.get_minimum_battery_voltage_today(),
        "generated_today":        CONTROLLER.get_generated_energy_today(),
        "generated_this_month":   CONTROLLER.get_generated_energy_this_month(),
        "generated_this_year":    CONTROLLER.get_generated_energy_this_year(),
        "total_generated":        CONTROLLER.get_total_generated_energy(),
        "consumed_today":         CONTROLLER.get_consumed_energy_today(),
        "consumed_this_month":    CONTROLLER.get_consumed_energy_this_month(),
        "consumed_this_year":     CONTROLLER.get_consumed_energy_this_year(),
        "total_consumed":         CONTROLLER.get_total_consumed_energy(),
    }

def read_controller_status() -> dict:
    return {
        "battery_status":             CONTROLLER.get_battery_status(),
        "charging_status":            CONTROLLER.get_charging_equipment_status(),
        "discharging_status":         CONTROLLER.get_discharging_equipment_status(),
        "is_day":                     CONTROLLER.is_day(),
        "controller_temperature":     CONTROLLER.get_controller_temperature(),
        "remote_battery_temperature": CONTROLLER.get_remote_battery_temperature(),
        "load_voltage":               CONTROLLER.get_load_voltage(),
        "load_current":               CONTROLLER.get_load_current(),
        "rtc":                        str(CONTROLLER.get_rtc()),
        "rated_charging_current":     CONTROLLER.get_rated_charging_current(),
        "rated_load_current":         CONTROLLER.get_rated_load_current(),
    }

def apply_controller_params(update: dict):
    if update.get("battery_capacity") is not None:
        CONTROLLER.set_battery_capacity(update["battery_capacity"])
    if update.get("temperature_compensation_coefficient") is not None:
        CONTROLLER.set_temperature_compensation_coefficient(
            update["temperature_compensation_coefficient"]
        )
    if update.get("voltage_controls") is not None:
        CONTROLLER.set_battery_voltage_control_registers_dict(update["voltage_controls"])


# --- Simulator ---

class _SimState:
    """
    Produces realistic solar readings without hardware.
    Hourly baselines are derived from 1.28 M rows of real deployment data
    (2025-05-11 → 2025-05-29, Milieux Institute roof array).
    Each tick interpolates linearly between adjacent hour anchors, adds
    Gaussian noise, then exponentially smooths to avoid step jumps.

    Tuple layout per hour:
        (pv_v, pv_a, pv_w, batt_v, batt_soc, batt_charge_w, batt_overall_a, load_w, cpu_w)
    """

    HOURLY = {
        0:  ( 1.77, 0.110,  1.59, 12.958, 68.3,  1.59, -0.719, 10.88, 3.92),
        1:  ( 1.78, 0.128,  1.83, 12.936, 67.5,  1.83, -0.590,  9.45, 2.86),
        2:  ( 1.87, 0.096,  1.41, 13.005, 70.0,  1.41, -0.638,  9.71, 2.96),
        3:  ( 1.87, 0.095,  1.39, 13.014, 70.4,  1.39, -0.542,  8.39, 2.17),
        4:  ( 2.83, 0.094,  1.38, 13.006, 70.1,  1.38, -0.532,  8.35, 2.07),
        5:  (12.30, 0.101,  1.48, 13.000, 69.9,  1.48, -0.526,  8.21, 2.00),
        6:  (14.08, 0.230,  3.37, 13.001, 69.9,  3.37, -0.388,  8.39, 2.13),
        7:  (14.91, 0.829, 12.95, 13.048, 71.7, 12.95,  0.330,  8.63, 2.37),
        8:  (15.21, 1.240, 19.80, 13.100, 73.6, 19.80,  0.837,  8.62, 2.52),
        9:  (14.91, 1.537, 23.65, 13.127, 74.6, 23.65,  1.105,  8.93, 2.78),
        10: (14.79, 1.844, 27.88, 13.155, 75.7, 27.88,  1.408,  9.04, 2.93),
        11: (14.67, 1.889, 28.30, 13.169, 76.2, 28.30,  1.425,  9.19, 2.96),
        12: (14.72, 2.002, 29.78, 13.186, 76.8, 29.77,  1.568,  8.79, 2.60),
        13: (14.75, 1.976, 29.37, 13.223, 78.2, 29.36,  1.540,  8.78, 2.54),
        14: (14.68, 0.796, 11.87, 13.177, 76.4, 11.86,  0.219,  8.98, 2.76),  # real afternoon dip
        15: (14.42, 0.542,  7.79, 13.155, 75.6,  7.78, -0.099,  9.15, 2.91),
        16: (14.39, 0.422,  6.09, 13.134, 74.9,  6.09, -0.268,  9.64, 3.23),
        17: (14.40, 0.380,  5.50, 13.116, 74.2,  5.50, -0.347, 10.09, 3.50),
        18: (14.28, 0.250,  3.61, 13.084, 73.0,  3.60, -0.482,  9.96, 3.38),
        19: (13.80, 0.173,  2.49, 13.066, 72.3,  2.49, -0.495,  8.99, 2.68),
        20: ( 9.42, 0.149,  2.10, 13.053, 71.8,  2.10, -0.571,  9.55, 3.03),
        21: ( 1.88, 0.157,  2.22, 13.034, 71.1,  2.22, -0.633, 10.47, 3.61),
        22: ( 1.73, 0.146,  2.06, 13.016, 70.4,  2.06, -0.645, 10.44, 3.62),
        23: ( 1.73, 0.127,  1.82, 12.987, 69.4,  1.82, -0.781, 11.93, 4.45),
    }

    # Gaussian noise sigma per channel
    # (pv_v, pv_a, pv_w, batt_v, batt_soc, batt_charge_w, batt_overall_a, load_w, cpu_w)
    SIGMA = (0.20, 0.06, 0.9, 0.015, 0.4, 0.8, 0.12, 1.3, 0.8)

    # Exponential smoothing: new = prev + ALPHA*(target - prev)
    ALPHA = 0.35

    def __init__(self):
        self._prev: list | None = None

    @staticmethod
    def _interp(hour: int, frac: float) -> tuple:
        h0 = hour
        h1 = (hour + 1) % 24
        r0 = _SimState.HOURLY[h0]
        r1 = _SimState.HOURLY[h1]
        return tuple(a + (b - a) * frac for a, b in zip(r0, r1))

    def step(self) -> dict:
        now = datetime.now()
        frac = (now.minute * 60 + now.second) / 3600.0
        base = list(self._interp(now.hour, frac))

        # Add per-channel Gaussian noise
        noisy = [b + random.gauss(0, s) for b, s in zip(base, self.SIGMA)]

        # Smooth toward noisy target from previous reading
        if self._prev is not None:
            noisy = [p + self.ALPHA * (n - p) for p, n in zip(self._prev, noisy)]

        self._prev = noisy

        pv_v, pv_a, pv_w, bv, soc, bcp, boc, load, cpu = noisy

        return {
            "Timestamp":          now.strftime("%Y-%m-%d %H:%M:%S"),
            "PVVoltage":          round(max(0.0, pv_v), 2),
            "PVCurrent":          round(max(0.0, pv_a), 3),
            "PVPower":            round(max(0.0, pv_w), 2),
            "BattVoltage":        round(max(11.5, min(14.8, bv)), 3),
            "BattTemperature":    round(25.0 + random.gauss(0, 0.8), 1),
            "BattChargePower":    round(max(0.0, bcp), 2),
            "BattOverallCurrent": round(boc, 3),
            "BattPercentage":     int(round(max(0, min(100, soc)))),
            "LoadPower":          round(max(0.0, load), 2),
            "CPUPowerDraw":       round(max(0.3, cpu), 3),
            "PowerProfile":       "power-saver",
        }


_SIM = _SimState()


def simulate_data() -> dict:
    """Drop-in replacement for parse_data() when SIM_MODE=true."""
    return _SIM.step()


# --- Polling loop ---

async def polling_loop():
    global SOLAR_DATA, POLLING_ACTIVE
    await sunblock_log("Waking Up...")
    if DATA_MAN:
        await sunblock_log("Data Management is " + str(DATA_MAN))
        await asyncio.to_thread(check_db)

    loop = asyncio.get_running_loop()
    _poll_fn = simulate_data if SIM_MODE else parse_data
    while POLLING_ACTIVE:
        try:
            new_data = await loop.run_in_executor(None, _poll_fn)
            SOLAR_DATA = new_data  # atomic reference swap
        except Exception as e:
            await sunblock_log("Hardware error, stopping poll: " + str(e))
            break

        if DATA_MAN:
            try:
                await loop.run_in_executor(None, write_db)
            except Exception as e:
                await sunblock_log("DB write error (continuing): " + str(e))

        try:
            await sio.emit("solar_data", {**SOLAR_DATA, "ConnectedUsers": ACTIVE_USERS})
        except Exception as e:
            await sunblock_log("Socket emit error (continuing): " + str(e))

        await asyncio.sleep(READ_INTERVAL)

    await sunblock_log("Exiting polling loop.")


# --- App setup ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    global CONTROLLER, ACTIVE_USERS_LOCK, POLLING_ACTIVE, POLLING_TASK

    os.makedirs(DATA_DIRECTORY, exist_ok=True)

    if not ADMIN_PASSWORD_HASH:
        await sunblock_log(
            "WARNING: ADMIN_PASSWORD_HASH not set. "
            "Run scripts/gen_password_hash.py and set it in .env. Login is disabled."
        )

    if SIM_MODE:
        await sunblock_log("SIM_MODE=true — skipping hardware init, using simulator.")
    else:
        try:
            CONTROLLER = EpeverChargeController(CONTROLLER_PORT, CONTROLLER_SLAVE)
        except Exception as e:
            await sunblock_log("Failed to connect to controller: " + str(e))

    ACTIVE_USERS_LOCK = asyncio.Lock()

    if CONTROLLER is not None or SIM_MODE:
        POLLING_ACTIVE = True
        POLLING_TASK = asyncio.create_task(polling_loop())
    else:
        await sunblock_log("Controller unavailable — polling disabled.")

    yield

    if POLLING_TASK is not None:
        POLLING_ACTIVE = False
        POLLING_TASK.cancel()
        try:
            await POLLING_TASK
        except asyncio.CancelledError:
            pass
    if DB_CONNECTION:
        DB_CONNECTION.close()
    await sunblock_log("Server shutting down.")


sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="public"), name="static")

socket_app = socketio.ASGIApp(sio, app)


# --- Routes ---

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request":          request,
        "is_authenticated": _check_session(request),
        "sim_mode":         SIM_MODE,
    })

@app.get("/api/mode")
async def get_mode():
    return {"mode": "simulator" if SIM_MODE else "live"}


# Auth

@app.post("/api/login")
@limiter.limit("5/minute")
async def login(request: Request, body: LoginRequest, response: Response):
    if not ADMIN_PASSWORD_HASH:
        raise HTTPException(status_code=503, detail="Admin password not configured on server.")
    if body.username != ADMIN_USERNAME or not _bcrypt.checkpw(body.password.encode(), ADMIN_PASSWORD_HASH.encode()):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    response.set_cookie(
        key="sb_session",
        value=_create_token(body.username),
        httponly=True,
        secure=SECURE_COOKIES,
        samesite="strict",
        max_age=TOKEN_EXPIRE_HOURS * 3600,
    )
    return {"message": "Logged in"}

@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie("sb_session", samesite="strict")
    return {"message": "Logged out"}

@app.get("/api/auth/status")
async def auth_status(request: Request):
    return {"authenticated": _check_session(request)}


# Live data

@app.get("/api/data")
async def get_data():
    return JSONResponse(content=SOLAR_DATA, status_code=200)


# Power profile

@app.get("/api/power-profile")
async def get_power_profile(_=Depends(require_controller)):
    loop = asyncio.get_running_loop()
    output = await loop.run_in_executor(None, check_power_profile)
    return {"profile": output}

@app.post("/api/performance-mode")
async def set_performance(user: str = Depends(verify_session), _=Depends(require_real_controller)):
    loop = asyncio.get_running_loop()
    output = await loop.run_in_executor(None, set_power_profile, "performance")
    return {"profile": output}

@app.post("/api/power-saver-mode")
async def set_power_saver(user: str = Depends(verify_session), _=Depends(require_real_controller)):
    loop = asyncio.get_running_loop()
    output = await loop.run_in_executor(None, set_power_profile, "power-saver")
    return {"profile": output}

@app.post("/api/balanced")
async def set_balanced(user: str = Depends(verify_session), _=Depends(require_real_controller)):
    loop = asyncio.get_running_loop()
    output = await loop.run_in_executor(None, set_power_profile, "balanced")
    return {"profile": output}


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
    def do_sync():
        CONTROLLER.set_rtc(datetime.now())
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, do_sync)
    return {"message": "RTC synced to server time"}


# --- Socket.IO events ---

@sio.event
async def connect(sid, environ):
    global ACTIVE_USERS
    async with ACTIVE_USERS_LOCK:
        ACTIVE_USERS += 1
    print("Client connected:", sid)

@sio.event
async def disconnect(sid):
    global ACTIVE_USERS
    async with ACTIVE_USERS_LOCK:
        ACTIVE_USERS -= 1
    print("Client disconnected:", sid)
