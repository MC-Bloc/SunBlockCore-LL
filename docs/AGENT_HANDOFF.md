# SunBlockCore-LL — Agent Handoff Document

> This document is written for a Claude agent (or any developer) who is picking up this project cold.  
> Read this before touching any code.

---

## What This Project Is

SunBlockCore-LL is a real-time solar energy monitoring and admin panel for the SunBlock Project (TAG MC-Bloc, Milieux Institute, Concordia University, Montreal). It:

- Reads telemetry from an Epever MPPT charge controller over RS-485/Modbus
- Persists readings to SQLite at 1 reading/second
- Broadcasts live data to browser clients via Socket.IO
- Serves a single-page Alpine.js admin panel with 6 tabs: Live, Parameters, Energy, Settings, History, Visualize
- Has a simulator mode (for development without hardware) that generates realistic data from 1.28M rows of real Montreal solar data

**Repository:** the root of this repo (wherever you cloned it)

---

## File Map — Read These First

```
config.py       ← START HERE. All env vars + all shared mutable state.
                   Every module reads/writes state through this module.
auth.py         ← JWT, bcrypt, Pydantic models (SettingsUpdate, PasswordChange, etc.)
db.py           ← SQLite: logging, admin audit log, telemetry r/w, settings persistence,
                   history & visualize queries, path validation (_validate_data_directory)
hardware.py     ← Epever Modbus: parse_data(), read_controller_*(), apply_params()
simulator.py    ← _SimState: synthetic data from real baselines
sunblock.py     ← ASGI app, lifespan, ALL routes, Socket.IO events
templates/
  index.html    ← Entire frontend: Alpine.js SPA, 6 tabs, all CSS, all JS
  404.html      ← Custom 404 page (dark theme, matching the app)
public/vendor/  ← alpine.min.js, socket.io.min.js, plotly.min.js
scripts/
  deploy.sh     ← Ubuntu/systemd deployment automation
  vendor.sh     ← Downloads and pins all frontend vendor assets
  gen_password_hash.py
```

No other Python files contain business logic. No build step. No TypeScript. No React.

---

## How to Run

```bash
# With simulator (no hardware needed):
SIM_MODE=true DATA_DIRECTORY=./demo_data/ \
  .venv/bin/uvicorn sunblock:socket_app --host 0.0.0.0 --port 3707

# With hardware:
.venv/bin/uvicorn sunblock:socket_app --host 0.0.0.0 --port 3707
```

The virtualenv is at `.venv/`. Python version is **3.9** — do not use `str | None` union syntax; use `Optional[str]` from `typing`.

**Public URL:** `http://localhost:3707` — shows live data to unauthenticated visitors.  
**Admin login:** `http://localhost:3707/<ADMIN_PATH>` — secret slug from `.env`; auto-opens the login modal.

---

## The Most Important Pattern

All mutable runtime state lives in `config.py`. Other modules mutate it like this:

```python
import config
config.SIM_MODE = True       # CORRECT — visible to all modules
```

Never do this:

```python
from config import SIM_MODE
SIM_MODE = True              # WRONG — only updates the local binding
```

This pattern is used everywhere. It is not an accident. It ensures that settings changes from the REST API immediately affect the polling loop, the DB write, and any other consumer.

---

## Configuration Priority Chain

```
Code defaults (config.py)
    → overridden by .env (load_dotenv at import)
        → ENV_DEFAULTS snapshot taken here ← this is the reset anchor
    → overridden by sunblock_settings.db (load_settings() in lifespan)
```

`ENV_DEFAULTS` is a dict defined in `config.py` at module level that captures the `.env` values permanently. The `DELETE /api/settings/{key}` endpoint uses this to restore the live config without restarting.

---

## Database & Log Files

All files live in `DATA_DIRECTORY` (required — must be set in `.env`, no default):

| File | Purpose |
|---|---|
| `SunBlockCore-LL.db` | Solar telemetry — `solardata` table (12 columns) |
| `sunblock_settings.db` | Runtime settings — `settings(key TEXT PK, value TEXT)` and `api_tokens` (hashed bearer tokens for external API access) |
| `SunBlockCoreLogs.txt` | Application log (startup, errors, polling events) |
| `SunBlockAdminAudit.txt` | Admin audit log — every write action with timestamp + IP |

Exception: `sunblock_settings.db` may also live next to the source files before `DATA_DIRECTORY` is configured (see `config.py` for the bootstrapping logic).

**`api_tokens` schema** (in `sunblock_settings.db`, created lazily by `db._tokens_conn()`):
```sql
CREATE TABLE api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,   -- SHA-256 of the raw token; raw value never stored
    created_at TEXT NOT NULL,
    expires_at TEXT,                   -- NULL = never expires
    last_used_at TEXT
);
```

**Telemetry schema:**
```sql
CREATE TABLE solardata (
    Timestamp TEXT, PVVoltage REAL, PVCurrent REAL, PVPower REAL,
    BattVoltage REAL, BattTemperature REAL, BattChargePower REAL,
    LoadPower REAL, BattPercentage INT, BattOverallCurrent REAL,
    CPUPowerDraw REAL, PowerProfile TEXT
);
```

