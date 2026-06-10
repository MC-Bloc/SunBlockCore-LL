# SunBlockCore-LL — Agent Handoff Document

> This document is written for a Claude agent (or any developer) who is picking up this project cold.  
> Read this before touching any code.

---

## What This Project Is

SunBlockCore-LL is a real-time solar energy monitoring and admin panel for the SunBlock Project (TAG MC-Bloc, Milieux Institute, Concordia University, Montreal). It:

- Reads telemetry from an Epever MPPT charge controller over RS-485/Modbus
- Persists readings to SQLite at 1 reading/second
- Broadcasts live data to browser clients via Socket.IO
- Serves a single-page Alpine.js admin panel with 7 tabs: Live, Parameters, Energy, Settings, History, Visualize, Logs
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
  _base.html    ← Shared page shell (head/<link> CSS, header, Live tab, Alpine
                   bootstrap, <script> tags). Declares 4 empty Jinja2 blocks:
                   referrer_meta, extra_tabs, live_admin_extras, admin_panels
  admin.html    ← extends _base.html; fills all 4 blocks with the full admin UI
                   (Parameters/Energy/Settings/History/Visualize/Logs panels, login
                   + edit-params modals). Rendered ONLY at /<ADMIN_PATH>
  public.html   ← extends _base.html; overrides nothing — admin blocks render
                   empty, so zero admin markup is ever sent for GET /
  404.html      ← Custom 404 page (dark theme, matching the app)
public/
  css/index.css   ← Extracted CSS (was an inline <style> block)
  js/sunblock.js  ← Extracted Alpine.data('sunblock', ...) component (was an
                     inline <script>; now a same-origin static file)
  vendor/         ← alpine.min.js, socket.io.min.js, plotly.min.js, qrcode.min.js
scripts/
  deploy.sh     ← Ubuntu/systemd deployment automation
  vendor.sh     ← Downloads and pins all frontend vendor assets
  gen_password_hash.py
Systemd/        ← Pre-made systemd unit + launcher script (manual alternative to deploy.sh);
                   see Systemd/README.md
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
| `sunblock_settings.db` | Runtime settings — `settings(key TEXT PK, value TEXT)`, `api_tokens` (hashed bearer tokens for external API access), and `backup_codes` (hashed one-time 2FA recovery codes) |
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

