"""Solar charge controller I/O: live data polling, power profiles, parameter r/w."""

import glob
import os
import subprocess
import time
from datetime import datetime
from typing import Optional

import config

# ── CPU power draw (Intel RAPL) ────────────────────────────────────────────────
# Replaces the old POWER_DRAW_SCRIPT_ADDR external shell script (power_draw.sh).
# That script read /sys/devices/virtual/powercap/*/energy_uj counters, summed the
# deltas between two ~1-second-apart samples, and printed a wattage. The same
# logic now lives here directly — no separate script file to install or maintain.
#
# Domain filtering mirrors the original script exactly:
#   - skip any path containing "intel-rapl-mmio" (duplicate of "intel-rapl")
#   - skip any domain whose name contains "core" (covers both "core" and "uncore")
#     or "psys" — both are already summed into their parent "package" reading
#
# Power is computed as the energy delta between this call and the previous one,
# divided by the *actual* elapsed time (via time.monotonic()), rather than
# assuming a fixed ~1s gap like the original script did — this stays accurate
# regardless of READ_INTERVAL or how long a given poll tick took.
#
# energy_uj is root-only on most distros. A direct read is tried first (works if
# the operator has set up a udev rule making it world-readable); if that raises
# PermissionError, falls back to one batched `sudo -n cat <path...>` call. See
# docs/DEPLOYMENT.md for the required sudoers entry. If RAPL isn't present at
# all (non-Intel hardware, containers, this dev machine, etc.), CPUPowerDraw is
# simply 0.0 — same fallback behaviour as the old script being absent.
_power_cap_paths:       Optional[list] = None   # None = not yet scanned
_power_last_energy_uj:  Optional[int]  = None
_power_last_time:       Optional[float] = None


def _scan_power_caps() -> list:
    """Find one energy_uj path per top-level RAPL domain, e.g. 'package-0'."""
    paths = []
    for name_path in glob.glob("/sys/devices/virtual/powercap/**/name", recursive=True):
        if "intel-rapl-mmio" in name_path:
            continue
        try:
            with open(name_path) as f:
                domain = f.read().strip()
        except OSError:
            continue
        if "core" in domain or "psys" in domain:
            continue
        energy_path = os.path.join(os.path.dirname(name_path), "energy_uj")
        if os.path.isfile(energy_path):
            paths.append(energy_path)
    return paths


def _read_energy_values(paths: list) -> Optional[list]:
    """Read raw energy_uj counters — direct read first, then a batched sudo fallback."""
    try:
        return [int(open(p).read().strip()) for p in paths]
    except (OSError, ValueError):
        pass
    try:
        result = subprocess.run(
            ["sudo", "-n", "cat", *paths], capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None
        lines = result.stdout.strip().splitlines()
        if len(lines) != len(paths):
            return None
        return [int(v) for v in lines]
    except Exception:
        return None


def _read_cpu_power_draw() -> float:
    """Instantaneous CPU package power (W), or 0.0 if RAPL is unavailable/unreadable."""
    global _power_cap_paths, _power_last_energy_uj, _power_last_time

    if _power_cap_paths is None:
        _power_cap_paths = _scan_power_caps()
    if not _power_cap_paths:
        return 0.0

    now = time.monotonic()
    values = _read_energy_values(_power_cap_paths)
    if values is None:
        return 0.0

    total_uj = sum(values)
    power = 0.0
    if _power_last_energy_uj is not None and _power_last_time is not None:
        delta_uj = total_uj - _power_last_energy_uj
        delta_s  = now - _power_last_time
        # A negative delta means a counter wrapped or was reset between reads —
        # skip this tick's reading rather than report a nonsensical value.
        if delta_uj >= 0 and delta_s > 0:
            power = (delta_uj / 1_000_000.0) / delta_s

    _power_last_energy_uj = total_uj
    _power_last_time = now
    return round(power, 3)


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
    cpu_power = _read_cpu_power_draw()

    return {
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