**Settings keys:** `read_interval`, `data_man`, `sim_mode`, `token_expire_hours`, `admin_password_hash`

---

## API Quick Reference

| Method | Path | Auth | What it does |
|---|---|---|---|
| GET | `/` | No | Public live view (data cards only) |
| GET | `/<ADMIN_PATH>` | No | Admin entry-point; auto-opens login modal |
| GET | `/api/data` | No | Current live reading |
| GET | `/api/data/history` | **Yes** (60/min) | Paginated history |
| GET | `/api/data/visualize/fields` | **Yes** (60/min) | Field metadata |
| GET | `/api/data/visualize` | **Yes** (20/min) | Time-series data for Visualize tab |
| GET | `/api/data/download` | **Yes** | SQLite download |
| GET | `/api/data/download/csv` | **Yes** | CSV export |
| GET | `/api/data/download/xlsx` | **Yes** | XLSX export |
| GET/PATCH | `/api/settings` | **Yes** | Get/update runtime settings |
| DELETE | `/api/settings/{key}` | **Yes** | Reset setting to .env value |
| POST | `/api/settings/password` | Session only | Change admin password |
| POST | `/api/tokens` | Session only | Generate an API bearer token (raw value shown once) |
| GET | `/api/tokens` | Session only | List API tokens (metadata only) |
| DELETE | `/api/tokens/{id}` | Session only | Revoke an API token |
| GET | `/api/power-profile` | Controller* | Current power profile |
| GET | `/api/controller/parameters` | Controller* | Battery/charge config |
| PUT | `/api/controller/parameters` | **Yes**+HW | Write battery/charge config |
| GET | `/api/controller/stats` | Controller* | Energy stats |
| GET | `/api/controller/status` | Controller* | Status + RTC |
| POST | `/api/controller/rtc/sync` | **Yes**+HW | Sync RTC |
| POST | `/api/performance-mode` etc. | **Yes**+HW | Power profiles |
| POST | `/api/login` | Rate-limited (5/min) | Set JWT cookie |
| POST | `/api/logout` | No | Clear cookie |

**Yes** = `Depends(verify_session_or_token)` — accepts either the JWT session cookie or an `Authorization: Bearer <sbll_...>` API token (Settings → API Tokens; see `auth.verify_session_or_token`).
**Session only** = `Depends(verify_session)` — cookie required, bearer tokens deliberately rejected (password change and token management itself; prevents a leaked token from escalating to full account takeover).
*Controller = requires controller OR sim mode active  
Rate limits are per IP, enforced by slowapi.

---

## Frontend Architecture

Single Jinja2 template `templates/index.html`. No build step.

- **Alpine.js** for reactivity (vendored at `public/vendor/alpine.min.js`)
- **Socket.IO** for live data (vendored at `public/vendor/socket.io.min.js`)
- **Plotly.js** basic bundle for all charts (vendored at `public/vendor/plotly.min.js`)

The Alpine component is bootstrapped with server-side values:

```html
<div x-data='sunblock(
  {{ is_authenticated | tojson }},
  {{ sim_mode         | tojson }},
  {{ env_defaults     | tojson }},
  {{ admin_mode       | tojson }},
  {{ viz_fields       | tojson }}
)'>
```

**Critical:** The `x-data` attribute uses single quotes. JSON from `tojson` contains double quotes — if the attribute used double quotes, the JSON would break HTML attribute parsing. Do not change the quoting.

**Access tiers:** Unauthenticated visitors see only the Live tab (data cards + charts). All other tabs are `<template x-if="authed">` — not rendered until login. The `switchTab()` method also enforces this client-side as a UX guard.

**CSP nonce:** Every HTML response gets a unique nonce in both the `Content-Security-Policy` header and the inline `<script nonce="...">` tag. This blocks injected scripts while allowing the Alpine bootstrap.

Tab panels use `x-show` (not `x-if`) — DOM is retained between switches so charts don't need re-initialisation.

---

## Security Architecture (Brief)

- Admin login path is a random slug (`ADMIN_PATH` in `.env`) — never in logs, never in Referer
- Per-request CSP nonce — blocks injected scripts
- Rate limiting on login (5/min) and data endpoints (20–60/min)
- `data_directory` validated with `_validate_data_directory()` — resolves symlinks, rejects system paths
- All data endpoints (history, visualize, downloads) require auth
- API bearer tokens (`auth.verify_session_or_token`) let external scripts authenticate without a session cookie — generated/revoked from Settings → API Tokens, hashed with SHA-256 in `api_tokens` (never stored raw), with configurable expiry
- Every write action logged to `SunBlockAdminAudit.txt` with IP

See `docs/SECURITY.md` for the full security model.

---

## Admin Audit Log

`admin_log(action, detail, ip)` in `db.py` writes to `SunBlockAdminAudit.txt`. Call it after every admin action. Format:

