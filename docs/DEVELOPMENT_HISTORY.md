# SunBlock — Development History

> Chronological record of all decisions, changes, and reasoning across the development sessions.

---

## Session 1 — Baseline Server

### Starting point

The original codebase was a single-file FastAPI + python-socketio server (`sunblock.py`). It:

- Connected to an Epever MPPT charge controller over Modbus RTU
- Polled the controller every second
- Broadcast live readings via Socket.IO (`solar_data` event)
- Persisted readings to `SunBlockCore-LL.db` (SQLite)
- Served a minimal Jinja2 HTML admin panel
- Handled login via bcrypt + JWT stored in an HttpOnly cookie
- Exposed REST endpoints for controller parameters, energy stats, power profiles

### Known limitations at this point

- No way to run without physical hardware
- All configuration required restarting the server
- Single large file (~400+ lines) with no module separation
- No persistent settings between restarts

---

## Session 2 — Simulator Mode

### Problem

The system could not be developed, tested, or demonstrated without the physical Epever controller connected. This made development on laptops and remote demos impossible.

### Solution: `simulator.py`

A `_SimState` class was built using **1.28 million rows of real deployment data** (2025-05-11 to 2025-05-29, Montreal).

**What was built:**
- 24-entry hourly anchor tables for each channel, derived from real data medians
- Gaussian noise per channel, tuned to match real data variance
- Exponential smoothing (α=0.35) to produce realistic temporal autocorrelation
- `simulate_data() -> dict` — drop-in replacement for `parse_data()`

**Activation:** `SIM_MODE=true` in `.env`

**Key design choice — re-selecting poll function per tick:**  
The polling loop originally selected `poll_fn` once before the while loop. This was changed so that the selection happens *inside* the loop:

```python
while config.POLLING_ACTIVE:
    poll_fn = simulate_data if config.SIM_MODE else parse_data
```

This means toggling `SIM_MODE` at runtime takes effect on the next tick without a server restart.

---

## Session 3 — Module Split

### Problem

`sunblock.py` had grown to a point where navigating and reasoning about it was difficult. All concerns — hardware I/O, authentication, DB operations, simulation, routes — were in a single file.

### Solution: Split into 6 modules

| Module | Responsibility |
|---|---|
| `config.py` | All env constants + all shared mutable runtime state |
| `auth.py` | JWT creation/verification, bcrypt, FastAPI `Depends` functions, Pydantic models |
| `db.py` | SQLite helpers: logging, telemetry r/w, settings persistence, history queries |
| `hardware.py` | Epever controller communication, power profiles, parameter r/w |
| `simulator.py` | Synthetic data generation from real baselines |
| `sunblock.py` | ASGI app, lifespan, all routes, Socket.IO events |

**Key design choice — `config.py` as the shared-state bus:**  
All mutable state (`SOLAR_DATA`, `POLLING_ACTIVE`, `SIM_MODE`, `DB_CURSOR`, etc.) was centralised in `config.py`. All other modules mutate it via `import config; config.X = ...` rather than `from config import X`. This ensures mutations are visible globally without re-importing, because Python module objects are singletons.

---

## Session 4 — Admin Settings Panel

### Problem

All configuration (read interval, data management, sim mode, session expiry) required editing `.env` and restarting the server — impossible to change at runtime.

### Solution: Settings tab + `PATCH /api/settings`

**Backend additions:**
- `SettingsUpdate` Pydantic model (optional fields for each setting)
- `GET /api/settings` — returns current runtime values
- `PATCH /api/settings` — updates one or more settings live
- Validation: `read_interval` 1–3600, `token_expire_hours` 1–720, `sim_mode` blocked if no hardware

**Frontend additions:**
- New "Settings" tab in the admin panel
- Toggle switches for booleans (data_man, sim_mode)
- Number inputs for numerics (read_interval, token_expire_hours)
- Auth guard — Settings tab shows lock screen if not logged in
- SIM badge in header (`<span class="badge-sim">SIM</span>`) when SIM_MODE is active

**Bug encountered and fixed:**  
`x-data="sunblock(..., {{ env_defaults | tojson }})"` — the JSON output contains double quotes, which broke the HTML attribute (also double-quoted). Fixed by switching the `x-data` attribute to single quotes:

```html
<div x-data='sunblock({{ is_authenticated | tojson }}, {{ sim_mode | tojson }}, {{ env_defaults | tojson }})'>
```

---

## Session 5 — Persistent Settings (SQLite)

### Problem

Settings changed via the admin panel were held only in memory. A server restart reverted everything to `.env` defaults.

### Solution: `sunblock_settings.db`

A second SQLite database (`DATA_DIRECTORY/sunblock_settings.db`) was added alongside the telemetry DB. It uses a simple key-value schema:

