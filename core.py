from dotenv import load_dotenv
import serial
import time
import re
import json
import subprocess
from datetime import datetime
import os
from enum import Enum
import sqlite3


USBDEV = "/dev/ttyUSB0"
DATA_DIRECTORY = "/home/pc/SunblockData/"
POWER_DRAW_SCRIPT_ADDR = "/home/pc/power_scripts/powerdraw.sh"
ACTIVE_DATA = DATA_DIRECTORY + "solar_data.json"
POWER_LOGS_FILE = "/home/pc/SunblockData/SunblockPowerlogs.txt"

DATA_MAN = True

DB_NAME = DATA_DIRECTORY + "sunblockone.db"
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
    "BattChargeCurrent": 0,
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
            DB_CURSOR.execute("CREATE TABLE solardata(Timestamp text, PVVoltage real, PVCurrent real, PVPower real, BattVoltage real, BattChargeCurrent real, BattChargePower real, LoadPower real, BattPercentage int, BattOverallCurrent real, CPUPowerDraw real, PowerProfile text)")
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

def ParseData(data_string):
    global JSON_DATA

    data_array = data_string.strip().split(" ")
def CheckPowerProfile():
    return os.popen("sudo powerprofilesctl get").read().strip()


    JSON_DATA["Timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    JSON_DATA["PVVoltage"] = data_array[0].strip()
    JSON_DATA["PVCurrent"] = data_array[1].strip()
    JSON_DATA["PVPower"] = data_array[2].strip()

    JSON_DATA["BattVoltage"] = data_array[3].strip()
    JSON_DATA["BattChargeCurrent"] = data_array[4].strip()
    JSON_DATA["BattChargePower"] = data_array[5].strip()
    JSON_DATA["BattOverallCurrent"] = data_array[9].strip()
    JSON_DATA["BattPercentage"] = data_array[7].strip()

    JSON_DATA["LoadPower"] = data_array[6].strip()

    powerdraw = subprocess.run(POWER_DRAW_SCRIPT_ADDR, capture_output=True)
    powerdraw = powerdraw.stdout.decode().replace("W", "").strip()
    JSON_DATA["CPUPowerDraw"] = powerdraw
    
    JSON_DATA["PowerProfile"] = os.popen("sudo powerprofilesctl get").read().strip()


# POWER MANAGEMENT
def Powerlog(log):
    with open(POWER_LOGS_FILE, 'a') as log_file:
        log_file.write(datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S") + ": " + log + "\n")


def Main():
    first_time = True
    load_dotenv()
    Powerlog("\nWaking Up...")
    Powerlog("Power Management is " + str(POWER_MAN))
    Powerlog("Data Management is " + str(DATA_MAN))

    print("Starting serial reader from: ", USBDEV)
    SerialObj = serial.Serial(USBDEV, 115200)
    time.sleep(3)  # MSA: Why is this done?
    SerialObj.timeout = 3  # read timeout
    CheckDB()

    GetAMPInstance()

    while (SerialObj.is_open):
        try:
            ReceivedString = SerialObj.readline().decode("utf-8")
            matches = re.findall("(\d)+\.\d\d", ReceivedString)
            if (matches and len(matches) == 10):

                ParseData(ReceivedString)

                WriteJSON()
                if DATA_MAN:
                    WriteDB()

                if POWER_MAN:
                    ProfileAndStateValidation()
                    if first_time:
                        Powerlog(str(JSON_DATA))
                        Powerlog("State: " + str(CURRENT_STATE) +
                                 ", Power Saving: " + str(POWER_SAVER_ON))
                        CheckFailsafes()
                        first_time = False

                    PowerStateManagement()
                    PowerProfileManagement()

        except Exception or KeyboardInterrupt:
            Powerlog("Error: Couldnt continue data logging. \n\nExiting...")
            SerialObj.close()
            if DB_CONNECTION:
               DB_CONNECTION.close()
            return
    Powerlog("Exiting While loop. Serial Object Closed.")


Main()
