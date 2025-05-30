from dotenv import load_dotenv
import json
from datetime import datetime
import os
from epevermodbus.driver import EpeverChargeController


CONTROLLER = None
DATA_DIRECTORY = None
DATA_MAN = None


POWER_DRAW_SCRIPT_ADDR = "/home/pc/power_scripts/powerdraw.sh"
ACTIVE_DATA = DATA_DIRECTORY + "solar_data.json"


JSON_DATA = {
    "Timestamp": 0,
    "PVVoltage": 0,
    "PVCurrent": 0,
    "PVPower": 0,
    "BattPercentage": 0,
    "BattVoltage": 0,
    "LoadPower": 0,
    "BattOverallCurrent": 0,
    "CPUPowerDraw": 0,
    "PowerProfile": "",
    "BattTemperature": 0,
    "BattChargePower": 0,
}

def WriteJSON():
    # active data is the data being used by other services.
    # this is in JSON format and kept in the same directory as the minecraft server.
    json_file = open(ACTIVE_DATA, "w")
    json.dump(JSON_DATA, json_file, indent=4)
    json_file.close()

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

    # powerdraw = subprocess.run(POWER_DRAW_SCRIPT_ADDR, capture_output=True)
    # powerdraw = powerdraw.stdout.decode().replace("W", "").strip()
    JSON_DATA["CPUPowerDraw"] = -1
    JSON_DATA["PowerProfile"] = CheckPowerProfile()

def Main():
    global CONTROLLER, DATA_DIRECTORY

    load_dotenv()
    
    CONTROLLER = EpeverChargeController(os.getenv('CONTROLLER_ADDRESS'), 1)
    DATA_DIRECTORY = os.getenv('DATA_DIRECTORY')    

    while (CONTROLLER != None):
        try:
            ParseData()
            WriteJSON()

        except Exception or KeyboardInterrupt:
            return

Main()
