"""SQLite persistence and application logging."""

import asyncio
import os
import sqlite3
from datetime import datetime

import config


# ── Logging ───────────────────────────────────────────────────────────────────

async def sunblock_log(message: str):
    line = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + ": " + message + "\n"
    await asyncio.to_thread(_write_log, line)

def _write_log(line: str):
    with open(config.POWER_LOGS_FILE, 'a') as f:
        f.write(line)


# ── Database ──────────────────────────────────────────────────────────────────

def check_db():
    if config.DB_CONNECTION is not None and config.DB_CURSOR is not None:
        return
    create_table = not os.path.isfile(config.DB_NAME)
    config.DB_CONNECTION = sqlite3.connect(config.DB_NAME, check_same_thread=False)
    config.DB_CURSOR = config.DB_CONNECTION.cursor()
    if create_table:
        config.DB_CURSOR.execute(
            "CREATE TABLE solardata("
            "Timestamp text, PVVoltage real, PVCurrent real, PVPower real, "
            "BattVoltage real, BattTemperature real, BattChargePower real, "
            "LoadPower real, BattPercentage int, BattOverallCurrent real, "
            "CPUPowerDraw real, PowerProfile text)"
        )


def write_db():
    placeholders = ", ".join(["?"] * len(config.SOLAR_DATA))
    config.DB_CURSOR.execute(
        f"INSERT INTO {config.DB_TABLE_NAME} VALUES ({placeholders})",
        list(config.SOLAR_DATA.values())
    )
    config.DB_CONNECTION.commit()


# ── Persistent settings store ─────────────────────────────────────────────────

def _settings_conn() -> sqlite3.Connection:
    """Open the settings DB and ensure the table exists."""
    conn = sqlite3.connect(config.SETTINGS_DB_NAME)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"
    )
    conn.commit()
    return conn


def load_settings():
    """
    Read persisted settings and apply them to config.
    Called once at startup — persisted values take precedence over .env defaults.
    """
    conn = _settings_conn()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    finally:
        conn.close()

    _apply = {
        "read_interval":      lambda v: setattr(config, "READ_INTERVAL",       int(v)),
        "data_man":           lambda v: setattr(config, "DATA_MAN",            v == "true"),
        "sim_mode":           lambda v: setattr(config, "SIM_MODE",            v == "true"),
        "token_expire_hours": lambda v: setattr(config, "TOKEN_EXPIRE_HOURS",  int(v)),
        "admin_password_hash":lambda v: setattr(config, "ADMIN_PASSWORD_HASH", v),
    }
    for key, value in rows:
        if key in _apply:
            _apply[key](value)


def save_setting(key: str, value) -> None:
    """Upsert a single setting. Booleans serialised as 'true'/'false'."""
    serialised = ("true" if value else "false") if isinstance(value, bool) else str(value)
    conn = _settings_conn()
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, serialised),
        )
        conn.commit()
    finally:
        conn.close()


def delete_setting(key: str) -> None:
    """Remove a persisted setting so the .env default takes effect on next load."""
    conn = _settings_conn()
    try:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()
