"""Solar charge controller I/O: live data polling, power profiles, parameter r/w."""

import subprocess
import threading
import time
from datetime import datetime

import config

# ── Controller lock ───────────────────────────────────────────────────────────
# The Epever controller is a single RS-485/Modbus RTU device on one serial port.
# minimalmodbus/pyserial are not thread-safe for concurrent calls on the same
# instrument — if two threads write to the port at the same time (e.g. the
# polling loop's parse_data() and a Parameters-tab fetch landing at the same
# instant), the request bytes interleave on the wire. Each individual get_*()
# call has a 1s read timeout (set in EpeverChargeController.__init__), but a
# corrupted RS-485 conversation can leave the USB-serial adapter itself stuck
# in a bad half-duplex state that doesn't self-recover — symptom: the whole
# polling loop hangs and stops broadcasting solar_data until the service is
# restarted. _controller_lock makes every controller I/O call here mutually
# exclusive, regardless of which thread/request it came from.
_controller_lock = threading.Lock()

# ── Controller data caches ────────────────────────────────────────────────────
# Without this, every Parameters/Energy tab load (and every full page reload)
# triggers a fresh round of 9-12 sequential Modbus reads, even though none of
# this data changes meaningfully faster than these TTLs. apply_controller_params()
# writes through to _params_cache immediately so a save is reflected without
# waiting out the TTL.
_PARAMS_CACHE_TTL = 60.0
_STATS_CACHE_TTL  = 30.0
_STATUS_CACHE_TTL = 15.0
_params_cache: dict = {}
_params_cache_at: float = 0.0
_stats_cache: dict = {}
_stats_cache_at: float = 0.0
_status_cache: dict = {}
_status_cache_at: float = 0.0

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
    global _cached_profile
    result = subprocess.run(["sudo", "powerprofilesctl", "set", profile])
    if result.returncode != 0:
        raise RuntimeError(f"powerprofilesctl set {profile} failed with code {result.returncode}")
    # Force a fresh read so the cache reflects the change immediately. Clearing
    # _cached_profile itself (rather than just _cached_profile_at = 0.0) matters:
    # time.monotonic() starts near 0 at process startup on some platforms, so
    # resetting only the timestamp can fail to invalidate within the first
    # _PROFILE_CACHE_TTL seconds of uptime — (now - 0.0) can still be < TTL.
    _cached_profile = ""
    return check_power_profile()


# ── Live data ─────────────────────────────────────────────────────────────────

def parse_data() -> dict:
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

    with _controller_lock:
        ctrl = config.CONTROLLER
        data = {
            "Timestamp":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "PVVoltage":          ctrl.get_solar_voltage(),
            "PVCurrent":          ctrl.get_solar_current(),
            "PVPower":            ctrl.get_solar_power(),
            "BattVoltage":        ctrl.get_battery_voltage(),
            "BattTemperature":    ctrl.get_battery_temperature(),
            "BattChargePower":    ctrl.get_battery_power(),
            "LoadPower":          ctrl.get_load_power(),
            "BattPercentage":     ctrl.get_battery_state_of_charge(),
            "BattOverallCurrent": ctrl.get_battery_current(),
            "CPUPowerDraw":       cpu_power,
            "PowerProfile":       check_power_profile(),
        }
    return data


# ── Controller parameters ─────────────────────────────────────────────────────
# All three read_* functions below are cached (see TTLs at top of file) and
# every Modbus call they make is serialized through _controller_lock, same as
# parse_data() — see the comment on _controller_lock for why both matter.

def read_controller_params() -> dict:
    global _params_cache, _params_cache_at
    now = time.monotonic()
    if _params_cache and (now - _params_cache_at) < _PARAMS_CACHE_TTL:
        return _params_cache
    with _controller_lock:
        ctrl = config.CONTROLLER
        _params_cache = {
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
        _params_cache_at = now
    return _params_cache


def read_controller_stats() -> dict:
    global _stats_cache, _stats_cache_at
    now = time.monotonic()
    if _stats_cache and (now - _stats_cache_at) < _STATS_CACHE_TTL:
        return _stats_cache
    with _controller_lock:
        ctrl = config.CONTROLLER
        _stats_cache = {
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
        _stats_cache_at = now
    return _stats_cache


def read_controller_status() -> dict:
    global _status_cache, _status_cache_at
    now = time.monotonic()
    if _status_cache and (now - _status_cache_at) < _STATUS_CACHE_TTL:
        return _status_cache
    with _controller_lock:
        ctrl = config.CONTROLLER
        _status_cache = {
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
        _status_cache_at = now
    return _status_cache


def apply_controller_params(update: dict):
    with _controller_lock:
        ctrl = config.CONTROLLER
        if update.get("battery_capacity") is not None:
            ctrl.set_battery_capacity(update["battery_capacity"])
        if update.get("temperature_compensation_coefficient") is not None:
            ctrl.set_temperature_compensation_coefficient(
                update["temperature_compensation_coefficient"]
            )
        if update.get("voltage_controls") is not None:
            ctrl.set_battery_voltage_control_registers_dict(update["voltage_controls"])
    # Force read_controller_params()'s next call to do a fresh read rather than
    # serve the now-stale cached values. Clearing the cache dict itself (not
    # just _params_cache_at = 0.0) matters: time.monotonic() can start near 0
    # at process startup, so resetting only the timestamp can fail to
    # invalidate within the first _PARAMS_CACHE_TTL seconds of uptime.
    global _params_cache
    _params_cache = {}


def sync_rtc():
    """Sync the controller's RTC to the server's current time."""
    with _controller_lock:
        config.CONTROLLER.set_rtc(datetime.now())
    # status cache includes 'rtc' — invalidate (see apply_controller_params()
    # above for why clearing the dict, not just the timestamp, is required).
    global _status_cache
    _status_cache = {}
