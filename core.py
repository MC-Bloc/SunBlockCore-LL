from ampapi.modules.ADS import ADS
from dotenv import load_dotenv
import serial
import time
import re
import json
import subprocess
from datetime import datetime
import csv
import os
from enum import Enum
import sqlite3


USBDEV = "/dev/ttyUSB0"
DATA_DIRECTORY = "/home/pc/SunblockData/"
POWER_DRAW_SCRIPT_ADDR = "/home/pc/power_scripts/powerdraw.sh"
ACTIVE_DATA = DATA_DIRECTORY + "solar_data.json"
POWER_LOGS_FILE = "/home/pc/SunblockData/SunblockPowerlogs.txt"

POWER_MAN = False
DATA_MAN = True

DB_NAME = DATA_DIRECTORY + "sunblockone.db"
DB_TABLE_NAME = "solardata"
DB_CONNECTION = None
DB_CURSOR = None
'''
SYSTEM STATES:

HOT: System is on and MC Server is on.
WARM: System is on and MC Server is off. This is the default assumed state 
COOL: System is off. Minimal battery remaining
COLD: Sunblock as a whole is now off - battery is dead.

COOL and COLD happen outside the system so not included as part of this script

Values sequence: 
WARM_HEATUP > HOT_COOLDOWN > WARM_COOLDOWN  >= FAILSAFE

'''


SYSTEM_STATES = Enum("SYSTEM_STATE", ["HOT", "WARM"])
CURRENT_STATE = SYSTEM_STATES.WARM  # Default system state


# VOLTAGE THRESHOLDS - in volts
SUNBLOCK_WARM_HEATUP = 13.00  # From WARM to HOT
SUNBLOCK_HOT_COOLDOWN = 12.85  # From HOT to WARM
SUNBLOCK_WARM_COOLDOWN = 12.18  # From WARM to COOL

# 11.90 is the hard limit to protect the battery in the Solar controller.
# WARM_COOLDOWN must not be lower than this value.
FAILSAFE_LOWERBOUND = 12.00


# POWER THRESHOLD - in watts
TO_POWER_SAVER = 12.5  # if loadpower is at or below this, go to power saver
TO_PERFORMANCE = 15.0  # if loadpower is above this, go to performance mode
POWER_SAVER_ON = True  # Default - must stay on

AMP_INSTANCE_NAME = "Sunblock01"
# AMP CubeCoders Instances
AMP_CORE_INSTANCE = None
SUNBLOCK_AMP_INSTANCE = None
SUNBLOCK_AMP_MC_INSTANCE = None

'''
AMP Instance State values and meanings: 

Undefined = -1
Stopped = 0
PreStart = 5
Configuring = 7 # The server is performing some first-time-start configuration.
Starting = 10
Ready = 20
Restarting = 30 # Server is in the middle of stopping, but once shutdown has finished it will automatically restart.
Stopping = 40
PreparingForSleep = 45
Sleeping = 50 # The application should be able to be resumed quickly if using this state. Otherwise use Stopped.
Waiting = 60 # The application is waiting for some external service/application to respond/become available.
Installing = 70
Updating = 75
AwaitingUserInput = 80 # Used during installation, means that some user input is required to complete setup (authentication etc).
Failed = 100
Suspended = 200
Maintainence = 250
Indeterminate = 999 # The state is unknown, or doesn't apply (for modules that don't start an external process)
'''


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


def is_float(string):
    try:
        float(string)
        return True
    except ValueError:
        return False


def PrintJSON():
    subprocess.call("clear")
    for i in JSON_DATA:
        print(i, ":", JSON_DATA[i])
    print("\n\n")


def WriteJSON():
    # active data is the data being used by other services.
    # this is in JSON format and kept in the same directory as the minecraft server.
    json_file = open(ACTIVE_DATA, "w")
    json.dump(JSON_DATA, json_file, indent=4)
    json_file.close()


'''
Deprecated: CSVs are deprecated. Use Database instead
'''

def OpenCSVFile():
    global LOG_FILE_NAME

    LOG_FILE_NAME = DATA_DIRECTORY + "solar_data_log---" + \
        str(datetime.now().strftime("%Y-%m-%d-%H:%M:%S")) + ".csv"
    with open(LOG_FILE_NAME, 'w', newline='') as log_file:
        csv_header = JSON_DATA.keys()
        csv_writer = csv.writer(log_file)
        csv_writer.writerow(csv_header)

'''
Deprecated: CSVs are deprecated. Use Database instead
'''
def WriteCSV():

    if (not os.path.isfile(LOG_FILE_NAME)):
        OpenCSVFile()

    with open(LOG_FILE_NAME, 'a', newline='') as log_file:
        csv_writer = csv.writer(log_file)
        csv_writer.writerow(JSON_DATA.values())


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
    return False


def WriteDB():
    global DB_CURSOR, DB_CONNECTION
    if CheckDB():
        db_query = "INSERT INTO " + DB_TABLE_NAME + " VALUES " + str(tuple(JSON_DATA.values()))
        DB_CURSOR.execute(db_query)
        DB_CONNECTION.commit()

def ParseData(data_string):
    global JSON_DATA

    data_array = data_string.strip().split(" ")

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


def MCServerOn():
    if (AMP_CORE_INSTANCE and SUNBLOCK_AMP_MC_INSTANCE and SUNBLOCK_AMP_INSTANCE):
        # Get the current CPU usage
        currentStatus = SUNBLOCK_AMP_MC_INSTANCE.Core.GetStatus()
        if currentStatus.State in [10, 20, 30]:
            return True
        elif currentStatus.State in [0, 40, 50, 60, 100]:
            return False
    else:
        Powerlog("function-> MCServerOn \nError: AMP SERVER ACCESS NOT FOUND \n\n")
        raise Exception

    return False


