"""
Centralised config constants and shared mutable runtime state.

All other modules do `import config` and reference state as `config.X`
so that assignments (config.SOLAR_DATA = new_dict, config.CONTROLLER = ...)
are visible everywhere without re-importing.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Hardware ──────────────────────────────────────────────────────────────────
CONTROLLER_PORT  = os.getenv("CONTROLLER_PORT",  "/dev/ttyACM0")
CONTROLLER_SLAVE = int(os.getenv("CONTROLLER_SLAVE", 1))

# ── Paths ���────────────────────────────────────────────────────────────────────
DATA_DIRECTORY    = os.getenv("DATA_DIRECTORY")
POWER_DRAW_SCRIPT = os.getenv("POWER_DRAW_SCRIPT_ADDR")

if not DATA_DIRECTORY:
    raise ValueError(
        "DATA_DIRECTORY is not set. "
        "Add DATA_DIRECTORY=/path/to/your/data to your .env file."
    )

POWER_LOGS_FILE  = os.path.join(DATA_DIRECTORY, "SunBlockCoreLogs.txt")
DB_NAME          = os.path.join(DATA_DIRECTORY, "SunBlockCore-LL.db")
DB_TABLE_NAME    = "solardata"
SETTINGS_DB_NAME = os.path.join(DATA_DIRECTORY, "sunblock_settings.db")

# ── Data collection ───────────────────────────────────────────────────────────
DATA_MAN      = os.getenv("DATA_MAN", "true").lower() == "true"
READ_INTERVAL = int(os.getenv("READ_INTERVAL", 1))

# ── Auth ──────────────────────────────────────────────────────────────────────
ADMIN_USERNAME      = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
SECRET_KEY          = os.getenv("SECRET_KEY", "changeme-secret-key")
TOKEN_EXPIRE_HOURS  = int(os.getenv("TOKEN_EXPIRE_HOURS", 24))
SECURE_COOKIES      = os.getenv("SECURE_COOKIES", "false").lower() == "true"

# ── Mode ──────────────────────────────────────────────────────────────────────
SIM_MODE = os.getenv("SIM_MODE", "false").lower() == "true"

# ── Env-value snapshot ───────────────────────────────────────────────────────
# Captured here, at import time, before load_settings() can overwrite anything.
# Used by the reset-to-env endpoint so the original .env values are never lost.
ENV_DEFAULTS = {
    "read_interval":      READ_INTERVAL,
    "data_man":           DATA_MAN,
    "sim_mode":           SIM_MODE,
    "token_expire_hours": TOKEN_EXPIRE_HOURS,
}

# ── Mutable runtime state ─────────────────────────────────────────────────────
# Mutated by lifespan (sunblock.py) and polling_loop. Never import these
# symbols directly — always read through the module so mutations are visible.

CONTROLLER        = None   # EpeverChargeController instance, set in lifespan
ACTIVE_USERS      = 0
ACTIVE_USERS_LOCK = None   # asyncio.Lock, created in lifespan
POLLING_ACTIVE    = False
POLLING_TASK      = None

DB_CONNECTION = None
DB_CURSOR     = None

SOLAR_DATA = {
    "Timestamp":          "",
    "PVVoltage":          0,
    "PVCurrent":          0,
    "PVPower":            0,
    "BattVoltage":        0,
    "BattTemperature":    0,
    "BattChargePower":    0,
    "LoadPower":          0,
    "BattPercentage":     0,
    "BattOverallCurrent": 0,
    "CPUPowerDraw":       0,
    "PowerProfile":       "",
}
