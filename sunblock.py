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

Run with:
    uvicorn sunblock:socket_app --host 0.0.0.0 --port ${PORT:-3000}

Install dependencies:
    pip install fastapi uvicorn python-socketio epevermodbus python-dotenv

Requires Python 3.9+ (uses asyncio.to_thread).
'''

import asyncio
import os
import sqlite3
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime

from dotenv import load_dotenv
import socketio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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
    allow_headers=["Content-Type"],
)

socket_app = socketio.ASGIApp(sio, app)


# --- REST endpoints ---

@app.get("/api/data")
async def get_data():
    return JSONResponse(content=SOLAR_DATA, status_code=200)


@app.get("/api/power-profile")
async def get_power_profile():
    loop = asyncio.get_running_loop()
    output = await loop.run_in_executor(None, check_power_profile)
    return JSONResponse(content={"body": "Current Profile: " + output}, status_code=200)


@app.post("/api/performance-mode")
async def set_performance():
    loop = asyncio.get_running_loop()
    output = await loop.run_in_executor(None, set_power_profile, "performance")
    return JSONResponse(content={"response": "Profile changed successfully to " + output}, status_code=200)


@app.post("/api/power-saver-mode")
async def set_power_saver():
    loop = asyncio.get_running_loop()
    output = await loop.run_in_executor(None, set_power_profile, "power-saver")
    return JSONResponse(content={"response": "Profile changed successfully to " + output}, status_code=200)


@app.post("/api/balanced")
async def set_balanced():
    loop = asyncio.get_running_loop()
    output = await loop.run_in_executor(None, set_power_profile, "balanced")
    return JSONResponse(content={"response": "Profile changed successfully to " + output}, status_code=200)


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
