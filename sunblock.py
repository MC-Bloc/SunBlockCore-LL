'''
SunBlock — unified data collection + API server

Written by M. Shahrom Ali (github.com/estineali)
for The SunBlock Project
under the TAG MC-Bloc, Milieux Institute, Concordia University, Montreal, Canada.

Check it out at https://github.com/MC-Bloc/SunBlock

Setup notes:
1. Update the path constants below to match your server.
2. The solar controller address is /dev/ttyACM0 for our computer.
   To find yours, connect the RS485 cable and run `sudo dmesg` in the terminal.
3. All data is stored in ~/SunblockData (absolute: /home/{YOUR_USER_NAME}/SunblockData).
4. The server's user account was granted passwordless sudo — see sudo visudo.
5. Set ADMIN_USERNAME, ADMIN_PASSWORD, and SECRET_KEY in your .env before deploying.

Run with:
    uvicorn sunblock:socket_app --host 0.0.0.0 --port ${PORT:-3000}

Install dependencies:
    pip install fastapi uvicorn python-socketio epevermodbus python-dotenv python-jose[cryptography]

Requires Python 3.9+ (uses asyncio.to_thread).
'''

import asyncio
import os
import sqlite3
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional

from dotenv import load_dotenv
from jose import JWTError, jwt
from pydantic import BaseModel
import socketio
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from epevermodbus.driver import EpeverChargeController

load_dotenv()

# --- Config ---

CONTROLLER_PORT = os.getenv("CONTROLLER_PORT", "/dev/ttyACM0")
CONTROLLER_SLAVE = int(os.getenv("CONTROLLER_SLAVE", 1))
DATA_DIRECTORY = os.getenv("DATA_DIRECTORY", "/home/pc/SunblockData/")
POWER_DRAW_SCRIPT_ADDR = os.getenv("POWER_DRAW_SCRIPT_ADDR", "/home/pc/power_scripts/powerdraw.sh")
POWER_LOGS_FILE = os.path.join(DATA_DIRECTORY, "SunBlockCoreLogs.txt")
STATIC_DIR = os.getenv("STATIC_DIR", "/home/pc/GitHub/SunBlockExpress/public")

DATA_MAN = os.getenv("DATA_MAN", "true").lower() == "true"
DB_NAME = os.path.join(DATA_DIRECTORY, "SunBlockCore-LL.db")
DB_TABLE_NAME = "solardata"
READ_INTERVAL = int(os.getenv("READ_INTERVAL", 1))  # seconds

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")
SECRET_KEY = os.getenv("SECRET_KEY", "changeme-secret-key")
TOKEN_EXPIRE_HOURS = int(os.getenv("TOKEN_EXPIRE_HOURS", 24))


# --- State ---
# CONTROLLER, ACTIVE_USERS_LOCK, POLLING_TASK initialised in lifespan

CONTROLLER = None
ACTIVE_USERS = 0
ACTIVE_USERS_LOCK = None
POLLING_ACTIVE = False
POLLING_TASK = None
DB_CONNECTION = None
DB_CURSOR = None

SOLAR_DATA = {
    "Timestamp": "",
    "PVVoltage": 0,
    "PVCurrent": 0,
    "PVPower": 0,
    "BattVoltage": 0,
    "BattTemperature": 0,
    "BattChargePower": 0,
    "LoadPower": 0,
    "BattPercentage": 0,
    "BattOverallCurrent": 0,
    "CPUPowerDraw": 0,
    "PowerProfile": "",
}


# --- Auth ---

class LoginRequest(BaseModel):
    username: str
    password: str

class ControllerParamsUpdate(BaseModel):
    battery_capacity: Optional[int] = None
    temperature_compensation_coefficient: Optional[float] = None
    voltage_controls: Optional[dict] = None

_security = HTTPBearer()

def _create_token(username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS)
    return jwt.encode({"sub": username, "exp": expire}, SECRET_KEY, algorithm="HS256")

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(_security)) -> str:
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=["HS256"])
        username: str = payload.get("sub")
        if username != ADMIN_USERNAME:
            raise HTTPException(status_code=401, detail="Invalid token")
        return username
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

def require_controller():
    if CONTROLLER is None:
        raise HTTPException(status_code=503, detail="Controller not connected")


# --- Logging ---