```sql
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
```

**Functions added to `db.py`:**
- `_settings_conn()` — opens the settings DB and ensures the table exists
- `load_settings()` — reads all rows and applies them to `config.*`; called once in lifespan
- `save_setting(key, value)` — upserts a single setting; called on every PATCH
- `delete_setting(key)` — removes a row; called on reset-to-env

**Booleans serialised as `"true"` / `"false"`** (lowercase) to avoid ambiguity with Python's `"True"` / `"False"` or SQLite integers.

**`ENV_DEFAULTS` snapshot:**  
This dictionary is defined in `config.py` at module level, *after* `load_dotenv()` runs but *before* `load_settings()` can overwrite anything. It captures the original `.env` values permanently for use by the reset endpoint.

**Priority chain established:**
```
Code defaults → .env overrides → DB overrides (wins at runtime)
```

---

## Session 6 — Reset-to-Env Buttons

### Problem

After changing a setting in the admin panel, there was no way to revert it to the value defined in `.env` without manually editing the settings DB.

### Solution: `DELETE /api/settings/{key}` + ↺ buttons

**Backend:**
- `DELETE /api/settings/{key}` — deletes the persisted row, restores `config.*` to `ENV_DEFAULTS[key]`
- Guard: cannot reset `sim_mode` to `false` if no hardware controller is connected

**Frontend:**
- ↺ button next to every setting
- Hint text on each row showing the env default: `env: 1s`, `env: on`, `env: 24h`
- Env default sourced from `envDefaults` Alpine state (injected from server at page load)

---

## Session 7 — History Tab

### Problem

There was no way to inspect historical solar readings stored in the database from the admin panel. The only access was the live Socket.IO stream or direct SQLite queries.

### Solution: History tab + `GET /api/data/history`

**Backend (`db.py`):**
- `query_history(limit, offset, from_ts, to_ts, order)` added
- Opens a fresh connection per call (never shares the write cursor)
- Supports `from`/`to` ISO date bounds (date-only strings extended to `YYYY-MM-DD 23:59:59`)
- Returns `{rows, total, limit, offset}`
- Gracefully returns empty payload if DB file doesn't exist yet

**Backend (`sunblock.py`):**
- `GET /api/data/history` with query params: `limit`, `offset`, `from`, `to`, `order`
- `from` is a reserved Python keyword so FastAPI `Query(alias="from")` is used
- Run off-thread via `run_in_executor`
- Public endpoint (read-only, consistent with existing `/api/data`)

**Frontend (`index.html`):**
- "History" tab added to nav
- Toolbar: date pickers (from/to), limit selector (50/100/250/500), order toggle, Load + ↻ refresh
- Summary: "Showing 1–100 of 1,519 records"
- Prev/Next pagination buttons, disabled at boundaries
- Horizontally scrollable table with all 12 columns
- Numbers formatted (2dp for voltages/currents, 1dp for temperature, integer for SOC %)
- Placeholder state before first load; auto-loads on first tab visit

**Bug encountered and fixed:**  
`str | None` type union syntax requires Python 3.10+. The venv uses Python 3.9. Fixed by importing `Optional` from `typing` and using `Optional[str]` throughout.

---

## Session 8 — Security Hardening + Feature Expansion

### Features added

**Plotly.js migration**  
Replaced uPlot with the Plotly.js basic bundle (~1MB) for all charts. Plotly provides interactive zoom, hover tooltips, multi-trace support, and a Python-consistent API that mirrors the notebook. `scripts/vendor.sh` updated.

**CSV and XLSX export**  
`GET /api/data/download/csv` and `GET /api/data/download/xlsx` added alongside the existing SQLite download. The XLSX variant uses `openpyxl` with styled headers (amber bold on dark fill) and auto-fit column widths.

**Configurable extra live charts**  
Admins can add any telemetry variable as an additional rolling chart on the Live tab. Managed by the `extraCharts` Alpine array; data is backfilled from the `liveHistory` rolling buffer.

**Visualize tab**  
Full replication of `SunBlock_DataProcessing.ipynb` inside the browser:
- Date range, sampling rate, multi-variable selection, optional moving-average smoothing, optional spike filtering
- Backend: `query_visualize()` in `db.py`; same pipeline as the notebook
- SQL `LIMIT 5_000_000` hard cap before Python resampling to prevent OOM on large databases
- Rendered with `Plotly.react()` on a single multi-trace chart

### Security hardening

**Secret admin path** — `ADMIN_PATH` slug replaces `/admin`; slug never in logs; `no-referrer` meta on admin page.

**Custom 404 page** — `templates/404.html`; API paths get JSON 404, browser paths get styled HTML.

