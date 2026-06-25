# SunBlockCore-LL — Development History

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

## Session 9 — API Tokens for External Access

### Problem

All authenticated API access required a browser session cookie (`sb_session`). External scripts, monitoring dashboards, and automations have no way to obtain or send a cookie, so the API was effectively unusable from outside the browser.

### Solution: revocable bearer tokens, generated and managed from the admin panel

**Storage decision — new `api_tokens` table inside the existing `sunblock_settings.db`** (not a new database file, not the telemetry DB):
- That database already lives at a fixed location independent of `DATA_DIRECTORY`
- It's already the home for low-write-volume admin/config metadata — auth records belong with it
- Reuses the established `_settings_conn()`-style connection-per-call / `CREATE TABLE IF NOT EXISTS` / try-finally pattern (`db._tokens_conn()`)
- Avoids proliferating SQLite files for a handful of records

**Token format and hashing** — `secrets.token_urlsafe(32)` prefixed `sbll_` (consistent with `SECRET_KEY`/`ADMIN_PATH`/CSP-nonce generation already in the codebase). Only a SHA-256 hash is ever stored (`db._hash_token`); the raw value is returned exactly once, at creation, and cannot be retrieved afterward. SHA-256 — not bcrypt — because tokens are already 256-bit random strings; bcrypt's deliberate slowness defends low-entropy human passwords and would only add latency to every API call here.

**New `db.py` functions** — `create_api_token(name, expires_in_hours)`, `list_api_tokens()`, `revoke_api_token(id)`, `verify_api_token(raw_token)` (validates hash + expiry, lazily deletes expired tokens, records `last_used_at`, returns `ADMIN_USERNAME` or `None`).

**New `auth.py` dependency — `verify_session_or_token`** — tries the JWT session cookie first (browser path, unchanged), then falls back to parsing `Authorization: Bearer <token>` and validating it via `verify_api_token`. Replaces `Depends(verify_session)` on all data/control endpoints (`history`, `visualize`, downloads, `settings` get/patch/reset, power profiles, controller parameters/RTC sync).

**Deliberate carve-out — token management stays session-only.** `POST/GET/DELETE /api/tokens` and `POST /api/settings/password` keep `Depends(verify_session)`. Reasoning: if a leaked token could create new tokens or revoke the admin's own, a single leak could escalate into permanent, unrecoverable persistence. Restricting self-management to the cookie-based session means a leaked token's worst case is data/control access — bounded, and always revocable by the legitimate admin.

**New routes** — `POST /api/tokens` (create; returns the raw token once), `GET /api/tokens` (list metadata only — id, name, created_at, expires_at, last_used_at; never the hash or raw value), `DELETE /api/tokens/{id}` (revoke; takes effect immediately). Each is logged via `admin_log()` (`TOKEN_CREATED`, `TOKEN_REVOKED`).

**Admin panel UI** — new "API Tokens" card in the Settings tab: name + expiry (1 day / 1 week / 30 / 90 / 365 days / never) form, a one-time reveal banner with copy-to-clipboard for the freshly generated token, and a table of existing tokens (name, created, expires, last used, revoke).

### Why this design and not a separate permissions tier

SunBlockCore-LL has exactly one admin account (see `docs/SECURITY.md` — "Single admin account"). Building a scoped/role-based token system would add real complexity (token scopes, permission checks on every route, UI for configuring them) with no second principal to apply it to. Granting tokens session-equivalent access — minus self-management — gets external integrations working with the simplest model that actually matches the deployment reality, while still capping the damage a leak can do.

### Pre-made systemd unit files (`Systemd/`)

