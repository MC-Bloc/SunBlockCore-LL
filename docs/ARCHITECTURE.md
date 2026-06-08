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
  _base.html     ← Shared page shell (head, header, Live tab, Alpine bootstrap,
                    script tags); declares 4 empty Jinja2 blocks for admin-only content
  admin.html     ← extends _base.html; fills the blocks with the full admin UI
                    (all tabs, both modals); rendered only at /<ADMIN_PATH>
  public.html    ← extends _base.html; overrides nothing — admin blocks stay empty,
                    so the public response structurally cannot contain admin markup
  404.html       ← Custom 404 page
public/
  css/           ← index.css — extracted page styles (formerly an inline <style> block)
  js/            ← sunblock.js — extracted Alpine component (formerly an inline <script>)
  vendor/        ← Vendored JS (Socket.IO, Alpine.js, Plotly basic bundle ~1MB, qrcodejs)
scripts/
  deploy.sh      ← Ubuntu deployment automation
  gen_password_hash.py
  vendor.sh      ← Downloads and pins all frontend vendor assets
Systemd/
  SB_RunSunBlockCore-LL.service ← pre-made systemd unit (alternative to deploy.sh's auto-generated one)
  SB_RunSunBlockCore-LL.sh      ← launcher script; goes in /usr/local/bin/
  README.md      ← manual install + troubleshooting steps
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
| `secret_key` | random hex string | JWT signing key — auto-generated and persisted on first run if `SECRET_KEY` isn't set in `.env` (see SEC-001 in `docs/SECURITY.md`) |
| `totp_secret` | base32 string | Active TOTP secret — written only after enrollment is confirmed |
| `totp_enabled` | bool string | Whether 2FA is currently active |

### `backup_codes` — 2FA recovery codes

```sql
CREATE TABLE backup_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash TEXT NOT NULL UNIQUE,   -- SHA-256 of the formatted code; raw never stored
    created_at TEXT NOT NULL,
    used_at TEXT                      -- NULL until consumed (one-time use)
);
```

Created lazily by `db._backup_codes_conn()`, mirroring `_tokens_conn()` exactly — same rationale (fixed-location DB, low write volume, established connection pattern, fast unsalted SHA-256 appropriate for high-entropy random values). Codes are generated 10 at a time via `generate_backup_codes()`, shown to the admin once, and consumed (marked `used_at`) on first successful use by `verify_and_consume_backup_code()`.

### `api_tokens` — external API authentication

```sql
CREATE TABLE api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT,        -- NULL = never expires
    last_used_at TEXT
);
```

Lives in the same `sunblock_settings.db` rather than a dedicated database file or the telemetry DB — see "API token design rationale" below for why. Created lazily by `db._tokens_conn()`, following the exact `_settings_conn()` pattern (connection-per-call, `CREATE TABLE IF NOT EXISTS`, try/finally close).

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
  └─ if TOTP_ENABLED:
       └─ JWT { sub, purpose: "2fa_pending", exp: now + 5min }, signed with SECRET_KEY
       └─ Set-Cookie: sb_2fa_pending=<jwt>; HttpOnly; SameSite=Strict
       └─ respond { requires_2fa: true } — no sb_session issued yet
       └─ admin_log("LOGIN_PASSWORD_OK_2FA_PENDING", ...)
  └─ else:
       └─ JWT signed with SECRET_KEY (HS256, exp = now + TOKEN_EXPIRE_HOURS)
       └─ Set-Cookie: sb_session=<jwt>; HttpOnly; SameSite=Strict; Secure (if configured)
       └─ admin_log("LOGIN" | "LOGIN_FAILED", user=..., ip=...)

POST /api/login/verify-2fa  (rate-limited: 5 req/min/IP) — only reachable mid-2FA-challenge
  └─ verify_pending_2fa_token(sb_2fa_pending) → must be unexpired, purpose == "2fa_pending"
  └─ verify_totp_code(code)  OR  verify_and_consume_backup_code(code)
  └─ on success: delete sb_2fa_pending, issue sb_session exactly as the no-2FA path above
  └─ admin_log("2FA_LOGIN" / "2FA_CHALLENGE_FAILED" / "BACKUP_CODE_USED", ...)

Subsequent requests (browser)
  └─ Cookie: sb_session=<jwt>
  └─ verify_session() dependency ── FastAPI Depends
       └─ jwt.decode → validate sub == ADMIN_USERNAME AND "purpose" not in payload
       └─ raise 401 if missing/expired/invalid/pending-2FA
       (the purpose check is what stops a captured sb_2fa_pending token from
        ever being replayed as a full session — see "2FA design rationale")

Subsequent requests (external API client)
  └─ Authorization: Bearer sbll_<token>
  └─ verify_session_or_token() dependency ── FastAPI Depends
       └─ tries the sb_session cookie first (browser path, above)
       └─ else: hashes the bearer token (SHA-256) and looks it up in api_tokens
       └─ rejects if hash not found, or expires_at has passed (and lazily deletes it)
       └─ records last_used_at, returns ADMIN_USERNAME
       └─ raise 401 if neither credential is valid
```

### API token design rationale

The admin panel (Settings → API Tokens) lets the admin generate bearer tokens for scripts, dashboards, and other external clients that can't hold a session cookie. Three decisions were made explicitly, each with a one-line reason:

| Decision | Reasoning |
|---|---|
| **Storage: new `api_tokens` table in the existing `sunblock_settings.db`**, not a new database file | That DB already sits at a fixed location independent of `DATA_DIRECTORY`, already holds low-write-volume admin/config metadata, and reuses the established `_settings_conn()`-style connection pattern. A new file would add an operational artifact for no benefit; the high-volume telemetry DB is the wrong home for auth records. |
| **Hashing: SHA-256 of the raw token**, not bcrypt | Tokens are generated with `secrets.token_urlsafe(32)` — 256 bits of entropy, already far beyond brute-force range. bcrypt's deliberate slowness exists to defend *low-entropy human passwords*; applying it here would only slow down every API request without adding real protection. The raw token is shown to the admin exactly once, at creation, and is never persisted. |
| **Scope: token = full session-equivalent access, except token/password management**, not a separate permission tier | There is a single admin account (see "Single admin account" in `docs/SECURITY.md`) — a multi-tier permission system would add complexity with no second principal to apply it to. The one carve-out: `POST/GET/DELETE /api/tokens` and `POST /api/settings/password` always require the real session cookie (`verify_session`, not `verify_session_or_token`). This bounds a leaked token's blast radius — it can read/write data and control hardware, but cannot mint replacement tokens, see/revoke other tokens, or change the admin password, so the legitimate admin always retains the ability to shut it down. |

`POST /api/tokens` returns the raw value once (`{id, name, token, message}`); `GET /api/tokens` returns metadata only (id, name, created_at, expires_at, last_used_at — never the hash or raw value); `DELETE /api/tokens/{id}` deletes the row immediately, taking effect on the next request. All three actions are written to the admin audit log (`TOKEN_CREATED`, `TOKEN_REVOKED`).

### 2FA design rationale

Two-factor authentication (Settings → Two-Factor Authentication) adds a TOTP-based second factor (`pyotp`, RFC 6238 — compatible with any standard authenticator app). Several decisions were made explicitly:

| Decision | Reasoning |
|---|---|
| **Two-step enrollment (`/setup` then `/confirm`)**, secret held only in memory until confirmed | A single-step "generate and immediately activate" flow risks permanent lockout if the admin mistypes the secret or the authenticator app is misconfigured — there would be no way back in without DB surgery. Requiring a successful code BEFORE persisting `totp_secret`/`totp_enabled` means a failed confirmation changes nothing; the admin just calls `/setup` again. The pending secret lives in `sunblock._pending_totp_secret` (a module-level variable, single-admin model), never written to disk — it cannot "accidentally" activate via a server restart mid-enrollment. |
| **A distinct "pending" JWT (`purpose: "2fa_pending"`) in its own cookie (`sb_2fa_pending`)**, not a partial/scoped session | This is the crux of the two-step login's security: `verify_session`, `check_session`, and `verify_session_or_token` all explicitly check `"purpose" not in payload`, so a pending token can *never* be replayed as `sb_session` — even if it leaked (e.g. via a misconfigured proxy log) it is useless outside `POST /api/login/verify-2fa`, and even there it only grants a 5-minute window to attempt the second factor (itself rate-limited at 5/min like the first). |
| **Persist-once-and-reuse, not regenerate-per-login**, for `totp_secret` | TOTP is inherently a shared-secret scheme — the server must hold the same secret the authenticator app was provisioned with, for the lifetime of the enrollment. This mirrors why `SECRET_KEY` is now persisted rather than regenerated (see SEC-001): a stable secret is what makes the feature usable at all. |
| **Backup codes: 10 single-use SHA-256-hashed codes**, mirroring `api_tokens` | Authenticator devices get lost, factory-reset, or left at home. Without an out-of-band recovery path, a lost device would be a hard lockout (no email/SMS infrastructure exists in this single-admin, offline-capable system). Codes are high-entropy (`secrets.token_hex`), so a fast hash is correct — same reasoning as API tokens. Each is consumed (one-time) on use, and the admin is warned in the application log when ≤2 remain. |
| **Disable requires BOTH password and a valid code**, session-only (never bearer-token-eligible) | Exactly mirrors `change_password`'s defense-in-depth (see `docs/SECURITY.md` §2). A hijacked session alone (XSS, shared-machine carelessness) must not be able to single-handedly strip the account's strongest protection — and a leaked API bearer token, which is already barred from password/token management, is barred here too. |

`POST /api/2fa/setup` returns `{secret, otpauth_uri}`; `POST /api/2fa/confirm` returns the 10 backup codes once (`{backup_codes, backup_codes_notice}`); `POST /api/2fa/disable` and `POST /api/2fa/backup-codes/regenerate` require a current password+code or code respectively. All five `/api/2fa/*` endpoints are written to the admin audit log under their own action names (see `docs/SECURITY.md` §8 for the full list).

### Secret admin login path

The admin login page is only reachable at `/<ADMIN_PATH>` where `ADMIN_PATH` is a random slug set in `.env`. The slug is never written to any log file. The admin page injects `<meta name="referrer" content="no-referrer">` to prevent slug leakage via the HTTP Referer header.

### Content Security Policy

`_page_response()` attaches a static `Content-Security-Policy` header to every HTML response: `default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'`.

Earlier versions of this app served one inline `<script>` (the Alpine component) and used a per-request `secrets.token_urlsafe(16)` nonce — minted fresh on every render and stamped onto both the CSP header (`script-src 'nonce-<n>'`) and the `<script nonce="...">` tag — so that only that specific script could execute and injected scripts (lacking the nonce) were blocked.

That inline script has since been extracted to `public/js/sunblock.js` (a same-origin static file — see §9 "Frontend Architecture"), which means **there is no inline `<script>` left in any template**. `script-src 'self'` alone now covers loading every script the app uses (vendored libs + `sunblock.js`), so the nonce machinery was removed entirely as dead complexity — fewer moving parts for the same (arguably stronger — zero exceptions for inline content) protection. `'unsafe-eval'` remains required because Alpine.js evaluates `x-data`/`x-on` expressions via `new Function()`, and `style-src 'unsafe-inline'` remains required because Alpine's `:style` bindings set inline `style=""` attributes on elements.

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
| Historical / analysis data | Yes (cookie or API token) | `GET /api/data/history`, `GET /api/data/visualize` |
| Exports | Yes (cookie or API token) | `GET /api/data/download/*` |
| Configuration writes | Yes (cookie or API token) | `PATCH /api/settings` |
| Hardware writes | Yes (cookie or API token) + real controller | `PUT /api/controller/parameters`, `POST /api/performance-mode` |
| Account / token management | **Session cookie only** — bearer tokens rejected | `POST /api/settings/password`, `POST/GET/DELETE /api/tokens` |

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

The UI is a **single-page application** assembled from a small set of Jinja2 templates and two extracted static assets, driven by Alpine.js with zero build tooling.

### Template structure — inheritance, not duplication

Originally the entire frontend (CSS + HTML + JS, ~1850 lines) lived in one `templates/index.html`, with `{% if admin_mode %}` guards switching admin content on/off. That file was split into:

- **`templates/_base.html`** — the shared shell: `<head>` (incl. the CSS `<link>`), header, the Live tab/panel, the Alpine `x-data` bootstrap div, and the closing `<script>` tags. It declares four empty Jinja2 blocks where admin-only markup used to be inlined: `referrer_meta`, `extra_tabs`, `live_admin_extras`, `admin_panels`.
- **`templates/admin.html`** — `{% extends "_base.html" %}`, overriding all four blocks with the full admin UI (Parameters/Energy/Settings/History/Visualize panels, the login modal, the edit-params modal, the logout toolbar, etc). Rendered **only** by the route registered at the secret `ADMIN_PATH` slug.
- **`templates/public.html`** — `{% extends "_base.html" %}`, overriding nothing. Because the blocks default to empty, the rendered output for `GET /` **structurally cannot contain** any admin markup — it's not hidden with CSS or `x-show`, it's simply never generated or transmitted.

This was chosen over copy-pasting two near-identical files specifically to avoid maintaining duplicate copies of the shared CSS/header/Live-tab/Alpine-bootstrap — changes to shared chrome happen in exactly one place (`_base.html`) and automatically apply to both page variants.

### Why Alpine.js instead of React/Vue?

- No Node.js build pipeline on the deployment target
- The entire app is served as static files from a Python process
- Alpine's `x-data` / `x-model` pattern is sufficient for the UI complexity
- No bundler: the frontend is a handful of plain HTML/CSS/JS files — easy to audit and deploy

### Extracted static assets

The page-wide `<style>` block and the `Alpine.data('sunblock', ...)` component (the two largest chunks of the old monolith) now live as standalone static files served via the existing `/static` mount (`StaticFiles(directory="public")`):

- **`public/css/index.css`** — all page styles, referenced via `<link rel="stylesheet" href="/static/css/index.css" />`
- **`public/js/sunblock.js`** — the entire Alpine component plus its supporting constants (`CHART_VARS`, `PLOTLY_LAYOUT`, etc.), referenced via `<script src="/static/js/sunblock.js"></script>`

Both were lifted out **verbatim** — neither contains any Jinja2 `{{ }}`/`{% %}` syntax. This matters for `sunblock.js` in particular: the component still receives all server-rendered values (auth state, sim mode, env defaults, `viz_fields`, etc.) but purely as **constructor arguments** passed in from `_base.html`'s `x-data='sunblock(...)'` call — never by templating values directly into the JS source. That's what makes it possible to serve the file as a plain, cacheable, same-origin static asset rather than re-rendering it through Jinja2 on every request. (It also happens to be what made the CSP nonce removal possible — see §7.)

### Server-side bootstrap

Six values are baked into the page at render time via Jinja2, in `_base.html`:

```html
<div x-data='sunblock(
  {{ is_authenticated | tojson }},
  {{ sim_mode         | tojson }},
  {{ env_defaults     | tojson }},
  {{ data_directory   | tojson }},
  {{ admin_mode       | tojson }},
  {{ viz_fields       | tojson }}
)'>
```

**Important**: the `x-data` attribute uses single quotes so that the JSON double quotes from `tojson` are safe inside it. Double-quoting `x-data` would break on the first `"` in the JSON output.

`admin_mode` is `True` when the page is served from the secret `ADMIN_PATH` route (and, correspondingly, `admin.html` is the template in use rather than `public.html`). It still drives Alpine.js behaviour (e.g. auto-open login modal for unauthenticated visitors) — the Jinja2-side gating it used to also control is now expressed structurally via which template extends `_base.html`.

`viz_fields` is the `VIZ_FIELD_META` dict from `db.py` — it populates the variable-picker checkboxes in the Visualize tab without an extra API round-trip.

### Access tiers

There are two distinct page variants, now backed by **two distinct templates** (rather than one template branching on `admin_mode`):

**Public live view (`GET /`)** — renders `public.html` (`admin_mode=False`)
Only the Live tab is present in the rendered HTML. Admin panels (Parameters, Energy, Settings, History, Visualize), the login/edit-params modals, the logout toolbar, the power-profile switcher, and the data-directory warning are absent from the response — not because of an `{% if %}` guard evaluating false, but because `public.html` simply never overrides the blocks that would contain them. The browser receives a page that contains only the live data cards, rolling charts, and Socket.IO connection logic.

**Admin panel (`GET /<ADMIN_PATH>`)** — renders `admin.html` (`admin_mode=True`)
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

### Pre-made systemd unit files (`Systemd/`)

For operators who prefer to install the service by hand, or who are migrating
from the original SunBlock project's two-process layout, `Systemd/` ships
ready-made files that mirror what `deploy.sh` generates:

```
Systemd/SB_RunSunBlockCore-LL.service  ← systemd unit (mirrors sunblock.service above)
Systemd/SB_RunSunBlockCore-LL.sh       ← launcher script; cd's to the repo and execs uvicorn
Systemd/README.md                      ← copy-paste install + troubleshooting steps
```

The `.sh` launcher must be copied to `/usr/local/bin/` (the `.service` file's
`ExecStart` points there), matching the convention used by the original
`SB_RunSunBlockCore.sh` / `SB_RunSunBlockExpress.sh` scripts in
[MC-Bloc/SunBlock/Systemd](https://github.com/MC-Bloc/SunBlock/tree/main/Systemd).
Because SunBlockCore-LL is a single unified ASGI app, only **one** service is
needed where the original required two.