**`backup_codes` schema** (in `sunblock_settings.db`, created lazily by `db._backup_codes_conn()`):
```sql
CREATE TABLE backup_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash TEXT NOT NULL UNIQUE,    -- SHA-256 of the formatted code; raw value never stored
    created_at TEXT NOT NULL,
    used_at TEXT                       -- NULL until consumed (one-time use)
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

**Settings keys:** `read_interval`, `data_man`, `sim_mode`, `token_expire_hours`, `admin_password_hash`, `secret_key` (auto-generated on first run if not supplied via `.env` — see `db.load_settings()`), `totp_secret` and `totp_enabled` (2FA — absent/false until the admin enrolls via Settings)

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
| GET | `/api/logs` | **Yes** (60/min) | Recent application log lines for the Logs tab: `?lines=` (1-2000, default 200) |
| GET | `/api/logs/download` | **Yes** | Download `SunBlockCoreLogs.txt` |
| GET/PATCH | `/api/settings` | **Yes** | Get/update runtime settings |
| DELETE | `/api/settings/{key}` | **Yes** | Reset setting to .env value |
| POST | `/api/settings/password` | Session only | Change admin password |
| GET | `/api/2fa/status` | Session only | `{enabled, backup_codes_remaining}` |
| POST | `/api/2fa/setup` | Session only | Begin 2FA enrollment (pending TOTP secret + QR URI) |
| POST | `/api/2fa/confirm` | Session only | Verify pending secret, activate 2FA, return backup codes |
| POST | `/api/2fa/disable` | Session only | Disable 2FA — requires password + valid code |
| POST | `/api/2fa/backup-codes/regenerate` | Session only | Invalidate + reissue backup codes — requires valid code |
| POST | `/api/login/verify-2fa` | Rate-limited (5/min) | Exchange a pending 2FA challenge + code for a session |
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

Three Jinja2 templates using inheritance — `_base.html` (shared shell) extended by
`admin.html` and `public.html` (see File Map above for what each contains and why
the split exists: the admin template is rendered only at the secret `/<ADMIN_PATH>`
route, and the public template structurally cannot include any admin markup).
Plus two extracted static assets, `public/css/index.css` and `public/js/sunblock.js`
(formerly an inline `<style>` and `<script>` in one 1850-line `index.html`).
Still **no build step** — these are plain files served by FastAPI's `StaticFiles`
mount at `/static`, no bundler/transpiler involved.

- **Alpine.js** for reactivity (vendored at `public/vendor/alpine.min.js`)
- **Socket.IO** for live data (vendored at `public/vendor/socket.io.min.js`)
- **Plotly.js** basic bundle for all charts (vendored at `public/vendor/plotly.min.js`)
- **qrcodejs** for client-side 2FA QR rendering (vendored at `public/vendor/qrcode.min.js`)

The Alpine component (now in `public/js/sunblock.js`) is bootstrapped with
server-side values passed as plain function arguments from `_base.html`:

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

**Critical:** The `x-data` attribute uses single quotes. JSON from `tojson` contains double quotes — if the attribute used double quotes, the JSON would break HTML attribute parsing. Do not change the quoting. Note that `sunblock.js` itself contains **no Jinja interpolation** — it was extracted verbatim and is pure JS; all server-rendered values flow in solely through these `x-data(...)` constructor arguments. Keep it that way — adding `{{ }}` to that file would require it to go back through Jinja2 (defeating the point of serving it as a cacheable static asset).

**Access tiers:** Unauthenticated visitors get `public.html`, which renders only the Live tab (data cards + charts) — the admin tabs/panels/modals don't exist in that response at all (not just hidden). The admin template additionally wraps its tabs in `<template x-if="authed">` so they appear in the DOM only after login; `switchTab()` enforces this client-side too as a UX guard. Real protection is server-side (401 on data endpoints).

Tab panels use `x-show` (not `x-if`) — DOM is retained between switches so charts don't need re-initialisation.

**CSP (no nonce anymore):** Since the JS extraction removed the last inline `<script>` from every template, `_page_response()` no longer mints a per-request nonce — `script-src 'self' 'unsafe-eval'` is sufficient (everything loads from same-origin static files). See "Content Security Policy" in `docs/SECURITY.md` §7 for the current header value and rationale.

---

## Security Architecture (Brief)

- Admin login path is a random slug (`ADMIN_PATH` in `.env`) — never in logs, never in Referer
- Strict CSP (`script-src 'self' 'unsafe-eval'`, `default-src 'self'`, `frame-ancestors 'none'`, ...) — every script loads from a same-origin file (vendored libs + `public/js/sunblock.js`); there is no inline `<script>` left in any template, so injected inline scripts simply have no `'unsafe-inline'`/nonce to satisfy and won't execute
- Rate limiting on login (5/min) and data endpoints (20–60/min)
- `data_directory` validated with `_validate_data_directory()` — resolves symlinks, rejects system paths
- All data endpoints (history, visualize, downloads) require auth
- API bearer tokens (`auth.verify_session_or_token`) let external scripts authenticate without a session cookie — generated/revoked from Settings → API Tokens, hashed with SHA-256 in `api_tokens` (never stored raw), with configurable expiry
- Optional TOTP-based two-factor authentication (`pyotp`) — when enabled, `/api/login` issues a short-lived `sb_2fa_pending` JWT (5 min, `purpose: "2fa_pending"`, never accepted by `verify_session`/`verify_session_or_token`) instead of a real session; `/api/login/verify-2fa` exchanges it for `sb_session` after validating a TOTP code or one-time SHA-256-hashed backup code. Enrollment is two-step (setup → confirm) so a typo'd authenticator never locks the admin out, and disabling 2FA requires both the password and a valid code (mirrors `change_password`'s defense-in-depth, all session-only — never bearer-token-eligible)
- Every write action logged to `SunBlockAdminAudit.txt` with IP

See `docs/SECURITY.md` for the full security model.

---

## Admin Audit Log

`admin_log(action, detail, ip)` in `db.py` writes to `SunBlockAdminAudit.txt`. Call it after every admin action. Format:

```
2026-06-06 10:30:00  [AUDIT]  LOGIN  user=admin  ip=192.168.1.10
```

2FA actions log under their own action names: `2FA_SETUP_STARTED`, `2FA_ENABLED`,
`2FA_DISABLED`, `2FA_DISABLE_FAILED`, `LOGIN_PASSWORD_OK_2FA_PENDING`, `2FA_LOGIN`
(`method=totp|backup_code`), `2FA_CHALLENGE_FAILED`, `BACKUP_CODE_USED`, and
`BACKUP_CODES_REGENERATED` — useful for spotting brute-force attempts against
the second factor or unexpected backup-code consumption.

Falls back to stderr if `DATA_DIRECTORY` is not yet set.

---

## Concurrency Rules

1. **Blocking calls** (Modbus I/O, SQLite writes, history queries) must go through `run_in_executor`. Never `await` a blocking call directly in a route handler.

2. **History and visualize queries** open their own `sqlite3.connect()` per call. They must never use `config.DB_CURSOR` — that cursor is owned by the polling loop.

3. **State mutations** from route handlers (e.g. `config.SIM_MODE = True`) are safe because CPython's GIL protects simple attribute assignments.

---

## Polling Loop & `parse_data()` Gotchas

These exist because of bugs that have already happened once — read before touching `polling_loop` or `hardware.py`.

- **`polling_loop`'s sleep is interval-correcting.** It captures `tick_start = loop.time()`, does all per-tick work, then sleeps only `max(0, READ_INTERVAL - elapsed)`. Don't replace this with a flat `asyncio.sleep(READ_INTERVAL)` — that makes the real cycle time `work_time + READ_INTERVAL` and broadcasts drift later every tick.

- **Error handling in `polling_loop` is asymmetric on purpose.** An exception from `poll_fn` (i.e. `parse_data`/`simulate_data`) is logged and **breaks the loop** — all `solar_data` broadcasts stop silently from that point on. Exceptions from `write_db()` or `sio.emit()` are logged but the loop **continues**. Any code that runs inside `poll_fn` must therefore either succeed or fail in a way that's actually fatal — see the `CPUPowerDraw` guard below for what happens when it doesn't.

- **`CPUPowerDraw` is optional and must stay guarded.** `parse_data()` only shells out to `config.POWER_DRAW_SCRIPT` (from `POWER_DRAW_SCRIPT_ADDR`) if that value is truthy, and wraps the call in `try/except`, defaulting to `0.0` (`float`, matches the `REAL` column). An earlier unguarded `subprocess.run([config.POWER_DRAW_SCRIPT], ...)` with `POWER_DRAW_SCRIPT_ADDR` blank threw `TypeError` on the first poll tick and silently killed the entire polling loop — symptom: "the server runs but isn't broadcasting any events on the socket," with no other error visible. Don't remove the `if config.POWER_DRAW_SCRIPT:` guard or the `try/except`.

- **`check_power_profile()` is cached for 30s** (`_PROFILE_CACHE_TTL`/`_cached_profile`/`_cached_profile_at` module globals in `hardware.py`). It exists because `sudo powerprofilesctl get` costs ~100–500ms (sudo + D-Bus), which at `READ_INTERVAL=1` was the dominant source of multi-second broadcast latency. `set_power_profile()` resets `_cached_profile_at = 0.0` to force a fresh read after a profile change — preserve that if you touch this code.

- **Don't batch `parse_data()`'s 9 `ctrl.get_*()` Modbus calls into bulk `retriable_read_registers()` reads without verifying byte order first.** This was tried (to cut round trips for latency) and reverted — `BYTEORDER_LITTLE_SWAP`'s exact 32-bit byte arrangement for `PVPower`/`BattChargePower`/`LoadPower`/`BattOverallCurrent` isn't derivable from the public `epevermodbus` driver alone, and a guessed formula produced grossly wrong readings (correctness regression, not a crash). If you revisit this: read minimalmodbus's actual `_bytestring_to_long`/`BYTEORDER_LITTLE_SWAP` source from the installed package (`.venv/lib/python3.9/site-packages/minimalmodbus.py`) and validate decoded values against `epevermodbus --portname /dev/ttyACM0 --slaveaddress 1` CLI output before trusting them. See `docs/DEVELOPMENT_HISTORY.md` Session 12.

---

## Known Issues to Fix (Priority Order)

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
| `templates/_base.html` — `x-data` quoting | Single quotes on `x-data` are load-bearing. See above. |
| `templates/_base.html` — block names | `admin.html` overrides blocks by name (`referrer_meta`, `extra_tabs`, `live_admin_extras`, `admin_panels`). Renaming/removing a block in `_base.html` silently breaks the admin template (Jinja2 doesn't error on an override of a non-existent block — it's just ignored, and the content disappears). |
| `public/js/sunblock.js` | Must stay free of Jinja2 `{{ }}` interpolation — see "Frontend Architecture" above. |
| `.env` | Contains `SECRET_KEY`, `ADMIN_PASSWORD_HASH`, and `ADMIN_PATH`. Never commit. |

---

## Testing Approach

There is currently no automated test suite. Manual testing procedure:

1. Start with `SIM_MODE=true DATA_DIRECTORY=./demo_data/`
2. Open `http://localhost:3707` in a browser — verify live data, data cards, and charts visible without login
3. Navigate to `http://localhost:3707/<ADMIN_PATH>` — verify login modal appears
4. Log in; verify all 7 tabs become accessible
5. **History:** load, filter by date, paginate
6. **Visualize:** select variables, set date range, click Plot — verify chart renders
7. **Settings:** change a setting (e.g. `read_interval=2`), restart server, verify it persisted; reset it, verify it returned to `.env` value
8. **Downloads:** CSV, XLSX, SQLite — verify files are downloaded
9. **Logs:** open the Logs tab — verify recent log lines render, "Auto-refresh" toggles a 5s poll, and the `.txt` download link works
10. **Audit log:** check `demo_data/SunBlockAdminAudit.txt` — verify login, settings change, download are all recorded
11. Try setting `data_directory` to `/etc` — verify it returns a 400 and the PATH_REJECTED entry appears in the audit log

---

## Deployment

Production runs on Ubuntu at port 3707 via systemd. `scripts/deploy.sh` writes and enables the unit automatically; `Systemd/` ships the same unit + launcher pre-written for manual installs (see `Systemd/README.md`) — useful when migrating from the original SunBlock project's `SB_RunSunBlockCore`/`SB_RunSunBlockExpress` two-service setup, since this single ASGI app replaces both.

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

# Test the logs endpoint (requires auth — use a session cookie or bearer token)
curl -H "Authorization: Bearer sbll_<token>" "http://localhost:3707/api/logs?lines=50"

# Test custom 404
curl http://localhost:3707/nonexistent-page
```
