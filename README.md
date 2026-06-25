# SunBlockCore-LL

Real-time solar energy monitoring and control system for the SunBlock Project.

**TAG MC-Bloc · Milieux Institute · Concordia University · Montreal, Canada**  
https://github.com/MC-Bloc/SunBlock

---

## Overview

SunBlockCore-LL reads telemetry from an Epever MPPT charge controller over RS-485/Modbus, persists every reading to SQLite, and streams live data to a browser-based admin panel via Socket.IO. A built-in simulator lets you run and develop the system without any hardware attached.

```
Browser (Alpine.js SPA)
   ↕  HTTP + WebSocket
SunBlockCore-LL Server (FastAPI + python-socketio)
   ↕  RS-485 / Modbus RTU
Epever MPPT Charge Controller
```

---

## Features

- **Live dashboard** — battery %, voltage, temperature, PV power, load, CPU draw; auto-updates every second via Socket.IO
- **Configurable extra live charts** — add any variable to the live view; data backfilled from the rolling buffer
- **Plotly charts** — interactive, zoomable charts on both the Live and Visualize tabs
- **Historical data browser** — paginated table with date-range filtering; auth-required
- **Visualize tab** — plot any combination of variables over a custom date range with configurable sampling rate, moving-average smoothing, and spike filtering; replicates the offline notebook workflow in-browser
- **Data export** — download the full telemetry database as SQLite, CSV, or XLSX (auth-required)
- **Power profile switching** — Performance / Balanced / Power Saver, applied immediately to the OS
- **Controller parameters** — view and edit battery configuration and voltage thresholds from the browser
- **Energy statistics** — today / this month / this year / all-time generated and consumed kWh
- **Runtime settings** — change poll interval, toggle data logging, toggle simulator, adjust session expiry — all live, all persistent across restarts
- **API tokens** — generate revocable bearer tokens with configurable expiry to call the API from outside the browser (scripts, dashboards, automations)
- **Two-factor authentication (TOTP)** — optional second factor for the admin login (any standard authenticator app), with one-time backup recovery codes
- **Simulator mode** — generates realistic synthetic data based on 1.28 million rows of real Montreal solar data; no hardware required
- **Separated public / admin views** — `/` serves only the live feed with no admin HTML in the page; the full admin panel (all tabs, login modal) is served exclusively from the secret `ADMIN_PATH` URL
- **Secure admin panel** — bcrypt passwords, JWT sessions in HttpOnly cookies, per-request CSP nonces, secret login path, per-IP rate limiting on all data endpoints, admin audit log

---

## Quick Start

### 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Vendor frontend assets

```bash
bash scripts/vendor.sh
```

### 3. Configure

```bash
cp sample.env .env
```

Edit `.env`:

```env
# Hardware
CONTROLLER_PORT=/dev/ttyACM0
CONTROLLER_SLAVE=1

# Paths — DATA_DIRECTORY is required
DATA_DIRECTORY=/opt/sunblock/data

# Auth
ADMIN_PASSWORD_HASH=<bcrypt hash>   # python3 scripts/gen_password_hash.py
# SECRET_KEY: leave blank — the server generates and persists a random key on
# first run (survives restarts). Set it explicitly only to pin a known value
# across multiple instances.
SECRET_KEY=
ADMIN_PATH=<random slug>            # python3 -c "import secrets; print(secrets.token_urlsafe(12))"

# Optional
SIM_MODE=false
READ_INTERVAL=1
DATA_MAN=true
TOKEN_EXPIRE_HOURS=24
SECURE_COOKIES=false    # set true behind HTTPS
```

### 4. Run

```bash
# With hardware:
.venv/bin/uvicorn sunblock:socket_app --host 0.0.0.0 --port 3707

# Without hardware (simulator):
SIM_MODE=true DATA_DIRECTORY=./demo_data/ \
  .venv/bin/uvicorn sunblock:socket_app --host 0.0.0.0 --port 3707
```

The public live view is at `http://localhost:3707`.  
The admin login page is at `http://localhost:3707/<ADMIN_PATH>` — keep this URL private.

---

## Project Structure

```
sunblock.py          ASGI app, lifespan, all routes, Socket.IO events
config.py            Environment constants + shared mutable runtime state
auth.py              JWT, bcrypt, FastAPI dependency functions, Pydantic models
db.py                SQLite: telemetry writes, settings persistence, history & visualize queries
hardware.py          Epever controller communication, power profiles, parameter r/w
simulator.py         Synthetic data generation from real deployment baselines
templates/
  _base.html         Shared page shell (head, header, Live tab, Alpine bootstrap)
  admin.html         Full admin panel (Parameters, Energy, Settings, History, Visualize,
                     login + edit-params modals) — extends _base.html; secret-path only
  public.html        Public live view — extends _base.html, overrides nothing
  404.html           Custom 404 page
public/
  css/index.css      Extracted page styles
  js/sunblock.js     Extracted Alpine.js admin-panel component
  vendor/            Vendored JS (Alpine.js, Socket.IO, Plotly basic bundle, qrcodejs)
scripts/
  deploy.sh          Automated Ubuntu/systemd deployment
  vendor.sh          Downloads and pins all frontend vendor assets
  gen_password_hash.py
Systemd/             Pre-made systemd unit + launcher script (manual install alternative
                       to deploy.sh) — see Systemd/README.md
docs/
  ARCHITECTURE.md    System design, module layout, all architecture decisions
  DEVELOPMENT_HISTORY.md  Chronological record of all changes
  USER_STORIES.md    User stories covering all features
  SECURITY.md        Security model, known issues, hardening checklist
  AGENT_HANDOFF.md   Onboarding doc for new developers / agents
```