**Auth-gated data endpoints** — history, visualize, and all downloads now require a valid session.

**Per-request CSP nonce** — `_page_response()` generates `secrets.token_urlsafe(16)` per request; nonce on inline Alpine script.

**HTTP security headers** — middleware for `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `X-XSS-Protection`, `Permissions-Policy`.

**Rate limiting on data endpoints** — `@limiter.limit("60/minute")` on history/fields; `@limiter.limit("20/minute")` on visualize.

**`data_directory` path manipulation protection** — `_validate_data_directory()` resolves symlinks and rejects system paths before any directory is created or config is mutated.

**Admin audit log** — `admin_log()` in `db.py` appends to `SunBlockAdminAudit.txt`; called at every authenticated write action.

**FastAPI docs disabled** — `docs_url=None`, `redoc_url=None`, `openapi_url=None`.

**Socket.IO CORS locked** — `cors_allowed_origins=[]`.

**`SECRET_KEY` startup warning** — logged if the default is detected.

---

## Summary of All API Endpoints (current)

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/` | No | Public live view (data cards only) |
| GET | `/<ADMIN_PATH>` | No | Admin login entry-point |
| GET | `/api/mode` | No | `{mode: "simulator"\|"live"}` |
| POST | `/api/login` | No (5/min) | Set session cookie |
| POST | `/api/logout` | No | Clear session cookie |
| GET | `/api/auth/status` | No | `{authenticated: bool}` |
| GET | `/api/data` | No | Current live reading |
| GET | `/api/data/history` | **Yes** (60/min) | Paginated historical readings |
| GET | `/api/data/visualize/fields` | **Yes** (60/min) | Plottable field metadata |
| GET | `/api/data/visualize` | **Yes** (20/min) | Time-series data for Visualize tab |
| GET | `/api/data/download` | **Yes** | Download as SQLite |
| GET | `/api/data/download/csv` | **Yes** | Download as CSV |
| GET | `/api/data/download/xlsx` | **Yes** | Download as Excel |
| GET | `/api/settings` | **Yes** | Current runtime settings |
| PATCH | `/api/settings` | **Yes** | Update one or more settings |
| DELETE | `/api/settings/{key}` | **Yes** | Reset setting to .env value |
| POST | `/api/settings/password` | **Yes** | Change admin password |
| GET | `/api/power-profile` | Controller | Current power profile |
| POST | `/api/performance-mode` | **Yes** + HW | Set performance profile |
| POST | `/api/power-saver-mode` | **Yes** + HW | Set power-saver profile |
| POST | `/api/balanced` | **Yes** + HW | Set balanced profile |
| GET | `/api/controller/parameters` | Controller | Read battery/charge params |
| PUT | `/api/controller/parameters` | **Yes** + HW | Write battery/charge params |
| GET | `/api/controller/stats` | Controller | Energy statistics |
| GET | `/api/controller/status` | Controller | Controller status + RTC |
| POST | `/api/controller/rtc/sync` | **Yes** + HW | Sync RTC to server time |

**Auth legend:**  
- *No* — public endpoint  
- ***Yes*** — requires valid JWT cookie (`verify_session`)  
- *Controller* — requires controller OR sim mode (`require_controller`)  
- ***Yes*** + HW — requires auth AND real hardware (`verify_session` + `require_real_controller`)

---

## Summary of All Files Changed

| File | Sessions | Key changes |
|---|---|---|
| `config.py` | 3, 5, 8 | Module introduced; SETTINGS_DB_NAME, ENV_DEFAULTS, ADMIN_PATH, ADMIN_AUDIT_FILE |
| `auth.py` | 3, 4, 5 | Module introduced; SettingsUpdate, PasswordChange models |
| `db.py` | 3, 5, 7, 8 | Module introduced; settings store, query_history, query_visualize, admin_log, _validate_data_directory |
| `hardware.py` | 3 | Extracted from sunblock.py |
| `simulator.py` | 2, 3 | Created; extracted to own module |
| `sunblock.py` | 2–8 | Refactored; all routes, security hardening, rate limits, CSP, audit log calls |
| `templates/index.html` | 4–8 | Settings, History, Visualize tabs; extra charts; auth-gating; CSP nonce; Plotly |
| `templates/404.html` | 8 | Created: custom 404 page |
| `sample.env` | 2, 8 | SIM_MODE, ADMIN_PATH, cleanup |
| `scripts/deploy.sh` | early, 8 | systemd deployment; ADMIN_PATH generation; openpyxl |
| `scripts/vendor.sh` | 8 | uPlot → Plotly basic bundle |
| `.gitignore` | 8 | Added *.db, demo_data/, *.xlsx |