Added a `Systemd/` directory at the repo root mirroring the layout and conventions of the original [MC-Bloc/SunBlock/Systemd](https://github.com/MC-Bloc/SunBlock/tree/main/Systemd) scripts (`SB_RunSunBlockCore.service`/`.sh`, `SB_RunSunBlockExpress.service`/`.sh`):

- **`SB_RunSunBlockCore-LL.service`** — systemd unit (`User=pc`, `Restart=on-failure` with burst limiting, `WantedBy=default.target`, `EnvironmentFile=` for `.env`)
- **`SB_RunSunBlockCore-LL.sh`** — launcher script that `cd`s into the repo and execs `.venv/bin/uvicorn sunblock:socket_app`; goes in `/usr/local/bin/`
- **`README.md`** — install/troubleshooting steps in the same style as the original

Because SunBlockCore-LL is a single unified ASGI app (FastAPI + python-socketio), only **one** service is needed where the original SunBlockCore + SunBlockExpress split required two. This is a manual-install alternative to `scripts/deploy.sh` (which generates and installs an equivalent unit automatically) — useful for operators who prefer to wire things up by hand, or who are migrating an existing systemd setup from the original two-service layout. `docs/DEPLOYMENT.md`, `docs/ARCHITECTURE.md`, `docs/AGENT_HANDOFF.md`, and the root `README.md` were all updated to reference it.

---

## Session 10 — SECRET_KEY Auto-Generation + Two-Factor Authentication

Triggered by the operator's plan — a publicly-reachable admin panel that controls real solar hardware is a meaningfully higher-stakes deployment, so two defense-in-depth gaps were closed before launch.

### Part 1 — `SECRET_KEY` is now always randomly generated, never a static placeholder

**Problem:** `config.py` fell back to the literal string `"changeme-secret-key"` if `SECRET_KEY` wasn't set in `.env`. An attacker who knew this well-documented default could forge arbitrary admin JWT sessions outright — this was tracked as **SEC-001 (High)**.

**Fix — persist-once-and-reuse, mirroring the existing `admin_password_hash` pattern exactly:**
- `config.py`: `SECRET_KEY` now defaults to `""` — no static fallback exists at all.
- `db.load_settings()`: after applying `.env` and any persisted value, if `config.SECRET_KEY` is still empty it generates one with `secrets.token_hex(32)` (256 bits), assigns it, persists it via `save_setting("secret_key", ...)`, and logs an informational startup message. Added `"secret_key"` to the `_apply` dict so a previously generated key loads automatically on every subsequent boot.
- `sunblock.py`: removed the now-impossible `if config.SECRET_KEY in ("changeme-secret-key", ""):` startup warning — dead code once the weak default can't exist.

**Why persist instead of regenerating on every restart:** a fresh key on every boot would silently invalidate every admin session — and, with 2FA now in the picture, every in-flight pending-2FA challenge — on every restart. That's a serious, surprising availability problem for an operator who isn't expecting forced re-logins. Persisting the generated key makes it behave exactly like an operator-supplied one: stable across restarts, rotatable on demand by deleting the `secret_key` row from `sunblock_settings.db`.

**Verified:** fresh start generates and persists a 64-char hex key; a simulated restart (re-running `load_settings()`) loads the *identical* key back — sessions survive restarts. SEC-001 is now marked **Resolved** in `docs/SECURITY.md`.

### Part 2 — Two-factor authentication (TOTP)

**Problem:** the admin account had exactly one factor (a password). On a publicly-reachable, hardware-controlling dashboard, a guessed/phished/reused password is a complete compromise.

**Solution — standard TOTP second factor via `pyotp`, with one-time backup recovery codes:**

- **Storage** — `totp_secret`/`totp_enabled` keys in the existing `sunblock_settings.db` `settings` table (mirrors `admin_password_hash` — no env fallback, since 2FA must start disabled until the admin explicitly enrolls); a new `backup_codes` table (`id, code_hash UNIQUE, created_at, used_at` — mirrors `api_tokens`: SHA-256 hashes only, raw codes shown once).
- **Two-step enrollment** — `POST /api/2fa/setup` generates a random base32 secret held *only in memory* (`sunblock._pending_totp_secret`, never written to disk) and returns an `otpauth://` URI for a client-rendered QR code; `POST /api/2fa/confirm {code}` verifies a code against that pending secret *before* persisting anything, then activates 2FA and returns 10 one-time backup codes. This ordering means a typo'd secret or misconfigured app can never lock the admin out — a failed confirmation simply changes nothing.
- **Two-step login** — when `TOTP_ENABLED`, `POST /api/login` no longer issues `sb_session` on a correct password. It instead issues a short-lived (5 min) JWT carrying `purpose: "2fa_pending"` in its own cookie (`sb_2fa_pending`) and responds `{requires_2fa: true}`. The new `POST /api/login/verify-2fa` (rate-limited 5/min, same as `/api/login`) validates that pending token plus a TOTP code or backup code, then issues the real session. **Critical hardening:** `verify_session`, `check_session`, and `verify_session_or_token` were all updated to reject any token carrying a `purpose` claim — without this, a captured pending token could simply be replayed as `sb_session` and skip the second factor entirely.
- **Disable** — `POST /api/2fa/disable {password, code}` requires *both* factors, exactly mirroring `change_password`'s defense-in-depth: a hijacked session alone must not be able to strip the account's strongest protection.
- **Backup codes** — `POST /api/2fa/backup-codes/regenerate {code}` invalidates and reissues all 10; the admin is warned in the application log when ≤2 unused codes remain.
- **Audit logging** — new action names: `2FA_SETUP_STARTED`, `2FA_ENABLED`, `2FA_DISABLED`, `2FA_DISABLE_FAILED`, `LOGIN_PASSWORD_OK_2FA_PENDING`, `2FA_LOGIN` (`method=totp|backup_code`), `2FA_CHALLENGE_FAILED`, `BACKUP_CODE_USED`, `BACKUP_CODES_REGENERATED`.
- **Session-only, never bearer-token-eligible** — all `/api/2fa/*` management routes use `Depends(verify_session)`, exactly like `change_password` and API token management. A leaked bearer token must never be able to touch the second factor.
- **UI** — new "Two-Factor Authentication" card in Settings: status indicator, QR-code enrollment flow (rendered client-side via vendored `qrcodejs`, so the secret never becomes a server-rendered image), one-time backup-code display, regeneration, and a password+code disable form. The login modal gained a second step that prompts for the 2FA code after a correct password.
- **New vendored asset** — `public/vendor/qrcode.min.js` (qrcodejs 1.0.0), added to `scripts/vendor.sh`.
- **New dependency** — `pyotp`, added to `requirements.txt`.

**Recovery procedure** — documented in `docs/SECURITY.md`: an operator with filesystem access can disable 2FA directly via `sqlite3` on `sunblock_settings.db` (same class of operation as the existing password-hash recovery path).

**Verified end-to-end with a live server:** enrollment (setup → confirm with a real generated TOTP code → backup codes returned), full two-step login with a correct code, rejection of an incorrect code, login via a backup code (and rejection of its reuse), disable requiring both password and code (and rejection with a wrong password), and confirmed every step appears correctly in the admin audit log.

---

## Session 11 — Frontend Modularization (Template Split + Asset Extraction)

Triggered by the operator noticing that `templates/index.html` had grown to ~1850 lines after the Session 10 2FA additions and asking whether it could be modularized. Two changes, both purely structural — no behavioural change to the running app (verified live both before and after each step).

### Part 1 — Split into separate public / admin templates via Jinja2 inheritance

**Problem:** a single `index.html` rendered two very different pages (`GET /` vs. the secret admin route) by branching on a server-side `admin_mode` variable, with `{% if admin_mode %}...{% endif %}` guards wrapping ~750 lines of admin-only markup (5 tab panels + login modal + edit-params modal). The operator specifically wanted the public and admin variants to be **separate files**.

**Why inheritance instead of two full copies:** a literal copy-paste split would require maintaining two near-identical ~1000-line files, duplicating the shared CSS, header, Live tab, and the ~700-line Alpine component. Template inheritance gives the requested file-level separation — admin markup now physically lives in its own file, and is provably absent from the public response — without duplicating the shared chrome.

**Implementation:**
- `templates/_base.html` — extracted shared shell (head, header, Live tab/panel, Alpine `x-data` bootstrap, script tags), with the four `{% if admin_mode %}` regions replaced by named empty Jinja2 blocks: `referrer_meta`, `extra_tabs`, `live_admin_extras`, `admin_panels`.
- `templates/admin.html` — `{% extends "_base.html" %}`, overriding all four blocks with the original admin-only content verbatim (the `<meta name="referrer" content="no-referrer">` tag, the 5 admin nav tabs, the live-tab admin toolbar/power-profile card/data-dir warning, and the 5 admin panels + both modals).
- `templates/public.html` — `{% extends "_base.html" %}` with **no overrides at all**; since the base's blocks default to empty, the rendered output structurally cannot contain any admin markup (it's not hidden by CSS/`x-show` — it's never generated or transmitted).
- `sunblock.py` — `index()` now renders `"public.html"`, `_admin_page()` now renders `"admin.html"` (both still pass `admin_mode` in context for Alpine's `initialAdminMode` / `authed`-driven UI behaviour). Old `templates/index.html` deleted.

**Verified:** rendered both templates directly through Jinja2 and confirmed zero admin-marker strings (`PARAMETERS`, `Admin Login`, `Edit Controller Parameters`, `🔓 Logged in`) in the public output vs. all four present in the admin output; then booted a live server and curl'd both routes, confirming 200s and the same marker split over the wire.

### Part 2 — Extract CSS and the Alpine component to static files

**Problem:** even after the template split, `_base.html` still contained the entire page `<style>` block (~170 lines) and the entire `Alpine.data('sunblock', ...)` component (~780 lines) inline — together the bulk of what made the original file unwieldy.

**Why this was safe to do verbatim:** grepped both blocks for Jinja2 `{{ }}`/`{% %}` syntax — neither contains any. The `initial*` values the Alpine component needs are passed in purely as **constructor arguments** from the `x-data='sunblock(...)'` call in `_base.html`, so the component body itself has no dependency on server-side templating.

**Implementation:**
- `public/css/index.css` (168 lines) — the `<style>` block contents, lifted out verbatim. `_base.html` now has `<link rel="stylesheet" href="/static/css/index.css" />`.
- `public/js/sunblock.js` (781 lines) — the `Alpine.data('sunblock', ...)` component plus its supporting constants (`VC_KEYS`, `CHART_VARS`, `PLOTLY_LAYOUT`, `PLOTLY_CONFIG`) and the `alpine:init` listener wrapper, lifted out verbatim. `_base.html` now has `<script src="/static/js/sunblock.js"></script>`. Both assets are served through the existing `app.mount("/static", StaticFiles(directory="public"))`.
- `_base.html` shrank from 1062 → 110 lines as a result.

**Bonus simplification — CSP nonce removal:** the only inline `<script>` left anywhere was the one just extracted, and `_page_response()` existed partly to mint a per-request `secrets.token_urlsafe(16)` nonce solely to authorize that one block (`script-src 'nonce-<n>'` + `<script nonce="...">`). With it gone, `'self'` alone covers script loading (everything is now a same-origin file), so the nonce generation, the `csp_nonce` template-context injection, and the now-unused `import secrets` were all removed from `sunblock.py`. CSP simplified to `script-src 'self' 'unsafe-eval'`. This is a net security simplification, not a weakening — there's no longer any `'nonce-...'`/inline-script exception to forge or leak; injected inline scripts are now unconditionally blocked.

**Verified:** booted a live server; confirmed `/`, the admin route, `/static/css/index.css`, and `/static/js/sunblock.js` all return 200, the page HTML references the new `<link>`/`<script src>` tags, both extracted files contain sane content (`header h1` CSS rule present, `Alpine.data('sunblock'` present in the JS), `/api/data` still works end-to-end, and the simplified CSP header renders correctly with no nonce.

### Part 3 — Follow-up security review of the now-public static assets

The operator asked two follow-up questions, both answered by direct inspection rather than assumption:

1. **"Is `/static/*` exploitable via URLs?"** — Read Starlette 0.49's `StaticFiles.lookup_path` source directly: it resolves both the requested path and the base directory with `os.path.realpath()` and rejects anything whose `os.path.commonpath` doesn't match the base directory — blocking `../` traversal, encoded variants, *and* malicious symlinks (since `realpath` resolves symlink targets before the containment check). Confirmed `directory="public"` resolves correctly to `<project_root>/public` because `scripts/deploy.sh` sets `WorkingDirectory=$PROJECT_DIR` in the systemd unit. Confirmed no symlinks exist under `public/`, the directory contains only intentionally-public frontend assets (no `.env`/`.db`/secrets), Starlette never implements directory listing, and `X-Content-Type-Options: nosniff` is already set globally. Conclusion: not exploitable.
2. **"Do the extracted CSS/JS contain sensitive info or hints about where other secrets live?"** — Grepped both files for secrets/paths/credentials/internal hostnames. Found none — matches for "secret"/"password"/"token" are all client-side variable/form-field names (`twofaSetup: { secret, otpauth_uri }`, `pwForm`, `tokenForm`), not real values. The JS does reveal the full `/api/*` endpoint list, but that's **not new exposure** — it was always inline in the rendered page (visible via View Source / devtools) and always will be for any browser-based SPA; the real protection is server-side auth, not URL secrecy. Notably absent: `ADMIN_PATH`, `SECRET_KEY`, `TOTP_SECRET`, password hashes, DB paths — none of those are ever templated into any page or script.

---

## Session 12 — Polling Loop Reliability & Latency Fixes

Triggered by the operator running on real hardware (Epever controller on `/dev/ttyACM0`) and reporting that `solar_data` socket events weren't being broadcast at all, then — after that was fixed — that readings were arriving 2–3 seconds late instead of every second.

### Problem 1 — No `solar_data` events at all

**Symptom:** server runs, no errors visible to the operator, but no socket clients ever receive `solar_data`.

**Root cause:** `parse_data()` called `subprocess.run([config.POWER_DRAW_SCRIPT], capture_output=True)` unconditionally. With `POWER_DRAW_SCRIPT_ADDR` blank in `.env`, `config.POWER_DRAW_SCRIPT` is `None`, so `subprocess.run([None], ...)` raised `TypeError` on the very first poll. `polling_loop` wraps `poll_fn` in `try/except Exception: await sunblock_log(...); break` — so this exception logged once and then **silently stopped the entire polling loop**, including all future `sio.emit("solar_data", ...)` calls.

**Fix (`hardware.py`, `parse_data`):**
- Only call the subprocess if `config.POWER_DRAW_SCRIPT` is truthy.
- Wrap the call in `try/except Exception`, defaulting `cpu_power = 0.0` on any failure (missing script, non-zero exit, unparsable output).
- `CPUPowerDraw` is now always a `float` (was previously `result.stdout.decode().replace("W", "").strip()` — a `str`), matching the `REAL` column in `solardata`.

### Problem 2 — Readings 2–3 seconds late at `READ_INTERVAL=1`

After Problem 1 was fixed, broadcasts resumed but lagged real time by 2–3 seconds per tick, growing/staying roughly constant rather than catching up.

**Root cause A — `check_power_profile()` on every tick.** It runs `sudo powerprofilesctl get`, which costs ~100–500ms (sudo + D-Bus round trip). At `READ_INTERVAL=1` this alone could account for the bulk of the reported lag.

**Fix (`hardware.py`):** added a 30-second cache (`_PROFILE_CACHE_TTL`, `_cached_profile`, `_cached_profile_at`). `check_power_profile()` returns the cached value if it's less than 30s old; otherwise it shells out and refreshes the cache. `set_power_profile()` resets `_cached_profile_at = 0.0` so a profile change made via the admin panel is reflected on the very next read rather than waiting up to 30s.

**Operator feedback after this fix:** "faster than before but still not per second" — confirming this was a real contributor but not the whole story.

**Root cause B — non-interval-correcting sleep.** `polling_loop` ended each iteration with `await asyncio.sleep(config.READ_INTERVAL)`, run *after* the poll, DB write, and socket emit. Real cycle time was therefore `work_time + READ_INTERVAL`, not `READ_INTERVAL` — every tick drifted later by `work_time`.

**Fix (`sunblock.py`, `polling_loop`):** capture `tick_start = loop.time()` at the top of each iteration; at the end, compute `elapsed = loop.time() - tick_start` and sleep only `max(0.0, config.READ_INTERVAL - elapsed)`. Cycle time now stays at `READ_INTERVAL` as long as per-tick work fits within that budget.

### Attempted and reverted — batching `parse_data()`'s Modbus reads

As a further latency reduction, `parse_data()`'s 9 individual `ctrl.get_*()` calls (each a separate Modbus RTU round trip, FC4) were replaced with 2 bulk `ctrl.retriable_read_registers()` calls (`0x3100`×27 registers and `0x331A`×3 registers), with manual decode helpers (`_swap`, `_long32`, `_s16`) reproducing minimalmodbus's `BYTEORDER_LITTLE_SWAP` 32-bit decoding for `PVPower`, `BattChargePower`, `LoadPower`, and `BattOverallCurrent`.

**Result:** "data doesn't read" — the manually-derived `BYTEORDER_LITTLE_SWAP` formula (`(_swap(hi) << 16) | _swap(lo)`) was wrong (the correct register-pair ordering could not be confirmed without minimalmodbus's actual `_bytestring_to_long` source), producing grossly incorrect values for the four 32-bit fields.

**Resolution:** reverted entirely (`git revert`) back to the 9 individual `get_*()` calls, which are known-correct (validated against `epevermodbus --portname /dev/ttyACM0 --slaveaddress 1` CLI output earlier in this session). Combined with the Decision-7/8 fixes above, this was sufficient to restore ~1Hz broadcast cadence — each `get_*()` call is a fast round trip over a direct USB-serial connection, so 9 sequential calls were not the dominant cost once the per-tick sudo subprocess and the extra full-interval sleep were removed. If round-trip count ever needs reducing again, the byte-order math must be validated against the installed minimalmodbus version's source before trusting decoded values (see `docs/AGENT_HANDOFF.md` → "Polling Loop & `parse_data()` Gotchas").

---

## Summary of All API Endpoints (current)

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/` | No | Public live view (data cards only) |
| GET | `/<ADMIN_PATH>` | No | Admin login entry-point |
| GET | `/api/mode` | No | `{mode: "simulator"\|"live"}` |
| POST | `/api/login` | No (5/min) | Set session cookie, or issue a 2FA challenge cookie + `{requires_2fa: true}` |
| POST | `/api/login/verify-2fa` | No (5/min) | Exchange a pending 2FA challenge + code for a session cookie |
| POST | `/api/logout` | No | Clear session cookie(s) |
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
| POST | `/api/settings/password` | Session only | Change admin password |
| GET | `/api/2fa/status` | Session only | `{enabled, backup_codes_remaining}` |
| POST | `/api/2fa/setup` | Session only | Begin 2FA enrollment (pending secret + QR URI) |
| POST | `/api/2fa/confirm` | Session only | Confirm enrollment, activate 2FA, return backup codes |
| POST | `/api/2fa/disable` | Session only | Disable 2FA (requires password + code) |
| POST | `/api/2fa/backup-codes/regenerate` | Session only | Invalidate + reissue backup codes |
| POST | `/api/tokens` | Session only | Generate an API bearer token (raw value shown once) |
| GET | `/api/tokens` | Session only | List API tokens (metadata only) |
| DELETE | `/api/tokens/{id}` | Session only | Revoke an API token |
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
- ***Yes*** now means `verify_session_or_token` — accepts a session cookie OR `Authorization: Bearer <sbll_...>` API token  
- *Session only* means `verify_session` — cookie required, bearer tokens deliberately rejected (Session 9: prevents a leaked token from escalating to account takeover)
  
- *No* — public endpoint  
- ***Yes*** — requires valid JWT cookie (`verify_session`)  
- *Controller* — requires controller OR sim mode (`require_controller`)  
- ***Yes*** + HW — requires auth AND real hardware (`verify_session` + `require_real_controller`)

---

## Summary of All Files Changed

| File | Sessions | Key changes |
|---|---|---|
| `config.py` | 3, 5, 8, 10 | Module introduced; SETTINGS_DB_NAME, ENV_DEFAULTS, ADMIN_PATH, ADMIN_AUDIT_FILE; SECRET_KEY default removed (now ""), TOTP_SECRET/TOTP_ENABLED runtime state |
| `auth.py` | 3, 4, 5, 9, 10 | Module introduced; SettingsUpdate, PasswordChange, TokenCreateRequest, TwoFACodeRequest, TwoFADisableRequest models; verify_session_or_token dependency; pending-2FA JWT helpers (create/verify), verify_totp_code; "purpose" claim rejected by all session-verifying functions |
| `db.py` | 3, 5, 7, 8, 9, 10 | Module introduced; settings store (now incl. secret_key/totp_secret/totp_enabled + auto-generate-and-persist SECRET_KEY), query_history, query_visualize, admin_log, _validate_data_directory, api_tokens store, backup_codes store (generate/verify-and-consume/count/clear) |
| `hardware.py` | 3, 12 | Extracted from sunblock.py; CPUPowerDraw made optional/guarded (float, defaults to 0.0); 30s cache for check_power_profile()/set_power_profile() |
| `simulator.py` | 2, 3 | Created; extracted to own module |
| `sunblock.py` | 2–12 | Refactored; all routes, security hardening, rate limits, CSP, audit log calls; API token routes; two-step login + full /api/2fa/* route set; removed obsolete SECRET_KEY startup warning; routes now render public.html/admin.html; CSP nonce + `import secrets` removed (Session 11); polling_loop sleep made interval-correcting via loop.time() (Session 12) |
| `templates/index.html` | 4–10 | **Deleted in Session 11** — split into `_base.html`/`admin.html`/`public.html` + extracted `public/css/index.css` + `public/js/sunblock.js`. History (4–10): Settings, History, Visualize tabs; extra charts; auth-gating; CSP nonce; Plotly; API Tokens panel; Two-Factor Authentication panel + two-step login modal; vendored QR rendering |
| `templates/_base.html` | 11 | Created: shared page shell extracted from index.html; declares 4 empty Jinja2 blocks (referrer_meta, extra_tabs, live_admin_extras, admin_panels); 110 lines (was 1062 right after the split, before CSS/JS extraction) |
| `templates/admin.html` | 11 | Created: `{% extends "_base.html" %}`, fills all 4 blocks with the full admin UI; rendered only at /<ADMIN_PATH> |
| `templates/public.html` | 11 | Created: `{% extends "_base.html" %}`, overrides nothing — admin blocks stay empty |
| `templates/404.html` | 8 | Created: custom 404 page |
| `public/css/index.css` | 11 | Created: extracted page styles (verbatim from the old inline `<style>` block, 168 lines) |
| `public/js/sunblock.js` | 11 | Created: extracted Alpine.data('sunblock', ...) component + supporting constants (verbatim from the old inline `<script>`, 781 lines) |
| `sample.env` | 2, 8, 10 | SIM_MODE, ADMIN_PATH, cleanup; SECRET_KEY now documented as optional/auto-generated |
| `scripts/deploy.sh` | early, 8 | systemd deployment; ADMIN_PATH generation; openpyxl |
| `scripts/vendor.sh` | 8, 10 | uPlot → Plotly basic bundle; added qrcodejs for client-side 2FA QR rendering |
| `requirements.txt` | 10 | Added pyotp |
| `public/vendor/qrcode.min.js` | 10 | Vendored qrcodejs 1.0.0 |
| `Systemd/` | 9 | Created: pre-made systemd unit + launcher (`SB_RunSunBlockCore-LL.service`/`.sh`) + README, mirroring MC-Bloc/SunBlock/Systemd conventions |
| `.gitignore` | 8 | Added *.db, demo_data/, *.xlsx |
