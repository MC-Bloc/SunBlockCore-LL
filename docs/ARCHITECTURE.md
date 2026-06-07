# SunBlockCore-LL — Architecture & Design Decisions

> Written for The SunBlock Project  
> TAG MC-Bloc · Milieux Institute · Concordia University · Montreal, Canada  
> https://github.com/MC-Bloc/SunBlock

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Runtime Architecture](#2-runtime-architecture)
3. [Module Layout](#3-module-layout)
4. [Data Flow](#4-data-flow)
5. [Concurrency Model](#5-concurrency-model)
6. [Persistence Layer](#6-persistence-layer)
7. [Authentication & Security Architecture](#7-authentication--security-architecture)
8. [Simulator Architecture](#8-simulator-architecture)
9. [Frontend Architecture](#9-frontend-architecture)
10. [Key Architecture Decisions & Rationale](#10-key-architecture-decisions--rationale)
11. [Configuration Priority Chain](#11-configuration-priority-chain)
12. [Deployment Architecture](#12-deployment-architecture)

---

## 1. System Overview

SunBlockCore-LL is a real-time solar energy monitoring and control system. It reads telemetry from an Epever MPPT charge controller over RS-485/Modbus, persists readings to SQLite, broadcasts live data over Socket.IO to connected browser clients, and exposes a REST API for configuration and control.

```
┌──────────────────────────────────────────────────────────┐
│                        Browser                           │
│  Alpine.js SPA ── Socket.IO client ── Plotly.js charts   │
└────────────────────┬─────────────────────────────────────┘
                     │  HTTP + WebSocket (port 3707)
┌────────────────────▼─────────────────────────────────────┐
│                SunBlockCore-LL Server                    │
│   FastAPI + python-socketio  (single ASGI process)       │
│                                                          │
│   polling_loop ──► hardware.py / simulator.py            │
│        │                                                 │
│        ▼                                                 │
│   db.py ──► SunBlockCore-LL.db  (solardata)              │
│        │    SunBlockCoreLogs.txt                         │
│        │    SunBlockAdminAudit.txt                       │
│        ▼                                                 │
│   sio.emit("solar_data", …)                              │
└────────────────────┬─────────────────────────────────────┘
                     │  RS-485 / Modbus RTU
┌────────────────────▼─────────────────────────────────────┐
│       Epever MPPT Charge Controller                      │
│       (Tracer-AN series, slave ID 1)                     │
└──────────────────────────────────────────────────────────┘
```

---

## 2. Runtime Architecture

The entire server is a **single ASGI process** composed of two ASGI apps mounted together:

```
socket_app = socketio.ASGIApp(sio, fastapi_app)
```

This means FastAPI HTTP routes and Socket.IO WebSocket upgrades are handled in the same process, share the same event loop, and have direct access to shared in-process state via the `config` module — no IPC, no message bus required.

### Why a monolith?

The deployment target is a low-power embedded computer (Raspberry Pi class) where running separate processes would consume meaningfully more RAM and add operational complexity. The simplicity of a single process with shared state outweighs the isolation benefits of splitting at this scale.

### Background Task: `polling_loop`

An asyncio `Task` runs for the lifetime of the server, managed by the `lifespan` context manager:

```
lifespan start
  └─ create_task(polling_loop())
       └─ while POLLING_ACTIVE:
            poll_fn = simulate_data | parse_data   ← selected each tick
            new_data = executor.run(poll_fn)
            SOLAR_DATA = new_data                  ← atomic ref swap
            write_db()                             ← if DATA_MAN
            sio.emit("solar_data", …)
            sleep(READ_INTERVAL)
lifespan end
  └─ POLLING_ACTIVE = False
  └─ POLLING_TASK.cancel()
```

The poll function is **re-selected on every iteration** so that toggling `SIM_MODE` at runtime takes effect immediately on the next tick without restarting the server.

---

## 3. Module Layout

```
sunblock.py      ← ASGI app, lifespan, all routes, Socket.IO events
config.py        ← ALL env constants + shared mutable runtime state
auth.py          ← JWT, bcrypt, FastAPI dependency functions, Pydantic models
db.py            ← SQLite helpers: logging, audit log, solar data r/w, settings store,
                   history & visualize queries, path validation
hardware.py      ← Epever controller: parse_data, read_controller_*, apply_*, power profiles
simulator.py     ← _SimState class: synthetic data generation from real baselines
templates/
  index.html     ← Single Jinja2 template; renders as public live view (Live tab only) or full
                    admin panel (all tabs) depending on the admin_mode context variable
  404.html       ← Custom 404 page
public/
  vendor/        ← Vendored JS (Socket.IO, Alpine.js, Plotly basic bundle ~1MB)
scripts/
  deploy.sh      ← Ubuntu deployment automation
  gen_password_hash.py
  vendor.sh      ← Downloads and pins all frontend vendor assets
```

### The `config.py` shared-state pattern

All modules mutate global state exclusively through the `config` module:

```python
# CORRECT — mutation is visible to all importers
import config
config.SIM_MODE = True

# WRONG — creates a local binding, other modules see the old value
from config import SIM_MODE
SIM_MODE = True  # only updates this module's local name
```

This pattern is used deliberately throughout. It avoids thread-safety issues around rebinding and ensures that a `PATCH /api/settings` request from a browser immediately affects the next polling tick.

---

## 4. Data Flow

### Live read cycle (1 second default)

```
polling_loop tick
  │
  ├─[SIM_MODE=true]──► simulate_data()
  │                        └─ _SimState.step()
  │                             ├─ interpolate 24h anchor table to current hour
  │                             ├─ add Gaussian noise per channel
  │                             └─ apply exponential smoothing (α=0.35)
  │
  └─[SIM_MODE=false]─► parse_data()
                           └─ EpeverChargeController.get_*()  ← Modbus RTU
                                └─ check_power_profile()
                                └─ build SOLAR_DATA dict

SOLAR_DATA dict  ──► write_db()   (if DATA_MAN=true)
                 └─► sio.emit("solar_data", {…, ConnectedUsers: N})
```

### History query cycle

```
GET /api/data/history?limit=100&offset=0&from=2025-05-11&to=2025-05-29
  │
  └─► run_in_executor(query_history, …)
          └─ sqlite3.connect(DB_NAME)   ← fresh connection, not the write cursor
               └─ SELECT * FROM solardata WHERE … LIMIT ? OFFSET ?
               └─ SELECT COUNT(*)  FROM solardata WHERE …
          └─ return {rows, total, limit, offset}
```

---

## 5. Concurrency Model

| Component | Thread/Task | Notes |
|---|---|---|
| FastAPI request handlers | asyncio coroutines on event loop | Non-blocking; blocking ops use `run_in_executor` |
| `polling_loop` | asyncio Task on same loop | Runs every `READ_INTERVAL` seconds |
| Hardware I/O (`parse_data`, `write_db`) | ThreadPoolExecutor worker | Modbus and SQLite writes are blocking |
| History queries (`query_history`) | ThreadPoolExecutor worker | Own connection — never shares write cursor |
| Socket.IO emit | async, same event loop | `await sio.emit(…)` |

### Why `run_in_executor` for DB and hardware?

SQLite and `epevermodbus` are synchronous blocking calls. Running them directly in the event loop would stall all other coroutines — including Socket.IO heartbeats and incoming HTTP requests — for the duration of the call. Offloading to a thread pool keeps the loop free.

### Write cursor vs read connections

The polling loop holds a persistent `DB_CURSOR` (opened once in `check_db()`) for writes. History queries open and close a **separate** connection per request. This avoids cursor state corruption and works around SQLite's default single-writer semantics without requiring WAL mode.

---

## 6. Persistence Layer

### `SunBlockCore-LL.db` — solar telemetry

```sql
CREATE TABLE solardata (
    Timestamp          TEXT,
    PVVoltage          REAL,
    PVCurrent          REAL,
    PVPower            REAL,
    BattVoltage        REAL,
    BattTemperature    REAL,
    BattChargePower    REAL,
    LoadPower          REAL,
    BattPercentage     INTEGER,
    BattOverallCurrent REAL,
    CPUPowerDraw       REAL,
    PowerProfile       TEXT
);
```

No primary key — rows are append-only. Timestamps are ISO strings (`YYYY-MM-DD HH:MM:SS`) stored as TEXT; SQLite's lexicographic sort on ISO dates makes date-range queries correct without a dedicated datetime column.

### `sunblock_settings.db` — runtime configuration

```sql
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
```

Simple key-value store. Booleans are serialised as the strings `"true"` / `"false"` (lowercase) for unambiguous round-tripping. The schema is intentionally minimal — no migration system is needed because there are only five known keys.

### Supported settings keys

| Key | Type | Description |
|---|---|---|
| `read_interval` | integer string | Seconds between hardware reads |
| `data_man` | bool string | Whether to write readings to SQLite |
| `sim_mode` | bool string | Whether to use the simulator |
| `token_expire_hours` | integer string | JWT session lifetime |
| `admin_password_hash` | bcrypt hash string | Hashed admin password |

### Log files

| File | Written by | Contents |
|---|---|---|
| `SunBlockCoreLogs.txt` | `sunblock_log()` | Startup events, polling errors, shutdown |
| `SunBlockAdminAudit.txt` | `admin_log()` | All authenticated write actions with timestamp + IP |

Both files fall back to stderr when `DATA_DIRECTORY` is not yet configured.

### Visualize query pipeline (`query_visualize`)

Matches the `SunBlock_DataProcessing.ipynb` notebook:

```
SQL SELECT with date WHERE clause  →  LIMIT 5_000_000 (OOM cap)
  │
  ├─ Row-based resampling:  rows[::sample]
  ├─ Spike forward-fill:    values > threshold replaced with last valid reading
  └─ Moving-average smooth: sliding window of `smooth` samples
```

Only field names present in `VIZ_FIELD_META` are accepted (allowlist). All user-supplied date strings and numeric parameters are parameterised or range-validated before reaching SQLite.

---

## 7. Authentication & Security Architecture

```
POST /api/login  (rate-limited: 5 req/min/IP)
  └─ bcrypt.checkpw(password, ADMIN_PASSWORD_HASH)
  └─ JWT signed with SECRET_KEY (HS256, exp = now + TOKEN_EXPIRE_HOURS)
  └─ Set-Cookie: sb_session=<jwt>; HttpOnly; SameSite=Strict; Secure (if configured)
  └─ admin_log("LOGIN" | "LOGIN_FAILED", user=..., ip=...)

Subsequent requests
  └─ Cookie: sb_session=<jwt>
  └─ verify_session() dependency ── FastAPI Depends
       └─ jwt.decode → validate sub == ADMIN_USERNAME
       └─ raise 401 if missing/expired/invalid
```

### Secret admin login path

The admin login page is only reachable at `/<ADMIN_PATH>` where `ADMIN_PATH` is a random slug set in `.env`. The slug is never written to any log file. The admin page injects `<meta name="referrer" content="no-referrer">` to prevent slug leakage via the HTTP Referer header.

### CSP per-request nonce

`_page_response()` generates a fresh `secrets.token_urlsafe(16)` nonce on every request. The nonce appears in both the `Content-Security-Policy` header (`script-src 'nonce-<n>'`) and the inline Alpine bootstrap `<script nonce="...">` tag. Injected scripts that lack the nonce are blocked by the browser.

### Rate limiting

| Endpoint | Limit |
|---|---|
| `POST /api/login` | 5 / minute per IP |
| `GET /api/data/history` | 60 / minute per IP |
| `GET /api/data/visualize/fields` | 60 / minute per IP |
| `GET /api/data/visualize` | 20 / minute per IP |

### Endpoint access model

| Category | Auth required | Examples |
|---|---|---|
| Public live view | No | `GET /api/data`, `GET /` |
| Historical / analysis data | Yes (JWT cookie) | `GET /api/data/history`, `GET /api/data/visualize` |
| Exports | Yes | `GET /api/data/download/*` |
| Configuration writes | Yes | `PATCH /api/settings`, `POST /api/settings/password` |
| Hardware writes | Yes + real controller | `PUT /api/controller/parameters`, `POST /api/performance-mode` |

### Admin audit log

`admin_log(action, detail, ip)` in `db.py` appends structured entries to `SunBlockAdminAudit.txt`. Called at every authenticated write endpoint. Falls back to stderr before `DATA_DIRECTORY` is configured so no events are silently dropped.

---

## 8. Simulator Architecture

The simulator (`simulator.py`) is designed to produce plausible solar curves without requiring hardware.

### Anchor table approach

Real deployment data (1.28 million rows, 2025-05-11 to 2025-05-29, Montreal) was analysed to extract hourly medians for each channel. These become the 24-entry anchor table in `_SimState`:

```python
HOURLY_BASELINES = {
    "PVVoltage": [0, 0, 0, 0, 0, 0, 2.1, 8.4, 13.2, 15.1, 15.8, 16.2,
                  16.4, 16.0, 15.3, 14.1, 11.2, 5.3, 0.8, 0, 0, 0, 0, 0],
    ...
}
```

### Per-tick synthesis

Each call to `simulate_data()`:

1. Gets current wall-clock hour `h` and fractional minute offset
2. Linearly interpolates between `HOURLY_BASELINES[h]` and `HOURLY_BASELINES[(h+1) % 24]`
3. Adds channel-specific Gaussian noise (`SIGMA` table)
4. Applies exponential smoothing with `ALPHA=0.35` against the previous tick's value — this produces realistic temporal autocorrelation (readings don't jump discontinuously)
5. Clamps to physically plausible bounds (no negative voltages, SOC 0–100, etc.)

### Why exponential smoothing?

Real solar data has strong autocorrelation — the next reading is always close to the current one. Pure Gaussian noise produces unrealistic step-changes. The smoothing constant α=0.35 was tuned to match the variance seen in the real dataset.

---

## 9. Frontend Architecture

The UI is a **single-page application** rendered by a single Jinja2 template (`index.html`), driven by Alpine.js with zero build tooling.

### Why Alpine.js instead of React/Vue?

- No Node.js build pipeline on the deployment target
- The entire app is served as static files from a Python process
- Alpine's `x-data` / `x-model` pattern is sufficient for the UI complexity
- Single file: the complete frontend is one `index.html` — easy to audit and deploy

### Server-side bootstrap

Five values are baked into the page at render time via Jinja2:

```html
<div x-data='sunblock(
  {{ is_authenticated | tojson }},
  {{ sim_mode         | tojson }},
  {{ env_defaults     | tojson }},
  {{ admin_mode       | tojson }},
  {{ viz_fields       | tojson }}
)'>
```

**Important**: the `x-data` attribute uses single quotes so that the JSON double quotes from `tojson` are safe inside it. Double-quoting `x-data` would break on the first `"` in the JSON output.

`admin_mode` is `True` when the page is served from the secret `ADMIN_PATH` route. It controls both Jinja2 server-side rendering (which HTML blocks are included) and Alpine.js behaviour (auto-open login modal for unauthenticated visitors).

`viz_fields` is the `VIZ_FIELD_META` dict from `db.py` — it populates the variable-picker checkboxes in the Visualize tab without an extra API round-trip.

### Access tiers

There are two distinct page variants rendered from the same `index.html` template, controlled by the server-side `admin_mode` Jinja2 variable:

**Public live view (`GET /`)** — `admin_mode=False`
Only the Live tab is present in the rendered HTML. Admin panels (Parameters, Energy, Settings, History, Visualize), the login/edit-params modals, the logout toolbar, the power-profile switcher, and the data-directory warning are excluded from the HTML entirely by Jinja2 `{% if admin_mode %}` guards. The browser receives a page that contains only the live data cards, rolling charts, and Socket.IO connection logic.

**Admin panel (`GET /<ADMIN_PATH>`)** — `admin_mode=True`
The full page is rendered: all six tabs, both modals, all admin controls. Admin tabs (`Parameters`, `Energy`, `Settings`, `History`, `Visualize`) are additionally wrapped in Alpine `<template x-if="authed">` so they appear in the DOM only after a successful login. The `switchTab()` method provides a UX guard; the real data protection is server-side (401 on all data endpoints).

### Real-time updates

Socket.IO pushes a `solar_data` event every `READ_INTERVAL` seconds. The client updates Alpine reactive state and extends the Plotly rolling chart traces via `Plotly.extendTraces()`.

### Live configurable extra charts

Admins can add any telemetry variable as an additional rolling chart on the Live tab. Extra charts are stored in the `extraCharts` Alpine array. Data is backfilled from the `liveHistory` rolling buffer (last `CHART_MAX` readings), so a newly added chart immediately shows recent history without a page refresh.

### Visualize tab

`plotViz()` calls `GET /api/data/visualize` with the selected date range, fields, sampling rate, smoothing window, and spike-filter flag. The server runs the same processing pipeline as the `SunBlock_DataProcessing.ipynb` notebook (date filter → row resampling → spike forward-fill → moving-average smoothing). The result is rendered with `Plotly.react()` on a single multi-trace chart.

### Charting library

All charts use **Plotly.js basic bundle** (vendored at `public/vendor/plotly.min.js`, ~1MB). The same library covers both live rolling charts (`Plotly.newPlot` + `Plotly.extendTraces`) and the historical visualize chart (`Plotly.react`).

### Tab architecture

Six tabs share a single Alpine component instance in admin mode; the public view has only the Live tab. Each tab's data is fetched lazily on first visit. Panels use `x-show` (not `x-if`) so DOM is retained between tab switches — charts don't need to be re-initialised.

---

## 10. Key Architecture Decisions & Rationale

### Decision 1: Single ASGI process

**Chosen**: FastAPI + python-socketio composed as one `ASGIApp`.  
**Rejected**: Separate HTTP and WebSocket processes/services.  
**Rationale**: Embedded deployment target (low RAM). Shared in-process state avoids Redis/IPC. Operational simplicity — one process to monitor, one port to open.

### Decision 2: `config.py` as the shared-state bus

**Chosen**: All mutable runtime state lives in `config.py`; all modules write to it via `import config; config.X = …`.  
**Rejected**: Passing state as function arguments; using a `dataclass` or `Pydantic` settings model.  
**Rationale**: Python module objects are singletons. This pattern makes mutations immediately visible across all importers without locks or queues. The convention is explicit (`config.SIM_MODE`, not `SIM_MODE`) so the shared nature is always visible at the call site.

### Decision 3: SQLite over PostgreSQL/InfluxDB

**Chosen**: SQLite for both telemetry and settings.  
**Rejected**: PostgreSQL, TimescaleDB, InfluxDB.  
**Rationale**: No database daemon process needed. Data files are portable. At 1 write/second the WAL lock contention is negligible. The deployment target may not have network access to a remote DB.

### Decision 4: ENV → DB → Code priority chain (inverted)

**Chosen**: Code defaults → `.env` overrides → DB overrides (DB wins at runtime).  
**Rejected**: DB as source of truth from first boot; `.env` ignored at runtime.  
**Rationale**: `.env` provides a reproducible "factory reset" anchor. Operators can always `DELETE FROM settings` to restore `.env` values. The `ENV_DEFAULTS` snapshot (taken at import time, before `load_settings()` runs) ensures the reset endpoint always knows the original `.env` value even after it has been overwritten in memory.

### Decision 5: Re-selecting `poll_fn` every tick

**Chosen**: `poll_fn = simulate_data if config.SIM_MODE else parse_data` inside the `while` loop.  
**Rejected**: Selecting once before the loop; restarting the loop on mode change.  
**Rationale**: Toggling SIM_MODE via the Settings panel takes effect on the very next tick with no server restart. Restarting the task would introduce a gap in data collection and require more complex task management.

### Decision 6: Separate read connection for history queries

**Chosen**: `query_history` opens `sqlite3.connect(DB_NAME)` per call.  
**Rejected**: Re-using `config.DB_CURSOR` for reads.  
**Rationale**: `DB_CURSOR` is in the middle of a write transaction when the polling loop calls `write_db()`. Sharing it for concurrent reads risks cursor state corruption and `database is locked` errors. A fresh connection is always in a clean state and SQLite supports concurrent readers safely.

---

## 11. Configuration Priority Chain

```
Lowest                                                  Highest
  ▼                                                       ▼
Code defaults  →  .env file  →  sunblock_settings.db  (runtime)
   (config.py)    (load_dotenv)   (load_settings on startup)
```

At startup:
1. `config.py` module loads → sets constants from code defaults
2. `load_dotenv()` runs → `.env` overrides code defaults
3. `ENV_DEFAULTS` snapshot is taken ← this is the "reset to env" anchor
4. `lifespan` calls `load_settings()` → DB values overwrite in-memory config

At runtime (PATCH /api/settings):
- New value written to `config.*` immediately (live effect)
- New value persisted to `sunblock_settings.db` (survives restart)

At reset (DELETE /api/settings/{key}):
- Row deleted from `sunblock_settings.db`
- `config.*` restored to `ENV_DEFAULTS[key]` (the `.env` value)

---

## 12. Deployment Architecture

```
Ubuntu server
  └─ systemd unit: sunblock.service
       └─ WorkingDirectory=/opt/sunblock
       └─ ExecStart=.venv/bin/uvicorn sunblock:socket_app --host 0.0.0.0 --port 3707
       └─ EnvironmentFile=/opt/sunblock/.env

Reverse proxy (nginx, optional)
  └─ proxy_pass http://127.0.0.1:3707
  └─ proxy_http_version 1.1
  └─ Upgrade / Connection headers for WebSocket
```

The deployment script (`scripts/deploy.sh`) automates:
- Creating the virtualenv
- Installing dependencies from `requirements.txt`
- Writing the systemd unit file
- Enabling and starting the service

**Port 3707** is the production default (configurable via `.env` `PORT`).