async def sunblock_log(message):
    line = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + ": " + message + "\n"
    await asyncio.to_thread(_write_log, line)


def _write_log(line):
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
    DB_CURSOR.execute(f"INSERT INTO {DB_TABLE_NAME} VALUES ({placeholders})", list(SOLAR_DATA.values()))
    DB_CONNECTION.commit()


# --- Hardware polling ---

def check_power_profile():
    result = subprocess.run(["sudo", "powerprofilesctl", "get"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"powerprofilesctl get failed with code {result.returncode}")
    return result.stdout.strip()


def set_power_profile(profile):
    result = subprocess.run(["sudo", "powerprofilesctl", "set", profile])
    if result.returncode != 0:
        raise RuntimeError(f"powerprofilesctl set {profile} failed with code {result.returncode}")
    return check_power_profile()


def parse_data():
    data = {}
    data["Timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["PVVoltage"] = CONTROLLER.get_solar_voltage()
    data["PVCurrent"] = CONTROLLER.get_solar_current()
    data["PVPower"] = CONTROLLER.get_solar_power()
    data["BattVoltage"] = CONTROLLER.get_battery_voltage()
    data["BattTemperature"] = CONTROLLER.get_battery_temperature()
    data["BattChargePower"] = CONTROLLER.get_battery_power()
    data["BattOverallCurrent"] = CONTROLLER.get_battery_current()
    data["BattPercentage"] = CONTROLLER.get_battery_state_of_charge()
    data["LoadPower"] = CONTROLLER.get_load_power()
    result = subprocess.run([POWER_DRAW_SCRIPT_ADDR], capture_output=True)
    data["CPUPowerDraw"] = result.stdout.decode().replace("W", "").strip()
    data["PowerProfile"] = check_power_profile()
    return data


# --- Controller parameter read/write ---

def read_controller_params():
    return {
        "battery_type":                          str(CONTROLLER.get_battery_type()),
        "battery_capacity":                      CONTROLLER.get_battery_capacity(),
        "battery_rated_voltage":                 str(CONTROLLER.get_battery_rated_voltage()),
        "charging_mode":                         str(CONTROLLER.get_charging_mode()),
        "temperature_compensation_coefficient":  CONTROLLER.get_temperature_compensation_coefficient(),
        "default_load_on_off":                   str(CONTROLLER.get_default_load_on_off_in_manual_mode()),
        "equalize_duration":                     CONTROLLER.get_equalize_duration(),
        "boost_duration":                        CONTROLLER.get_boost_duration(),
        "voltage_controls":                      CONTROLLER.get_battery_voltage_control_registers(),
    }


def read_controller_stats():
    return {
        "pv_voltage_max_today":      CONTROLLER.get_maximum_pv_voltage_today(),
        "pv_voltage_min_today":      CONTROLLER.get_minimum_pv_voltage_today(),
        "batt_voltage_max_today":    CONTROLLER.get_maximum_battery_voltage_today(),
        "batt_voltage_min_today":    CONTROLLER.get_minimum_battery_voltage_today(),
        "generated_today":           CONTROLLER.get_generated_energy_today(),
        "generated_this_month":      CONTROLLER.get_generated_energy_this_month(),
        "generated_this_year":       CONTROLLER.get_generated_energy_this_year(),
        "total_generated":           CONTROLLER.get_total_generated_energy(),
        "consumed_today":            CONTROLLER.get_consumed_energy_today(),
        "consumed_this_month":       CONTROLLER.get_consumed_energy_this_month(),
        "consumed_this_year":        CONTROLLER.get_consumed_energy_this_year(),
        "total_consumed":            CONTROLLER.get_total_consumed_energy(),
    }


def read_controller_status():
    return {
        "battery_status":            CONTROLLER.get_battery_status(),
        "charging_status":           CONTROLLER.get_charging_equipment_status(),
        "discharging_status":        CONTROLLER.get_discharging_equipment_status(),
        "is_day":                    CONTROLLER.is_day(),
        "controller_temperature":    CONTROLLER.get_controller_temperature(),
        "remote_battery_temperature": CONTROLLER.get_remote_battery_temperature(),
        "load_voltage":              CONTROLLER.get_load_voltage(),
        "load_current":              CONTROLLER.get_load_current(),
        "rtc":                       str(CONTROLLER.get_rtc()),
        "rated_charging_current":    CONTROLLER.get_rated_charging_current(),
        "rated_load_current":        CONTROLLER.get_rated_load_current(),
    }


def apply_controller_params(update: dict):
    if update.get("battery_capacity") is not None:
        CONTROLLER.set_battery_capacity(update["battery_capacity"])
    if update.get("temperature_compensation_coefficient") is not None:
        CONTROLLER.set_temperature_compensation_coefficient(update["temperature_compensation_coefficient"])
    if update.get("voltage_controls") is not None:
        CONTROLLER.set_battery_voltage_control_registers_dict(update["voltage_controls"])


async def polling_loop():
    global SOLAR_DATA, POLLING_ACTIVE
    await sunblock_log("Waking Up...")
    if DATA_MAN:
        await sunblock_log("Data Management is " + str(DATA_MAN))
        await asyncio.to_thread(check_db)

    loop = asyncio.get_running_loop()
    while POLLING_ACTIVE:
        try:
            new_data = await loop.run_in_executor(None, parse_data)
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

    try:
        CONTROLLER = EpeverChargeController(CONTROLLER_PORT, CONTROLLER_SLAVE)
    except Exception as e:
        await sunblock_log("Failed to connect to controller: " + str(e))

    ACTIVE_USERS_LOCK = asyncio.Lock()

    if CONTROLLER is not None:
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "PUT", "POST", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

socket_app = socketio.ASGIApp(sio, app)


# --- REST endpoints ---

@app.get("/api/data")
async def get_data():
    return JSONResponse(content=SOLAR_DATA, status_code=200)


@app.post("/api/login")
async def login(body: LoginRequest):
    if body.username == ADMIN_USERNAME and body.password == ADMIN_PASSWORD:
        return {"token": _create_token(body.username)}
    raise HTTPException(status_code=401, detail="Invalid credentials")


# Power profile

@app.get("/api/power-profile")
async def get_power_profile(_=Depends(require_controller)):
    loop = asyncio.get_running_loop()
    output = await loop.run_in_executor(None, check_power_profile)
    return JSONResponse(content={"body": "Current Profile: " + output}, status_code=200)


@app.post("/api/performance-mode")
async def set_performance(user: str = Depends(verify_token), _=Depends(require_controller)):
    loop = asyncio.get_running_loop()
    output = await loop.run_in_executor(None, set_power_profile, "performance")
    return JSONResponse(content={"response": "Profile changed successfully to " + output}, status_code=200)


@app.post("/api/power-saver-mode")
async def set_power_saver(user: str = Depends(verify_token), _=Depends(require_controller)):
    loop = asyncio.get_running_loop()
    output = await loop.run_in_executor(None, set_power_profile, "power-saver")
    return JSONResponse(content={"response": "Profile changed successfully to " + output}, status_code=200)


@app.post("/api/balanced")
async def set_balanced(user: str = Depends(verify_token), _=Depends(require_controller)):
    loop = asyncio.get_running_loop()
    output = await loop.run_in_executor(None, set_power_profile, "balanced")
    return JSONResponse(content={"response": "Profile changed successfully to " + output}, status_code=200)


# Controller parameters

@app.get("/api/controller/parameters")
async def get_controller_params(_=Depends(require_controller)):
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, read_controller_params)
    return JSONResponse(content=data, status_code=200)


@app.put("/api/controller/parameters")
async def update_controller_params(
    body: ControllerParamsUpdate,
    user: str = Depends(verify_token),
    _=Depends(require_controller),
):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, apply_controller_params, body.model_dump())
    data = await loop.run_in_executor(None, read_controller_params)
    return JSONResponse(content=data, status_code=200)


@app.get("/api/controller/stats")
async def get_controller_stats(_=Depends(require_controller)):
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, read_controller_stats)
    return JSONResponse(content=data, status_code=200)


@app.get("/api/controller/status")
async def get_controller_status(_=Depends(require_controller)):
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, read_controller_status)
    return JSONResponse(content=data, status_code=200)


@app.post("/api/controller/rtc/sync")
async def sync_rtc(user: str = Depends(verify_token), _=Depends(require_controller)):
    def do_sync():
        CONTROLLER.set_rtc(datetime.now())
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, do_sync)
    return JSONResponse(content={"message": "RTC synced to server time"}, status_code=200)


if os.path.isdir(STATIC_DIR):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


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
