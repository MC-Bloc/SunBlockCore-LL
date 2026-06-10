"""Solar charge controller I/O: live data polling, power profiles, parameter r/w."""

import subprocess
import time
from datetime import datetime

import config

# ── Power-profile cache ───────────────────────────────────────────────────────
# check_power_profile() shells out to `sudo powerprofilesctl get`, which is
# expensive (~100–500 ms per call due to sudo + D-Bus overhead).  Power profiles
# change at most a few times a day, so caching the result for 30 s reduces the
# per-poll cost to a single dict lookup on 29 out of every 30 ticks.
_PROFILE_CACHE_TTL = 30.0          # seconds between real subprocess calls
_cached_profile:     str   = ""
_cached_profile_at:  float = 0.0


# ── Power profiles ────────────────────────────────────────────────────────────

def check_power_profile() -> str:
    """Read current power profile, with a 30-second cache to avoid per-poll subprocess cost."""
    global _cached_profile, _cached_profile_at
    now = time.monotonic()
    if _cached_profile and (now - _cached_profile_at) < _PROFILE_CACHE_TTL:
        return _cached_profile
    result = subprocess.run(["sudo", "powerprofilesctl", "get"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"powerprofilesctl get failed with code {result.returncode}")
    _cached_profile = result.stdout.strip()
    _cached_profile_at = now
    return _cached_profile


def set_power_profile(profile: str) -> str:
    global _cached_profile, _cached_profile_at
    result = subprocess.run(["sudo", "powerprofilesctl", "set", profile])
    if result.returncode != 0:
        raise RuntimeError(f"powerprofilesctl set {profile} failed with code {result.returncode}")
    # Force a fresh read so the cache reflects the change immediately.
    _cached_profile_at = 0.0
    return check_power_profile()


# ── Live data ─────────────────────────────────────────────────────────────────

def parse_data() -> dict:
    ctrl = config.CONTROLLER

    # CPUPowerDraw is optional — only measured when POWER_DRAW_SCRIPT_ADDR is set.
    # If the script is absent or fails, fall back to 0 so the polling loop keeps
    # running instead of crashing on a FileNotFoundError / TypeError.
    cpu_power: float = 0.0
    if config.POWER_DRAW_SCRIPT:
        try:
            result = subprocess.run(
                [config.POWER_DRAW_SCRIPT], capture_output=True, timeout=5
            )
            cpu_power = float(result.stdout.decode().replace("W", "").strip())
        except Exception:
            cpu_power = 0.0

    return {
        "Timestamp":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "PVVoltage":          ctrl.get_solar_voltage(),
        "PVCurrent":          ctrl.get_solar_current(),
        "PVPower":            ctrl.get_solar_power(),
        "BattVoltage":        ctrl.get_battery_voltage(),
        "BattTemperature":    ctrl.get_battery_temperature(),
        "BattChargePower":    ctrl.get_battery_power(),
        "BattOverallCurrent": ctrl.get_battery_current(),
        "BattPercentage":     ctrl.get_battery_state_of_charge(),
        "LoadPower":          ctrl.get_load_power(),
        "CPUPowerDraw":       cpu_power,
        "PowerProfile":       check_power_profile(),
    }


# ── Controller parameters ─────────────────────────────────────────────────────

def read_controller_params() -> dict:
    ctrl = config.CONTROLLER
    return {
        "battery_type":                         str(ctrl.get_battery_type()),
        "battery_capacity":                     ctrl.get_battery_capacity(),
        "battery_rated_voltage":                str(ctrl.get_battery_rated_voltage()),
        "charging_mode":                        str(ctrl.get_charging_mode()),
        "temperature_compensation_coefficient": ctrl.get_temperature_compensation_coefficient(),
        "default_load_on_off":                  str(ctrl.get_default_load_on_off_in_manual_mode()),
        "equalize_duration":                    ctrl.get_equalize_duration(),
        "boost_duration":                       ctrl.get_boost_duration(),
        "voltage_controls":                     ctrl.get_battery_voltage_control_registers(),
    }


def read_controller_stats() -> dict:
    ctrl = config.CONTROLLER
    return {
        "pv_voltage_max_today":   ctrl.get_maximum_pv_voltage_today(),
        "pv_voltage_min_today":   ctrl.get_minimum_pv_voltage_today(),
        "batt_voltage_max_today": ctrl.get_maximum_battery_voltage_today(),
        "batt_voltage_min_today": ctrl.get_minimum_battery_voltage_today(),
        "generated_today":        ctrl.get_generated_energy_today(),
        "generated_this_month":   ctrl.get_generated_energy_this_month(),
        "generated_this_year":    ctrl.get_generated_energy_this_year(),
        "total_generated":        ctrl.get_total_generated_energy(),
        "consumed_today":         ctrl.get_consumed_energy_today(),
        "consumed_this_month":    ctrl.get_consumed_energy_this_month(),
        "consumed_this_year":     ctrl.get_consumed_energy_this_year(),
        "total_consumed":         ctrl.get_total_consumed_energy(),
    }


def read_controller_status() -> dict:
    ctrl = config.CONTROLLER
    return {
        "battery_status":             ctrl.get_battery_status(),
        "charging_status":            ctrl.get_charging_equipment_status(),
        "discharging_status":         ctrl.get_discharging_equipment_status(),
        "is_day":                     ctrl.is_day(),
        "controller_temperature":     ctrl.get_controller_temperature(),
        "remote_battery_temperature": ctrl.get_remote_battery_temperature(),
        "load_voltage":               ctrl.get_load_voltage(),
        "load_current":               ctrl.get_load_current(),
        "rtc":                        str(ctrl.get_rtc()),
        "rated_charging_current":     ctrl.get_rated_charging_current(),
        "rated_load_current":         ctrl.get_rated_load_current(),
    }


def apply_controller_params(update: dict):
    ctrl = config.CONTROLLER
    if update.get("battery_capacity") is not None:
        ctrl.set_battery_capacity(update["battery_capacity"])
    if update.get("temperature_compensation_coefficient") is not None:
        ctrl.set_temperature_compensation_coefficient(
            update["temperature_compensation_coefficient"]
        )
    if update.get("voltage_controls") is not None:
        ctrl.set_battery_voltage_control_registers_dict(update["voltage_controls"])