---

## API

All endpoints return JSON. Write operations and data access require either a valid session cookie (browser) or an `Authorization: Bearer <token>` header (external/programmatic access — generate tokens from Settings → API Tokens).

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | No | Public live view — data cards and rolling charts only; no admin HTML in page |
| GET | `/<ADMIN_PATH>` | No | Full admin panel — all tabs, login modal; path is secret |
| GET | `/api/mode` | No | `{mode: "simulator"\|"live"}` |
| POST | `/api/login` | No (5 req/min) | Authenticate — sets session cookie, or a short-lived 2FA challenge cookie + `{requires_2fa: true}` if 2FA is enabled |
| POST | `/api/login/verify-2fa` | No (5 req/min) | Complete login with a TOTP or backup code — exchanges the pending challenge for a session cookie |
| POST | `/api/logout` | No | Clear session cookie(s) |
| GET | `/api/auth/status` | No | `{authenticated: bool}` |
| GET | `/api/data` | No | Current live reading |
| GET | `/api/data/history` | **Yes** (60 req/min) | Paginated history: `?limit=&offset=&from=&to=&order=` |
| GET | `/api/data/visualize/fields` | **Yes** (60 req/min) | Plottable field metadata |
| GET | `/api/data/visualize` | **Yes** (20 req/min) | Time-series data: `?fields=&from=&to=&sample=&smooth=&filter_spikes=` |
| GET | `/api/data/download` | **Yes** | Download telemetry as SQLite |
| GET | `/api/data/download/csv` | **Yes** | Download telemetry as CSV |
| GET | `/api/data/download/xlsx` | **Yes** | Download telemetry as Excel |
| GET | `/api/settings` | **Yes** | Current runtime settings |
| PATCH | `/api/settings` | **Yes** | Update settings live |
| DELETE | `/api/settings/{key}` | **Yes** | Reset setting to `.env` value |
| POST | `/api/settings/password` | **Session only** | Change admin password |
| GET | `/api/2fa/status` | **Session only** | `{enabled, backup_codes_remaining}` |
| POST | `/api/2fa/setup` | **Session only** | Begin enrollment — returns a pending TOTP secret + `otpauth://` URI for the QR code |
| POST | `/api/2fa/confirm` | **Session only** | `{code}` — verify the pending secret and activate 2FA; returns one-time backup codes |
| POST | `/api/2fa/disable` | **Session only** | `{password, code}` — turn 2FA off (requires both factors) |
| POST | `/api/2fa/backup-codes/regenerate` | **Session only** | `{code}` — invalidate old backup codes and issue 10 new ones |
| POST | `/api/tokens` | **Session only** | Generate an API token — `{name, expires_in_hours}`; raw token returned once |
| GET | `/api/tokens` | **Session only** | List API tokens (metadata only — no raw tokens or hashes) |
| DELETE | `/api/tokens/{id}` | **Session only** | Revoke an API token |
| GET | `/api/power-profile` | Controller | Current power profile |
| GET | `/api/controller/parameters` | Controller | Battery and charge configuration |
| PUT | `/api/controller/parameters` | **Yes** + HW | Write battery/charge config |
| GET | `/api/controller/stats` | Controller | Energy statistics |
| GET | `/api/controller/status` | Controller | Controller status, temperatures, RTC |
| POST | `/api/controller/rtc/sync` | **Yes** + HW | Sync RTC to server time |
| POST | `/api/performance-mode` | **Yes** + HW | Set performance power profile |
| POST | `/api/power-saver-mode` | **Yes** + HW | Set power-saver profile |
| POST | `/api/balanced` | **Yes** + HW | Set balanced profile |

**Auth legend:** *Yes* = valid JWT session cookie **or** `Authorization: Bearer <API token>`. *Session only* = JWT cookie required; bearer tokens are deliberately rejected on these sensitive/self-management endpoints (see `docs/SECURITY.md`). *Controller* = requires controller or sim mode. *HW* = requires real hardware (not sim).

### Calling the API from outside the browser

1. Log in to the admin panel and open **Settings → API Tokens**.
2. Give the token a name and an expiry, then click **Generate**. The raw token is shown once — copy it immediately.
3. Send it as a bearer token on each request:
   ```bash
   curl -H "Authorization: Bearer sbll_<token>" http://localhost:3707/api/data/history
   ```
4. Revoke it from the same panel at any time — revocation takes effect immediately.

### Two-factor authentication

