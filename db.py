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


# ── Admin audit logging ───────────────────────────────────────────────────────

async def admin_log(action: str, detail: str = "", ip: str = "") -> None:
    """
    Append a structured audit entry to SunBlockAdminAudit.txt.

    Each line is tab-separated for easy grep / awk parsing::

        2026-06-06 10:30:00  [AUDIT]  LOGIN  user=admin  ip=192.168.1.10

    Falls back to stderr when DATA_DIRECTORY is not yet configured so that
    early login/logout events are never silently dropped.
    """
    parts = [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "[AUDIT]", action]
    if detail:
        parts.append(detail)
    if ip:
        parts.append(f"ip={ip}")
    line = "  ".join(parts) + "\n"
    await asyncio.to_thread(_write_audit_log, line)


def _write_audit_log(line: str) -> None:
    if not config.ADMIN_AUDIT_FILE:
        import sys
        print(line, end="", file=sys.stderr)
        return
    with open(config.ADMIN_AUDIT_FILE, "a") as f:
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


# ── Path validation ───────────────────────────────────────────────────────────

# Directories that must never be used as a DATA_DIRECTORY.
# Checked against the *resolved* (symlink-expanded) absolute path so that
# crafted symlink chains cannot bypass the check.
_BLOCKED_PREFIXES = (
    # Linux system paths
    "/etc", "/bin", "/sbin",
    "/usr/bin", "/usr/sbin", "/usr/local/bin", "/usr/local/sbin",
    "/sys", "/proc", "/dev", "/run", "/boot",
    "/root", "/lib", "/lib64", "/lib32",
    # macOS system paths (relevant during local development)
    "/System", "/Library",
    "/private/etc", "/private/var/db", "/private/var/root",
)

def _validate_data_directory(path: str) -> None:
    """
    Raise ValueError if *path* resolves to a system-critical location.

    Resolves symlinks and ``..`` traversal before checking so that crafted
    paths like ``/home/pi/../../etc/`` are caught correctly.
    """
    resolved = os.path.realpath(os.path.abspath(path))

    # Flat-out refuse the filesystem root.
    if resolved == "/":
        raise ValueError("Refusing to use filesystem root '/' as DATA_DIRECTORY.")

    for blocked in _BLOCKED_PREFIXES:
        if resolved == blocked or resolved.startswith(blocked + os.sep):
            raise ValueError(
                f"Path '{resolved}' is inside the protected system directory "
                f"'{blocked}'. Choose a path inside your home directory or a "
                "dedicated data mount."
            )


def apply_data_directory(path: str) -> None:
    """
    Set DATA_DIRECTORY and recompute all derived paths.
    Creates the directory if it does not exist.
    Called from load_settings() on startup and from the PATCH /api/settings
    route when the admin sets the directory for the first time via the panel.

    Raises ValueError if the resolved path points at a protected system directory.
    """
    path = path.rstrip("/\\") + os.sep   # normalise: always ends with separator
    _validate_data_directory(path)
    os.makedirs(path, exist_ok=True)
    config.DATA_DIRECTORY    = path
    config.POWER_LOGS_FILE   = os.path.join(path, "SunBlockCoreLogs.txt")
    config.DB_NAME           = os.path.join(path, "SunBlockCore-LL.db")
    config.ADMIN_AUDIT_FILE  = os.path.join(path, "SunBlockAdminAudit.txt")


# ── Visualisation queries ─────────────────────────────────────────────────────

# All plottable numeric fields and their display metadata
VIZ_FIELD_META: dict = {
    "PVVoltage":          {"label": "PV Voltage",           "unit": "V"},
    "PVCurrent":          {"label": "PV Current",           "unit": "A"},
    "PVPower":            {"label": "PV Power",             "unit": "W"},
    "BattVoltage":        {"label": "Battery Voltage",      "unit": "V"},
    "BattTemperature":    {"label": "Battery Temperature",  "unit": "°C"},
    "BattChargePower":    {"label": "Battery Charge Power", "unit": "W"},
    "LoadPower":          {"label": "Load Power",           "unit": "W"},
    "BattPercentage":     {"label": "Battery %",            "unit": "%"},
    "BattOverallCurrent": {"label": "Battery Current",      "unit": "A"},
    "CPUPowerDraw":       {"label": "CPU Power Draw",       "unit": "W"},
}