```
2026-06-06 10:30:00  [AUDIT]  LOGIN  user=admin  ip=192.168.1.10
```

Falls back to stderr if `DATA_DIRECTORY` is not yet set.

---

## Concurrency Rules

1. **Blocking calls** (Modbus I/O, SQLite writes, history queries) must go through `run_in_executor`. Never `await` a blocking call directly in a route handler.

2. **History and visualize queries** open their own `sqlite3.connect()` per call. They must never use `config.DB_CURSOR` — that cursor is owned by the polling loop.

3. **State mutations** from route handlers (e.g. `config.SIM_MODE = True`) are safe because CPython's GIL protects simple attribute assignments.

---

## Known Issues to Fix (Priority Order)

### High
- **SEC-001** — Default `SECRET_KEY=changeme-secret-key` must be replaced before production. Server warns at startup. See `docs/SECURITY.md`.

### Medium
- **BUG-002** — No index on `solardata.Timestamp`. Date-range queries do full table scans above ~5M rows. Fix: `CREATE INDEX IF NOT EXISTS idx_solardata_ts ON solardata(Timestamp);` in `check_db()`.

### Low
- **BUG-001** — SQLite concurrent write contention. Add `PRAGMA journal_mode=WAL` after opening `DB_CONNECTION` in `check_db()`.

---

## Feature Backlog (Not Yet Implemented)

1. **Alert thresholds** — Notify (email/webhook) when battery below X%, temperature above Y°C, etc.
2. **Multi-controller support** — Currently exactly one controller.
3. **WAL mode for SQLite** — See BUG-001.
4. **Timestamp index** — See BUG-002.
5. **Data pruning / retention policy** — The telemetry DB grows unbounded. A configurable rolling window would be useful.
6. **Dark/light theme toggle** — Currently hardcoded dark theme.
7. **Multiple admin users** — Currently one hardcoded username/password.
8. **Grafana / InfluxDB export** — Line-protocol endpoint for external dashboards.

---

## Files You Should NOT Modify Without Understanding

| File | Why it's sensitive |
|---|---|
| `config.py` | Adding imports here can change module-load order and break the `ENV_DEFAULTS` snapshot timing |
| `db.py` — `load_settings()` | Boolean parsing uses `v == "true"` (lowercase). Changing serialisation in `save_setting()` must match. |
| `db.py` — `_validate_data_directory()` | Blocklist must cover all platform-specific system paths. Symlink resolution via `os.path.realpath()` is load-bearing. |
| `templates/index.html` — `x-data` quoting | Single quotes on `x-data` are load-bearing. See above. |
| `.env` | Contains `SECRET_KEY`, `ADMIN_PASSWORD_HASH`, and `ADMIN_PATH`. Never commit. |

---

## Testing Approach

There is currently no automated test suite. Manual testing procedure:

1. Start with `SIM_MODE=true DATA_DIRECTORY=./demo_data/`
2. Open `http://localhost:3707` in a browser — verify live data, data cards, and charts visible without login
3. Navigate to `http://localhost:3707/<ADMIN_PATH>` — verify login modal appears
4. Log in; verify all 6 tabs become accessible
5. **History:** load, filter by date, paginate
6. **Visualize:** select variables, set date range, click Plot — verify chart renders
7. **Settings:** change a setting (e.g. `read_interval=2`), restart server, verify it persisted; reset it, verify it returned to `.env` value
8. **Downloads:** CSV, XLSX, SQLite — verify files are downloaded
9. **Audit log:** check `demo_data/SunBlockAdminAudit.txt` — verify login, settings change, download are all recorded
10. Try setting `data_directory` to `/etc` — verify it returns a 400 and the PATH_REJECTED entry appears in the audit log

---

## Deployment

Production runs on Ubuntu at port 3707 via systemd. See `scripts/deploy.sh`.

```
ExecStart=.venv/bin/uvicorn sunblock:socket_app --host 0.0.0.0 --port 3707
```

After changing `.env`, restart the service:
```bash
sudo systemctl restart sunblock
```

Settings in `sunblock_settings.db` take precedence over `.env` at runtime (see priority chain above).

---

## Quick Diagnostic Commands

```bash
# Check what the server logged on startup
tail -f demo_data/SunBlockCoreLogs.txt

# Check the admin audit log
tail -f demo_data/SunBlockAdminAudit.txt

# Check current settings in DB
sqlite3 demo_data/sunblock_settings.db "SELECT * FROM settings;"

# Check row count in telemetry DB
sqlite3 demo_data/SunBlockCore-LL.db "SELECT COUNT(*) FROM solardata;"

# Tail last 5 readings
sqlite3 demo_data/SunBlockCore-LL.db \
  "SELECT * FROM solardata ORDER BY Timestamp DESC LIMIT 5;"

# Test live endpoint
curl http://localhost:3707/api/data

# Test that history requires auth (should return 401)
curl http://localhost:3707/api/data/history

# Test custom 404
curl http://localhost:3707/nonexistent-page
```