1. Log in, open **Settings → Two-Factor Authentication**, and click **Enable**.
2. Scan the QR code with any TOTP authenticator app (Google Authenticator, Authy, 1Password, …) — or type in the secret manually — then enter the 6-digit code it shows to confirm.
3. Save the 10 one-time backup codes shown immediately afterward; they're shown exactly once and let you log in if you lose your authenticator device.
4. From then on, logging in requires the password **and** a current code from the app (or an unused backup code).

Disabling 2FA requires both the current password and a valid code — a leaked session cookie alone can't turn it off.

### Live Socket.IO

Connect a Socket.IO client and listen for `solar_data` (fires every `READ_INTERVAL` seconds):

```javascript
const socket = io("http://<host>:<port>");
socket.on("solar_data", data => console.log(data));
```

---

## Data Storage

All files are written to `DATA_DIRECTORY`:

| File | Contents |
|------|----------|
| `SunBlockCore-LL.db` | Telemetry — one row per reading, append-only |
| `sunblock_settings.db` | Runtime settings — key/value, survives restarts |
| `SunBlockCoreLogs.txt` | Application log (startup, errors, polling events) |
| `SunBlockAdminAudit.txt` | Admin audit log — every authenticated action with timestamp and IP |

**Telemetry columns:** `Timestamp`, `PVVoltage`, `PVCurrent`, `PVPower`, `BattVoltage`, `BattTemperature`, `BattChargePower`, `LoadPower`, `BattPercentage`, `BattOverallCurrent`, `CPUPowerDraw`, `PowerProfile`

---

## Configuration Reference

All values can be set in `.env`. Persistent overrides written via the admin panel are stored in `sunblock_settings.db` and take precedence over `.env` at runtime (see `docs/ARCHITECTURE.md` for the full priority chain).

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTROLLER_PORT` | `/dev/ttyACM0` | Serial port for the Epever controller |
| `CONTROLLER_SLAVE` | `1` | Modbus slave ID |
| `DATA_DIRECTORY` | *(required)* | Directory for DB files and logs |
| `DATA_MAN` | `true` | Write readings to SQLite |
| `READ_INTERVAL` | `1` | Seconds between readings |
| `SIM_MODE` | `false` | Use simulator instead of hardware |
| `ADMIN_USERNAME` | `admin` | Admin login username |
| `ADMIN_PASSWORD_HASH` | *(none)* | bcrypt hash — generate with `scripts/gen_password_hash.py` |
| `SECRET_KEY` | *(auto-generated)* | JWT signing key — leave blank; the server generates and persists a random 256-bit key on first run |
| `TOKEN_EXPIRE_HOURS` | `24` | Session lifetime in hours |
| `SECURE_COOKIES` | `false` | Set `true` when running behind HTTPS |
| `ADMIN_PATH` | *(none)* | Secret URL slug for the admin login page — **set this** |
| `PORT` | `3707` | Listening port (used by `deploy.sh`) |

---

## Deployment

```bash
bash scripts/deploy.sh
```

This installs dependencies, generates a random `ADMIN_PATH`, writes a systemd unit file, and starts the service on port 3707. The final output prints both the public URL and the private admin URL.

Prefer to wire up the service by hand, or migrating from the original SunBlock project's `SB_RunSunBlockCore` / `SB_RunSunBlockExpress` two-service systemd setup? `Systemd/` ships ready-made unit + launcher files (`SB_RunSunBlockCore-LL.service` / `.sh`) — see [`Systemd/README.md`](Systemd/README.md) for copy-paste install steps. Only one service is needed since SunBlockCore-LL is a single unified ASGI app.

For HTTPS, terminate TLS at a reverse proxy (nginx, Caddy) and proxy WebSocket upgrade headers.

See `docs/SECURITY.md` for the full production hardening checklist.

---

## Simulator

Set `SIM_MODE=true` to run without hardware. The simulator uses hourly baseline tables derived from real deployment data (Montreal, May 2025) with per-channel Gaussian noise and exponential smoothing (α=0.35) to produce realistic autocorrelated readings.

Toggle simulator mode live from the Settings tab in the admin panel — takes effect on the next poll tick.

---

## Documentation

Full documentation is in `docs/`:

- [`ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system design, concurrency model, all architectural decisions with rationale
- [`DEVELOPMENT_HISTORY.md`](docs/DEVELOPMENT_HISTORY.md) — chronological record of every change and why
- [`USER_STORIES.md`](docs/USER_STORIES.md) — user stories covering all implemented features
- [`SECURITY.md`](docs/SECURITY.md) — security model, known issues, hardening checklist
- [`AGENT_HANDOFF.md`](docs/AGENT_HANDOFF.md) — developer/agent onboarding guide

---

## License

[MIT](LICENSE) — © 2023–2026 Milieux Institute, Concordia University.

---

## Dependencies

```
fastapi
uvicorn
python-socketio
epevermodbus
python-dotenv
python-jose[cryptography]
bcrypt
pyotp
slowapi
jinja2
openpyxl
```

Vendor frontend assets: `bash scripts/vendor.sh`
