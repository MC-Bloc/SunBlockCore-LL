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

CONTROLLER = EpeverChargeController(os.getenv("CONTROLLER_PORT", "/dev/ttyACM0"), int(os.getenv("CONTROLLER_SLAVE", 1)))
DATA_DIRECTORY = os.getenv("DATA_DIRECTORY", "/home/pc/SunblockData/")
POWER_DRAW_SCRIPT_ADDR = os.getenv("POWER_DRAW_SCRIPT_ADDR", "/home/pc/power_scripts/powerdraw.sh")
POWER_LOGS_FILE = DATA_DIRECTORY + "SunBlockCoreLogs.txt"
STATIC_DIR = os.getenv("STATIC_DIR", "/home/pc/GitHub/SunBlockExpress/public")

DATA_MAN = os.getenv("DATA_MAN", "true").lower() == "true"
DB_NAME = DATA_DIRECTORY + "SunBlockCore-LL.db"
DB_TABLE_NAME = "solardata"
READ_INTERVAL = int(os.getenv("READ_INTERVAL", 1))  # seconds


# --- State ---

ACTIVE_USERS = 0
ACTIVE_USERS_LOCK = asyncio.Lock()
POLLING_ACTIVE = True
DB_CONNECTION = None
DB_CURSOR = None

SOLAR_DATA = {
    "Timestamp": 0,
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

def sunblock_log(message):
    with open(POWER_LOGS_FILE, 'a') as f:
        f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S") + ": " + message + "\n")


# --- Database ---

def check_db():
    global DB_CONNECTION, DB_CURSOR
    if DB_CONNECTION is not None and DB_CURSOR is not None:
        return
    create_table = not os.path.isfile(DB_NAME)
    DB_CONNECTION = sqlite3.connect(DB_NAME)
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
    db_query = f"INSERT INTO {DB_TABLE_NAME} VALUES ({placeholders})"
    DB_CURSOR.execute(db_query, list(SOLAR_DATA.values()))
    DB_CONNECTION.commit()


# --- Hardware polling ---

def check_power_profile():
    result = subprocess.run(["sudo", "powerprofilesctl", "get"], capture_output=True, text=True)
    return result.stdout.strip()


def set_power_profile(profile):
    subprocess.run(["sudo", "powerprofilesctl", "set", profile])
    return check_power_profile()


def parse_data():
    SOLAR_DATA["Timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    SOLAR_DATA["PVVoltage"] = CONTROLLER.get_solar_voltage()
    SOLAR_DATA["PVCurrent"] = CONTROLLER.get_solar_current()
    SOLAR_DATA["PVPower"] = CONTROLLER.get_solar_power()
    SOLAR_DATA["BattVoltage"] = CONTROLLER.get_battery_voltage()
    SOLAR_DATA["BattTemperature"] = CONTROLLER.get_battery_temperature()
    SOLAR_DATA["BattChargePower"] = CONTROLLER.get_battery_power()
    SOLAR_DATA["BattOverallCurrent"] = CONTROLLER.get_battery_current()
    SOLAR_DATA["BattPercentage"] = CONTROLLER.get_battery_state_of_charge()
    SOLAR_DATA["LoadPower"] = CONTROLLER.get_load_power()
    result = subprocess.run(POWER_DRAW_SCRIPT_ADDR, capture_output=True)
    SOLAR_DATA["CPUPowerDraw"] = result.stdout.decode().replace("W", "").strip()
    SOLAR_DATA["PowerProfile"] = check_power_profile()


async def polling_loop():
    global POLLING_ACTIVE
    sunblock_log("Waking Up...")
    if DATA_MAN:
        sunblock_log("Data Management is " + str(DATA_MAN))
        check_db()

    loop = asyncio.get_event_loop()
    while POLLING_ACTIVE:
        try:
            await loop.run_in_executor(None, parse_data)
            if DATA_MAN:
                write_db()
            await sio.emit("solar_data", {**SOLAR_DATA, "ConnectedUsers": ACTIVE_USERS})
        except Exception as e:
            sunblock_log("Error during polling: " + str(e))
            POLLING_ACTIVE = False
            break
        await asyncio.sleep(READ_INTERVAL)

    sunblock_log("Exiting polling loop. Controller unavailable.")


# --- App setup ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(polling_loop())
    yield
    if DB_CONNECTION:
        DB_CONNECTION.close()
    sunblock_log("Server shutting down.")


sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "PUT", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)

if os.path.isdir(STATIC_DIR):
    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

socket_app = socketio.ASGIApp(sio, app)


# --- REST endpoints ---

@app.get("/")
async def get_data():
    return JSONResponse(content=SOLAR_DATA, status_code=200)


@app.get("/power-profile")
async def get_power_profile():
    loop = asyncio.get_event_loop()
    output = await loop.run_in_executor(None, check_power_profile)
    return JSONResponse(content={"body": "Current Profile: " + output}, status_code=200)


@app.post("/performance-mode")
async def set_performance():
    loop = asyncio.get_event_loop()
    output = await loop.run_in_executor(None, set_power_profile, "performance")
    return JSONResponse(content={"response": "Profile changed successfully to " + output}, status_code=200)


@app.post("/power-saver-mode")
async def set_power_saver():
    loop = asyncio.get_event_loop()
    output = await loop.run_in_executor(None, set_power_profile, "power-saver")
    return JSONResponse(content={"response": "Profile changed successfully to " + output}, status_code=200)


@app.post("/balanced")
async def set_balanced():
    loop = asyncio.get_event_loop()
    output = await loop.run_in_executor(None, set_power_profile, "balanced")
    return JSONResponse(content={"response": "Profile changed successfully to " + output}, status_code=200)


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