# Fields known to produce occasional hardware spikes; values above these
# thresholds are replaced with the most recent valid reading (forward-fill).
_SPIKE_THRESHOLDS: dict = {
    "PVVoltage":  25.0,
    "PVCurrent":  5.0,
    "PVPower":    100.0,
    "BattVoltage": 15.0,
}


def _filter_spikes(values: list, field: str) -> list:
    threshold = _SPIKE_THRESHOLDS.get(field)
    if threshold is None:
        return values
    result, last_valid = [], None
    for v in values:
        if v is not None and v > threshold and last_valid is not None:
            result.append(last_valid)
        else:
            result.append(v)
            if v is not None:
                last_valid = v
    return result


def _moving_average(values: list, k: int) -> list:
    if k <= 1:
        return values
    result, window = [], []
    for v in values:
        if v is None:
            result.append(None)
            continue
        window.append(v)
        if len(window) > k:
            window.pop(0)
        result.append(sum(window) / len(window))
    return result


def query_visualize(
    fields:        list,
    from_ts:       Optional[str] = None,
    to_ts:         Optional[str] = None,
    sample:        int  = 1,      # keep every Nth row (matches 1 Hz write rate)
    smooth:        int  = 0,      # moving-average window in samples (0 = off)
    filter_spikes: bool = True,
) -> dict:
    """
    Return time-series data ready for Plotly.

    Processing pipeline (matches SunBlock_DataProcessing.ipynb):
      1. Date-range filter via SQL WHERE
      2. Row-based resampling  — take every ``sample``th row
      3. Spike filtering       — clamp hardware glitches on noisy fields
      4. Moving-average smooth — window of ``smooth`` samples (optional)

    Returns::

        {
          "timestamps": ["2025-05-30 10:00:00", ...],
          "series": {
            "PVPower": {"values": [...], "label": "PV Power", "unit": "W"},
            ...
          },
          "total_rows":   86400,
          "sampled_rows": 17280,
        }
    """
    empty: dict = {
        "timestamps": [], "series": {},
        "total_rows": 0, "sampled_rows": 0,
    }
    if not config.DB_NAME or not os.path.isfile(config.DB_NAME):
        return empty

    # Sanitise — only allow known numeric fields
    valid = [f for f in fields if f in VIZ_FIELD_META]
    if not valid:
        return empty

    col_sql = "Timestamp, " + ", ".join(valid)
    where_parts: list = []
    params:      list = []
    if from_ts:
        where_parts.append("Timestamp >= ?")
        params.append(from_ts)
    if to_ts:
        where_parts.append("Timestamp <= ?")
        params.append(to_ts + " 23:59:59" if len(to_ts) == 10 else to_ts)

    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    # Hard cap: fetch at most 5 M rows before resampling to avoid OOM on large DBs.
    # After resampling (step ≥ 1) the caller receives at most 5_000_000 / step points.
    _ROW_CAP = 5_000_000

    conn = sqlite3.connect(config.DB_NAME, check_same_thread=False)
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {col_sql} FROM {config.DB_TABLE_NAME} {where}"
            f" ORDER BY Timestamp ASC LIMIT {_ROW_CAP}",
            params,
        )
        rows = cur.fetchall()
    except Exception:
        return empty
    finally:
        conn.close()

    total_rows = len(rows)

    # Resample
    step = max(1, sample)
    rows = rows[::step]
    sampled_rows = len(rows)

    if not rows:
        return {
            "timestamps": [],
            "series": {
                f: {"values": [], **VIZ_FIELD_META[f]} for f in valid
            },
            "total_rows": total_rows,
            "sampled_rows": 0,
        }

    timestamps = [row[0] for row in rows]

    series: dict = {}
    for col_idx, field in enumerate(valid, 1):
        # Parse to float; keep None for missing
        values: list = []
        for row in rows:
            v = row[col_idx]
            try:
                values.append(float(v) if v is not None else None)
            except (TypeError, ValueError):
                values.append(None)

        if filter_spikes:
            values = _filter_spikes(values, field)
        if smooth > 1:
            values = _moving_average(values, smooth)

        series[field] = {"values": values, **VIZ_FIELD_META[field]}

    return {
        "timestamps":   timestamps,
        "series":       series,
        "total_rows":   total_rows,
        "sampled_rows": sampled_rows,
    }


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