def CheckPowerProfile():
    return os.popen("sudo powerprofilesctl get").read().strip()


def SwitchProfilePerformance():
    global POWER_SAVER_ON
    os.system("sudo powerprofilesctl set performance")
    POWER_SAVER_ON = False
    Powerlog("Power Saver Off. Load Power: " + JSON_DATA["LoadPower"])


def SwitchProfileSaver():
    global POWER_SAVER_ON
    os.system("sudo powerprofilesctl set power-saver")
    POWER_SAVER_ON = True
    Powerlog("Power Saver On. Load Power: " + JSON_DATA["LoadPower"])


# To Hot
def StartMCServer():
    global CURRENT_STATE
    if (not MCServerOn()):
        AMP_CORE_INSTANCE.ADSModule.StartInstance(AMP_INSTANCE_NAME)
        CURRENT_STATE = SYSTEM_STATES.HOT
        Powerlog("Minecraft Server Turned On. Battery Voltage: " +
                 JSON_DATA["BattVoltage"])


# To WARM
def StopMCServer():
    global CURRENT_STATE
    if (MCServerOn()):
        AMP_CORE_INSTANCE.ADSModule.StopInstance(AMP_INSTANCE_NAME)
        CURRENT_STATE = SYSTEM_STATES.WARM
        Powerlog("Minecraft Server Turned Off. Battery Voltage: " +
                 JSON_DATA["BattVoltage"])


# To COOL
def SuspendSystem():
    Powerlog("Shutting Down. Battery Voltage: " + JSON_DATA["BattVoltage"])
    os.system("sudo systemctl suspend")


def ProfileAndStateValidation():
    global POWER_SAVER_ON
    POWER_SAVER_ON = CheckPowerProfile() == "power-saver"

    global CURRENT_STATE
    if MCServerOn() and CURRENT_STATE == SYSTEM_STATES.WARM:
        CURRENT_STATE = SYSTEM_STATES.HOT
    elif not MCServerOn() and CURRENT_STATE == SYSTEM_STATES.HOT:
        CURRENT_STATE = SYSTEM_STATES.WARM


def PowerStateManagement():
    battery_voltage = JSON_DATA["BattVoltage"]

    # Switch Power States
    if is_float(battery_voltage):
        battery_voltage = float(battery_voltage)

        if CURRENT_STATE == SYSTEM_STATES.WARM and battery_voltage > SUNBLOCK_WARM_HEATUP:
            # If system is WARM and can heat up, Turn it up to HOT
            StartMCServer()
        elif CURRENT_STATE == SYSTEM_STATES.HOT and battery_voltage < SUNBLOCK_HOT_COOLDOWN:
            # if system is HOT but can no longer stay HOT, turn it down to WARM
            StopMCServer()
        elif CURRENT_STATE == SYSTEM_STATES.WARM and battery_voltage < SUNBLOCK_WARM_COOLDOWN:
            SwitchProfileSaver()
            # If system is WARM but cant stay WARM, turn it down to COOL
            SuspendSystem()


def PowerProfileManagement():
    load_power = JSON_DATA["LoadPower"]
    # Optimize Power states
    if CURRENT_STATE == SYSTEM_STATES.HOT and is_float(load_power):
        load_power = float(load_power)
        # consider adding cpu power draw?
        if load_power > TO_PERFORMANCE and POWER_SAVER_ON:
            SwitchProfilePerformance()
        elif not POWER_SAVER_ON and load_power <= TO_POWER_SAVER:
            SwitchProfileSaver()

    if CURRENT_STATE == SYSTEM_STATES.WARM and not POWER_SAVER_ON:
        SwitchProfileSaver()


def CheckFailsafes():
    global SUNBLOCK_WARM_COOLDOWN
    if SUNBLOCK_WARM_COOLDOWN > SUNBLOCK_HOT_COOLDOWN:
        # If the Warm-cooldown is high, it may send the system into bootloops.
        # This is to avoid such a thing.
        SUNBLOCK_WARM_COOLDOWN = FAILSAFE_LOWERBOUND
        Powerlog(
            "Warning: System WARM cooldown is set too high. Change it to avoid system failure")


def GetAMPInstance():
    global AMP_CORE_INSTANCE
    global SUNBLOCK_AMP_INSTANCE
    global SUNBLOCK_AMP_MC_INSTANCE

    try:
        AMP_CORE_INSTANCE = ADS(os.environ.get("amp_endpoint"),
                                os.environ.get("amp_username"),
                                os.environ.get("amp_password"))
        AMP_CORE_INSTANCE.Login()

        instances = AMP_CORE_INSTANCE.ADSModule.GetInstances()[
            0].AvailableInstances
        for i in instances:
            if i.InstanceName == AMP_INSTANCE_NAME:
                SUNBLOCK_AMP_INSTANCE = i

        SUNBLOCK_AMP_MC_INSTANCE = AMP_CORE_INSTANCE.InstanceLogin(
            SUNBLOCK_AMP_INSTANCE.InstanceID, "Minecraft")

    except Exception:
        Powerlog("Error connecting to AMP instance.")


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


def Testing():
    print("IN TESTING MODE:")
    Powerlog("Testing mode: ")

    return None


Main()
# Testing()
