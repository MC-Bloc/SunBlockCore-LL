"""SQLite persistence, application logging, and history queries."""

import asyncio
import os
import sqlite3
from datetime import datetime
from typing import Optional

import config


# ── Logging ───────────────────────────────────────────────────────────────────

async def sunblock_log(message: str):
    line = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + ": " + message + "\n"
    await asyncio.to_thread(_write_log, line)

def _write_log(line: str):
    if not config.POWER_LOGS_FILE:
        # DATA_DIRECTORY not configured yet — echo to stderr so nothing is lost.
        import sys
        print(line, end='', file=sys.stderr)
        return
    with open(config.POWER_LOGS_FILE, 'a') as f:
        f.write(line)


# ── Database ──────────────────────────────────────────────────────────────────

def check_db():
    if config.DB_NAME is None:
        return   # DATA_DIRECTORY not set yet
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
        "data_directory":     lambda v: apply_data_directory(v),
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


def apply_data_directory(path: str) -> None:
    """
    Set DATA_DIRECTORY and recompute all derived paths.
    Creates the directory if it does not exist.
    Called from load_settings() on startup and from the PATCH /api/settings
    route when the admin sets the directory for the first time via the panel.
    """
    path = path.rstrip("/\\") + os.sep   # normalise: always ends with separator
    os.makedirs(path, exist_ok=True)
    config.DATA_DIRECTORY  = path
    config.POWER_LOGS_FILE = os.path.join(path, "SunBlockCoreLogs.txt")
    config.DB_NAME         = os.path.join(path, "SunBlockCore-LL.db")


# ── History queries ───────────────────────────────────────────────────────────

def query_history(
    limit:   int = 100,
    offset:  int = 0,
    from_ts: Optional[str] = None,
    to_ts:   Optional[str] = None,
    order:   str = "desc",
) -> dict:
    """
    Return a paginated slice of the solardata table.

    Opens its own connection so it never contends with the write cursor
    that is held by the polling loop.

    Returns {"rows": [...], "total": N, "limit": N, "offset": N}.
    If the database file does not exist yet (DATA_MAN=false or first run)
    the empty payload is returned instead of raising.
    """
    empty = {"rows": [], "total": 0, "limit": limit, "offset": offset}
    if not config.DB_NAME or not os.path.isfile(config.DB_NAME):
        return empty

    limit  = max(1, min(1000, limit))
    offset = max(0, offset)

    where_parts: list[str] = []
    params: list = []
    if from_ts:
        where_parts.append("Timestamp >= ?")
        params.append(from_ts)
    if to_ts:
        # If caller passed just a date (10 chars) extend to end of that day.
        params.append(to_ts + " 23:59:59" if len(to_ts) == 10 else to_ts)
        where_parts.append("Timestamp <= ?")

    where     = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    order_dir = "DESC" if order.lower() != "asc" else "ASC"

    conn = sqlite3.connect(config.DB_NAME, check_same_thread=False)
    conn.row_factory = None  # plain tuples — we'll zip with column names
    try:
        cur   = conn.cursor()
        total = cur.execute(
            f"SELECT COUNT(*) FROM {config.DB_TABLE_NAME} {where}", params
        ).fetchone()[0]

        cur.execute(
            f"SELECT * FROM {config.DB_TABLE_NAME} {where}"
            f" ORDER BY Timestamp {order_dir} LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
    except Exception:
        return empty
    finally:
        conn.close()

    return {
        "rows":   [dict(zip(cols, row)) for row in rows],
        "total":  total,
        "limit":  limit,
        "offset": offset,
    }
