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

    # ── Two bulk Modbus reads instead of 9 individual ones ────────────────────
    # Each individual get_*() call is a separate serial round-trip (timeout=1 s).
    # Two bulk reads cover all the same registers in just 2 round-trips.
    #
    # Bulk 1 — 0x3100–0x311A (27 registers, FC=4):
    #   PV voltage/current/power, battery charge power, load power,
    #   battery temperature, battery state-of-charge.
    r1 = ctrl.retriable_read_registers(0x3100, 27, 4)
    #
    # Bulk 2 — 0x331A–0x331C (3 registers, FC=4):
    #   Battery voltage, battery current (32-bit signed).
    r2 = ctrl.retriable_read_registers(0x331A, 3, 4)

    # The driver uses minimalmodbus BYTEORDER_LITTLE_SWAP for all 32-bit values:
    #   combined = (_swap(hi_reg) << 16) | _swap(lo_reg)
    # where _swap byte-swaps within each 16-bit word and lo_reg is at the lower
    # Modbus address.  Replicate that formula here so batch parsing is identical
    # to what retriable_read_long() produces.
    def _swap(x: int) -> int:
        return ((x & 0xFF) << 8) | ((x >> 8) & 0xFF)

    def _long32(lo: int, hi: int, signed: bool = False) -> float:
        val = (_swap(hi) << 16) | _swap(lo)
        if signed and val > 0x7FFF_FFFF:
            val -= 0x1_0000_0000
        return val / 100

    def _s16(raw: int) -> float:
        """Signed 16-bit register → float (÷100)."""
        if raw > 0x7FFF:
            raw -= 0x10000
        return raw / 100

    # r1 index = register address − 0x3100
    # r2 index = register address − 0x331A
    return {
        "Timestamp":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "PVVoltage":          r1[0]  / 100,                        # 0x3100
        "PVCurrent":          r1[1]  / 100,                        # 0x3101
        "PVPower":            _long32(r1[2],  r1[3]),               # 0x3102–0x3103
        "BattVoltage":        r2[0]  / 100,                        # 0x331A
        "BattTemperature":    _s16(r1[16]),                         # 0x3110
        "BattChargePower":    _long32(r1[6],  r1[7]),               # 0x3106–0x3107
        "BattOverallCurrent": _long32(r2[1],  r2[2], signed=True),  # 0x331B–0x331C
        "BattPercentage":     r1[26],                               # 0x311A (integer %)
        "LoadPower":          _long32(r1[14], r1[15]),              # 0x310E–0x310F
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
