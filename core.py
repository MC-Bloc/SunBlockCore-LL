from dotenv import load_dotenv
import json
import subprocess
from datetime import datetime
import os
from enum import Enum
import sqlite3
from epevermodbus.driver import EpeverChargeController


CONTROLLER = EpeverChargeController("/dev/ttyACM0", 1)
DATA_DIRECTORY = "/home/pc/SunblockData/"
POWER_DRAW_SCRIPT_ADDR = "/home/pc/power_scripts/powerdraw.sh"
ACTIVE_DATA = DATA_DIRECTORY + "solar_data.json"
POWER_LOGS_FILE = "/home/pc/SunblockData/SunBlockCoreLogs.txt"

DATA_MAN = True
DB_NAME = DATA_DIRECTORY + "SunBlockCore-LL.db"
DB_TABLE_NAME = "solardata"
DB_CONNECTION = None
DB_CURSOR = None

# To be initialized in OpenCSVFile
LOG_FILE_NAME = None
JSON_DATA = {
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

def WriteJSON():
    # active data is the data being used by other services.
    # this is in JSON format and kept in the same directory as the minecraft server.
    json_file = open(ACTIVE_DATA, "w")
    json.dump(JSON_DATA, json_file, indent=4)
    json_file.close()


def CheckDB():
    global DB_CONNECTION, DB_CURSOR

    if DB_CONNECTION != None and DB_CURSOR != None:
        return True
    else:
        if not os.path.isfile(DB_NAME):
            DB_CONNECTION = sqlite3.connect(DB_NAME)
            DB_CURSOR = DB_CONNECTION.cursor()
            DB_CURSOR.execute("CREATE TABLE solardata(Timestamp text, PVVoltage real, PVCurrent real, PVPower real, BattVoltage real, BattTemperature real, BattChargePower real, LoadPower real, BattPercentage int, BattOverallCurrent real, CPUPowerDraw real, PowerProfile text)")
        else:
            DB_CONNECTION = sqlite3.connect(DB_NAME)
            DB_CURSOR = DB_CONNECTION.cursor()
        return True


def WriteDB():
    global DB_CURSOR, DB_CONNECTION
    if CheckDB():
        db_query = "INSERT INTO " + DB_TABLE_NAME + " VALUES " + str(tuple(JSON_DATA.values()))
        DB_CURSOR.execute(db_query)
        DB_CONNECTION.commit()


def CheckPowerProfile():
    return os.popen("sudo powerprofilesctl get").read().strip()


def ParseData():
    global JSON_DATA

    JSON_DATA["Timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    JSON_DATA["PVVoltage"] = CONTROLLER.get_solar_voltage()
    JSON_DATA["PVCurrent"] = CONTROLLER.get_solar_current()
    JSON_DATA["PVPower"] = CONTROLLER.get_solar_power()

    JSON_DATA["BattVoltage"] = CONTROLLER.get_battery_voltage()
    JSON_DATA["BattTemperature"] = CONTROLLER.get_battery_temperature()
    JSON_DATA["BattChargePower"] = CONTROLLER.get_battery_power()
    JSON_DATA["BattOverallCurrent"] = CONTROLLER.get_battery_current()
    JSON_DATA["BattPercentage"] = CONTROLLER.get_battery_state_of_charge()

    JSON_DATA["LoadPower"] = CONTROLLER.get_load_power()

    powerdraw = subprocess.run(POWER_DRAW_SCRIPT_ADDR, capture_output=True)
    powerdraw = powerdraw.stdout.decode().replace("W", "").strip()
    JSON_DATA["CPUPowerDraw"] = powerdraw
    JSON_DATA["PowerProfile"] = CheckPowerProfile()


def SunBlockLog(log):
    with open(POWER_LOGS_FILE, 'a') as log_file:
        log_file.write(datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S") + ": " + log + "\n")


def Main():
    SunBlockLog("Waking Up...")

    load_dotenv()

    if DATA_MAN:
        SunBlockLog("Data Management is " + str(DATA_MAN))
        CheckDB()

    while (CONTROLLER != None):
        try:
            ParseData()
            WriteJSON()
            
            if DATA_MAN:
                WriteDB()

        except Exception or KeyboardInterrupt:
            SunBlockLog(
                "Error: Couldnt continue data logging. \nExiting...\n\n")
            if DB_CONNECTION:
               DB_CONNECTION.close()
            return
    SunBlockLog("Exiting While loop. Serial Object Closed.")


Main()
