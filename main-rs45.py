from epevermodbus.driver import EpeverChargeController

CONTROLLER = EpeverChargeController("/dev/ttyACM0", 1)


SOLAR_DATA = {
        "PVVoltage" : 0,
        "PVCurrent" : 0, 
        "PVPower" : 0}

def read_controller_values():
    SOLAR_DATA["PVVoltage"] = CONTROLLER.get_solar_voltage()
    SOLAR_DATA["PVPower"] = CONTROLLER.get_solar_power()
    SOLAR_DATA["PVCurrent"] = CONTROLLER.get_solar_current()

read_controller_values()

print(SOLAR_DATA)
