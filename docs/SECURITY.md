# SunBlockCore-LL — Security & Known Issues

---

## Table of Contents

1. [Security Architecture Summary](#1-security-architecture-summary)
2. [Authentication](#2-authentication)
3. [Transport Security](#3-transport-security)
4. [API Access Control](#4-api-access-control)
5. [Input Validation & Path Protection](#5-input-validation--path-protection)
6. [Rate Limiting](#6-rate-limiting)
7. [Content Security Policy](#7-content-security-policy)
8. [Admin Audit Logging](#8-admin-audit-logging)
9. [Secrets Management](#9-secrets-management)
10. [Known Security Issues](#10-known-security-issues)
11. [Known Bugs & Limitations](#11-known-bugs--limitations)
12. [Hardening Checklist for Production](#12-hardening-checklist-for-production)

> **SEC-006** and **SEC-007** appear in section 10 (dependency CVE notes).

---

## 1. Security Architecture Summary

SunBlockCore-LL is designed as an **internal tool** exposed on a local network or behind a reverse proxy. It is not intended to be exposed directly to the public internet without additional hardening (firewall, VPN, or TLS termination proxy).

Current controls:

- bcrypt password hashing (cost factor 12)
- JWT sessions stored in HttpOnly, SameSite=Strict cookies
- API bearer tokens (SHA-256 hashed, configurable expiry, revocable) for external/programmatic access
- Secret admin login path (not guessable by bots)
- Per-request Content Security Policy with nonce — blocks injected scripts
- Per-IP rate limiting on login and all data endpoints
- Pydantic input validation on all JSON request bodies
- `data_directory` path validation — resolves symlinks, blocks system directories
- All write endpoints and all data endpoints are auth-gated
- Admin audit log — every authenticated action persisted with timestamp and IP

---

## 2. Authentication

### Password storage

Admin passwords are stored as bcrypt hashes. The hash is generated offline via `scripts/gen_password_hash.py` and stored in `.env` as `ADMIN_PASSWORD_HASH`. A runtime password change via `POST /api/settings/password` also stores the new hash in `sunblock_settings.db`.

**Cost factor:** bcrypt default (12). A password check takes ~250ms on modern hardware, appropriate for a login endpoint.

### Session tokens

```
JWT payload:  { sub: <ADMIN_USERNAME>, exp: <now + TOKEN_EXPIRE_HOURS> }
Algorithm:    HS256
Signing key:  SECRET_KEY (from .env)
Transport:    HttpOnly cookie, SameSite=Strict
```

`SameSite=Strict` prevents the cookie from being sent on cross-site requests, providing CSRF protection without a separate CSRF token.

`HttpOnly` prevents JavaScript from reading the cookie, mitigating XSS-based token theft.

### API tokens (external/programmatic access)

Browser sessions use cookies, which aren't usable from scripts, cron jobs, or external dashboards. For that, the admin panel (Settings → API Tokens) can generate **bearer tokens**:

```
Generation:   secrets.token_urlsafe(32), prefixed "sbll_"
Storage:      SHA-256 hash only, in api_tokens (sunblock_settings.db) — raw value never persisted
Transport:    Authorization: Bearer <token> header
Expiry:       configurable per token at creation (1 hour – 1 year, or never)
```

**Why a new table in `sunblock_settings.db` rather than a new database file:** that database already lives at a fixed location independent of `DATA_DIRECTORY`, is already the home for low-write-volume admin/config metadata, and reuses the existing connection-management pattern (`_settings_conn`-style helpers in `db.py`). Proliferating SQLite files for a handful of auth records would add operational overhead with no benefit.

**Why SHA-256 instead of bcrypt for tokens:** bcrypt's deliberate slowness defends against brute-forcing *low-entropy* human passwords. API tokens are 256-bit random strings — already far beyond brute-force range — so a fast cryptographic hash is the correct tool; bcrypt would needlessly slow down every API request.

**Why tokens are bearer-equivalent to a session, but cannot manage tokens:** there is only one admin account (see below), so a token is granted the same data/control-plane access as a logged-in session — creating a second permission tier wasn't requested and would add complexity without a second user to apply it to. The one deliberate exception: `POST/GET/DELETE /api/tokens` and `POST /api/settings/password` always require the actual session cookie, never a bearer token. This means a leaked token can be revoked by the admin and cannot be used to mint replacement tokens, change the password, or revoke the admin's own access — bounding the blast radius of a leak to data/control access, not account takeover.

Token validation also updates `last_used_at` so the admin can spot stale or abandoned tokens and revoke them. Every create/revoke is written to the admin audit log (`TOKEN_CREATED`, `TOKEN_REVOKED`).

### Two-factor authentication (TOTP)

Optional second factor for the admin login, built on the standard TOTP algorithm (`pyotp`, RFC 6238) — compatible with any authenticator app (Google Authenticator, Authy, 1Password, etc.). Disabled by default; the admin enrolls from Settings → Two-Factor Authentication.

**Enrollment (two-step, to prevent lockout):**
```
POST /api/2fa/setup    → generates a random base32 secret, kept ONLY in memory
                         (sunblock._pending_totp_secret) and returned with an
                         otpauth:// URI for the QR code. Never written to disk
                         at this stage.
POST /api/2fa/confirm  → verifies a code against the pending secret BEFORE
                         persisting anything. Only on success does the secret
                         get written to sunblock_settings.db (totp_secret,
                         totp_enabled=true) and 10 backup codes generated.
```
Requiring a successful verification before activation means a typo'd secret or a misconfigured authenticator app can never lock the admin out — if confirmation fails, nothing changes and `/setup` can simply be called again.

**Backup codes:** 10 single-use recovery codes (`XXXX-XXXX-XXXX-XXXX`, generated via `secrets.token_hex`), stored as SHA-256 hashes in a new `backup_codes` table (mirrors the `api_tokens` storage rationale — high-entropy random values, fast hash is appropriate, raw value never persisted). Shown to the admin exactly once, at creation/regeneration time. Each is marked `used_at` and rejected on reuse. The admin is warned in the application log when 2 or fewer remain.

**Login flow when 2FA is enabled:**
```
POST /api/login            → password verified, but NO sb_session is issued.
                             Instead: a short-lived (5 min) JWT with
                             purpose: "2fa_pending" is set in its own cookie,
                             sb_2fa_pending. Response: {requires_2fa: true}.
POST /api/login/verify-2fa → validates sb_2fa_pending + a TOTP code or backup
                             code (5 req/min rate limit, same as /api/login).
                             On success: deletes sb_2fa_pending, issues the
                             real sb_session cookie.
```

**Why a separate "purpose"-tagged token rather than a partial session:** `verify_session`, `check_session`, and `verify_session_or_token` all explicitly reject any JWT carrying a `purpose` claim — so even if an attacker captured `sb_2fa_pending` (e.g. via a misconfigured proxy log), replaying it as `sb_session` is rejected outright. The pending token can only ever be exchanged through the one endpoint that knows how to validate the second factor.

**Disabling 2FA — defense in depth:** `POST /api/2fa/disable` requires *both* the current password and a valid TOTP/backup code, exactly mirroring `change_password`. This is deliberate: a hijacked browser session (e.g. via XSS, or a forgotten logged-in session on a shared machine) should not by itself be able to strip the account's strongest protection.

**Session-only, never bearer-token-eligible:** all `/api/2fa/*` management endpoints use `Depends(verify_session)`, exactly like `change_password` and API token management — a leaked bearer token must never be able to touch the account's second factor or backup codes.

**Recovery if locked out (no backup codes, no authenticator access):** 2FA state lives entirely in `sunblock_settings.db`. An operator with filesystem/SSH access can disable it directly:
```bash
sqlite3 /opt/sunblock/data/sunblock_settings.db \
  "UPDATE settings SET value='false' WHERE key='totp_enabled'; \
   DELETE FROM settings WHERE key='totp_secret'; \
   DELETE FROM backup_codes;"
sudo systemctl restart sunblock
```
This is the same class of operation as the existing `admin_password_hash` recovery path — direct DB access is already required to recover from a lost admin password, so this adds no new attack surface, only a documented procedure for a new feature.

### Secret admin path

The admin login page is registered at a random URL slug set by `ADMIN_PATH` in `.env`. The server:

- Returns a 404 for any path not matching the slug (bots cannot enumerate it)
- Never writes the slug to logs — only `"Admin route registered (path configured in ADMIN_PATH)."` is logged
- Injects `<meta name="referrer" content="no-referrer">` on the admin page so the slug cannot leak via the HTTP Referer header to linked resources

If `ADMIN_PATH` is not set, the admin login page is unreachable and a warning is logged at startup.

### Single admin account

There is exactly one admin account. The username is set via `ADMIN_USERNAME` (default: `admin`). Multi-user or role-based access is not implemented.

---

## 3. Transport Security

### TLS / HTTPS

TLS is **not handled by SunBlockCore-LL itself**. For production:
- Terminate TLS at a reverse proxy (nginx, Caddy)
- Set `SECURE_COOKIES=true` in `.env` so cookies carry the `Secure` flag

Without TLS, credentials and session cookies are transmitted in plaintext. **Do not expose port 3707 directly to the internet without TLS.**

### WebSocket

Socket.IO upgrades from the same HTTP connection. TLS termination at the proxy covers WebSocket traffic automatically if the proxy is configured to pass `Upgrade` and `Connection` headers.

---

## 4. API Access Control

### Public endpoints (no authentication required)

| Endpoint | Rationale |
|---|---|
| `GET /` | Public live view — data cards and rolling charts only. The server renders this page with `admin_mode=False`, so **no admin HTML, login modal, or control tabs are present in the HTML source**. |
| `GET /api/mode` | Non-sensitive status |
| `GET /api/data` | Live readings — public by design |
| `GET /api/auth/status` | Returns only a boolean |

### Authenticated endpoints (session cookie OR API bearer token — `verify_session_or_token`)

| Endpoint | Notes |
|---|---|
| `GET /api/data/history` | Historical readings — rate-limited 60 req/min/IP |
| `GET /api/data/visualize/fields` | Field metadata — rate-limited 60 req/min/IP |
| `GET /api/data/visualize` | Time-series query — rate-limited 20 req/min/IP |
| `GET /api/data/download` | SQLite download |
| `GET /api/data/download/csv` | CSV export |
| `GET /api/data/download/xlsx` | XLSX export |
| `GET /api/logs` | Recent application log lines for the Logs tab — rate-limited 60 req/min/IP |
| `GET /api/logs/download` | Download the full `SunBlockCoreLogs.txt` |
| `GET /api/settings` | Runtime config |
| `PATCH /api/settings` | Mutate settings |
| `DELETE /api/settings/{key}` | Reset to `.env` |
| `POST/PUT /api/performance-mode`, `/api/power-saver-mode`, `/api/balanced`, `/api/controller/parameters`, `/api/controller/rtc/sync` | Hardware control — also gated by `require_real_controller` |

### Session-only endpoints (`verify_session` — bearer tokens rejected)

| Endpoint | Why session-only |
|---|---|
| `POST /api/settings/password` | Sensitive account change — requires an interactive, re-authenticatable session |
| `POST /api/tokens`, `GET /api/tokens`, `DELETE /api/tokens/{id}` | Token management — prevents a leaked token from minting replacements or revoking others (see "API tokens" above) |

### Hardware-gated endpoints

`require_real_controller` blocks write operations in simulator mode, preventing accidental hardware configuration via a simulated session.

### OpenAPI docs

FastAPI's `/docs`, `/redoc`, and `/openapi.json` endpoints are disabled at app initialisation (`docs_url=None`, `redoc_url=None`, `openapi_url=None`). The admin route is also registered with `include_in_schema=False`.

---

## 5. Input Validation & Path Protection

All JSON request bodies are validated by Pydantic models before any handler code runs. Invalid types, out-of-range values, and missing required fields return `422 Unprocessable Entity` before reaching business logic.

Additional validation in handlers:

- `read_interval`: must be 1–3600
- `token_expire_hours`: must be 1–720
- `new_password`: must be at least 8 characters
- Settings keys for reset: validated against a whitelist (`_restorable` set)
- Visualize `sample`: 1–3600 (prevents trivial DoS via tiny step values)
- Visualize `smooth`: 0–300

### `data_directory` path manipulation protection

`apply_data_directory()` in `db.py` calls `_validate_data_directory()` before creating any directory or changing any config value. The validator:

1. Resolves the submitted path to an absolute path with `os.path.abspath()`
2. Expands all symlinks with `os.path.realpath()` — this collapses `/home/pi/../../etc/` to `/etc` before checking
3. Checks the resolved path against a blocklist of system-critical prefixes:
   `/etc`, `/bin`, `/sbin`, `/usr/bin`, `/usr/sbin`, `/usr/local/bin`, `/usr/local/sbin`,
   `/sys`, `/proc`, `/dev`, `/run`, `/boot`, `/root`, `/lib`, `/lib64`, `/lib32`,
   `/System`, `/Library`, `/private/etc`, `/private/var/db`, `/private/var/root`
4. Rejects the filesystem root (`/`)

Any attempt to set `data_directory` to a blocked path is rejected with a `400` response and an entry written to the admin audit log (`PATH_REJECTED`).

### SQL injection

History and visualize queries use parameterised SQLite statements (`?` placeholders) for all user-controlled values. The only interpolated strings are `DB_TABLE_NAME` and column names, both of which are validated against the `VIZ_FIELD_META` allowlist before use.

---

## 6. Rate Limiting

All rate limits are per-IP, enforced by `slowapi`. Exceeding a limit returns `429 Too Many Requests`.

| Endpoint | Limit | Rationale |
|---|---|---|
| `POST /api/login` | 5 / minute | Brute-force protection |
| `GET /api/data/history` | 60 / minute | Prevents automated scraping / DB hammering |
| `GET /api/data/visualize/fields` | 60 / minute | Lightweight endpoint; high limit for UX |
| `GET /api/data/visualize` | 20 / minute | Heavy DB scan (up to 5M rows); lower cap prevents DoS |
| `GET /api/logs` | 60 / minute | Tail-read of a capped ~1MB window; high limit supports auto-refresh polling |

In addition, the visualize query has a hard SQL `LIMIT 5_000_000` cap on rows fetched before Python resampling, preventing OOM on very large databases regardless of the rate limit.

---

## 7. Content Security Policy

Every HTML page response is generated by `_page_response()` in `sunblock.py`, which attaches this `Content-Security-Policy` response header:
```
default-src 'self';
script-src 'self' 'unsafe-eval';
style-src 'self' 'unsafe-inline';
img-src 'self' data: blob:;
connect-src 'self';
frame-ancestors 'none'
```

**`script-src 'self'` — no nonce needed.** Earlier, the page included one inline `<script>` (the Alpine component), so `_page_response()` minted a fresh `secrets.token_urlsafe(16)` nonce on every request and stamped it onto both the CSP header (`'nonce-<n>'`) and the script tag (`<script nonce="...">`) — only a script carrying the matching nonce could execute, blocking injected inline scripts. That component has since been extracted to `public/js/sunblock.js` and is now loaded via `<script src="/static/js/sunblock.js">` — a same-origin file. **There is no inline `<script>` left in any template** (verified across all three: `_base.html`, `admin.html`, `public.html`), so `'self'` alone authorizes every script the app loads (vendored libs + `sunblock.js`), and the nonce machinery was removed as unnecessary complexity. Net effect: simpler code, and arguably *tighter* protection — there's no `'nonce-...'` exception for scripts at all anymore, so even a successfully-injected inline `<script>` (e.g. via a hypothetical stored-XSS bug elsewhere) would be unconditionally blocked by the browser, with no nonce to forge or leak.

`'unsafe-eval'` remains required because Alpine.js evaluates `x-data`/`x-on` expressions via `new Function()` internally. `style-src 'unsafe-inline'` remains required because Alpine's `:style` bindings set inline `style=""` attributes on elements — this is unrelated to (and unaffected by) the script-side nonce removal.

`'unsafe-eval'` is required because Alpine.js uses `new Function()` to evaluate `x-data` and `x-on` expressions. This cannot be removed without replacing Alpine.

Additional HTTP security headers set on all responses:

| Header | Value |
|---|---|
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `X-XSS-Protection` | `1; mode=block` |
| `Permissions-Policy` | `geolocation=(), microphone=(), camera=()` |

---

## 8. Admin Audit Logging

Every authenticated write action is appended to `<DATA_DIRECTORY>/SunBlockAdminAudit.txt`. Each line is space-delimited:

```
2026-06-06 10:30:00  [AUDIT]  LOGIN  user=admin  ip=192.168.1.10
2026-06-06 10:30:15  [AUDIT]  SETTINGS_CHANGE  read_interval=5  ip=192.168.1.10
2026-06-06 10:31:00  [AUDIT]  PATH_REJECTED  attempted=/etc  reason=...  ip=192.168.1.10
```

**Events logged:**

| Action | Trigger |
|---|---|
| `LOGIN` | Successful authentication |
| `LOGIN_FAILED` | Failed authentication attempt |
| `LOGOUT` | Session cookie cleared |
| `SETTINGS_CHANGE` | Any setting mutated via `PATCH /api/settings` |
| `PATH_REJECTED` | `data_directory` blocked by path validator |
| `RESET_SETTING` | Setting reverted to `.env` value |
| `PASSWORD_CHANGE` | Successful admin password change |
| `PASSWORD_CHANGE_FAILED` | Incorrect current password supplied |
| `DOWNLOAD` | Database or log file downloaded (format=sqlite / csv / xlsx / log) |
| `POWER_PROFILE` | Power profile switched |
| `CONTROLLER_PARAMS_UPDATE` | Controller register write |
| `RTC_SYNC` | RTC synchronised to server time |
| `LOGIN_PASSWORD_OK_2FA_PENDING` | Password correct; 2FA challenge issued (not yet logged in) |
| `2FA_LOGIN` | Second factor accepted — `method=totp` or `method=backup_code` |
| `2FA_CHALLENGE_FAILED` | Incorrect TOTP/backup code at login, setup confirmation, disable, or backup-code regeneration |
| `2FA_SETUP_STARTED` | Enrollment began — pending secret generated |
| `2FA_ENABLED` | Enrollment confirmed — 2FA now active |
| `2FA_DISABLED` | 2FA turned off (password + code both verified) |
| `2FA_DISABLE_FAILED` | Disable attempt rejected — `reason=bad_password` or `reason=bad_code` |
| `BACKUP_CODE_USED` | A one-time backup code was consumed to log in |
| `BACKUP_CODES_REGENERATED` | All backup codes invalidated and replaced |

The audit log falls back to stderr if `DATA_DIRECTORY` is not yet configured, so no events are silently lost during the initial setup flow.

**Protecting the audit log:**

```bash
chmod 600 /opt/sunblock/data/SunBlockAdminAudit.txt
```

The audit log is append-only at the application level. For tamper-evident logging in high-security contexts, consider shipping it to an external syslog service.

---

## 9. Secrets Management

### What must be kept secret

| Secret | Location | Risk if leaked |
|---|---|---|
| `SECRET_KEY` | `.env` (optional) / auto-generated into `sunblock_settings.db` | Attacker can forge arbitrary JWT sessions (full admin access) |
| `ADMIN_PASSWORD_HASH` | `.env` / `sunblock_settings.db` | Enables offline brute-force of the admin password |
| `ADMIN_PATH` | `.env` | Reveals the admin login URL; enables targeted login brute-force |
| `ADMIN_USERNAME` | `.env` | Low risk alone; reduces brute-force search space |
| API tokens (`sbll_...`) | Shown once at creation; never persisted in raw form | Grants the same data/control-plane access as an admin session until revoked or expired — treat like a password |
| `totp_secret` | `sunblock_settings.db` only (never shown again after enrollment) | Lets an attacker generate valid 2FA codes, defeating the second factor entirely |
| 2FA backup codes | Shown once at creation/regeneration; only SHA-256 hashes persisted | Each is a one-time bypass of the second factor — treat like a list of one-time passwords |

### `.env` security

`.env` is in `.gitignore`. `sample.env` contains only placeholder values and is safe to commit.

`SECRET_KEY` has no static default — if it's left blank in `.env`, the server generates a cryptographically random 32-byte key on first startup and persists it to `sunblock_settings.db` (see SEC-001 — now resolved).

### `sunblock_settings.db`

This file can contain the `admin_password_hash`, `secret_key`, and `totp_secret`/`totp_enabled` keys (the most sensitive values in the system — together they can grant full forged sessions and defeat 2FA), plus the `api_tokens` and `backup_codes` tables (SHA-256 hashes only — never raw values). Protect it:

```bash
chmod 600 /opt/sunblock/data/sunblock_settings.db
```

---

## 10. Known Security Issues

### SEC-001 — Weak default `SECRET_KEY` *(Resolved)*

**Description:** Previously, `SECRET_KEY` fell back to the static placeholder `"changeme-secret-key"` if an operator forgot to set it in `.env`. An attacker who knew this default could forge valid JWT admin sessions outright.

**Fix:** `config.py` no longer has *any* static fallback — `SECRET_KEY` defaults to `""`. At startup, `db.load_settings()` checks `config.SECRET_KEY`; if it's still empty after `.env` and persisted-settings are loaded, it generates one with `secrets.token_hex(32)` (256 bits of entropy) and immediately persists it to the `settings` table in `sunblock_settings.db` via `save_setting("secret_key", ...)` — exactly mirroring how `admin_password_hash` is generated-once-and-stored.

**Why persist instead of regenerating every boot:** A fresh key on every restart would silently invalidate every admin session (and, once 2FA ships, any in-flight pending-2FA challenge tokens) on every service restart — a serious availability/UX problem for an operator who isn't expecting it. Persisting the generated key means it behaves exactly like an operator-supplied key: stable across restarts, rotatable by deleting the `secret_key` row from `sunblock_settings.db` (forces regeneration on next start, invalidating all sessions — useful if compromise is suspected).

**Status:** Fixed — no operator action required. Setting `SECRET_KEY` explicitly in `.env` remains supported (useful for multi-instance deployments that must share a signing key, or to pin a known value); note that, per the usual settings-priority chain, a value already persisted in `sunblock_settings.db` takes precedence over `.env` at runtime — delete the persisted `secret_key` row if you need an `.env` value to take effect.

---

### SEC-002 — No CSRF protection on state-changing endpoints *(Low)*

**Description:** `SameSite=Strict` is the primary CSRF defence. This is effective for modern browsers. However, if `SECURE_COOKIES=false` (the default for HTTP deployments), the `Secure` flag is absent.

**Mitigation:** Use `SECURE_COOKIES=true` behind HTTPS. A dedicated CSRF token is not currently implemented.  
**Status:** Accepted risk for local deployments.

---

### SEC-003 — Single admin account with no account lockout *(Low)*

**Description:** The rate limiter (5 req/min on login) slows brute-force attempts but does not lock the account after N failures. Failed attempts are recorded in the audit log.

**Mitigation:** Use a strong password (>16 chars, random). Restrict network access at the firewall.  
**Status:** Accepted risk for internal deployments.

---

### SEC-004 — JWT secret is symmetric (HS256) *(Informational)*

**Description:** HS256 uses the same key for signing and verification. If `SECRET_KEY` is ever exposed, all sessions can be forged. There is no mechanism to rotate the key without invalidating all existing sessions.

**Mitigation:** To rotate immediately if compromise is suspected: delete the `secret_key` row from `sunblock_settings.db` (`sqlite3 sunblock_settings.db "DELETE FROM settings WHERE key='secret_key'"`) and restart — the server generates and persists a fresh random key automatically. Setting `SECRET_KEY` explicitly in `.env` also works but remember the persisted value normally takes precedence, so the old row must still be cleared first. All existing sessions will be invalidated either way.  
**Status:** Accepted. RS256 is not warranted for a single-admin system.

---

### SEC-005 — `'unsafe-eval'` required in CSP *(Low)*

**Description:** Alpine.js uses `new Function()` to evaluate `x-data` / `x-on` expressions, which requires `'unsafe-eval'` in `script-src`. This weakens the protection against certain XSS payloads that rely on `eval()`.

**Mitigation:** The nonce-based CSP still blocks injected `<script>` tags (which lack the nonce). `'unsafe-eval'` only affects existing inline evaluation contexts. Replacing Alpine.js with a compiled framework would remove this requirement.  
**Status:** Accepted.

---

### SEC-006 — python-jose CVE-2024-33663 / CVE-2024-33664 *(Informational — not affected)*

**Description:** CVE-2024-33663 and CVE-2024-33664 describe an algorithm-confusion attack in `python-jose` that allows an attacker to forge tokens by confusing an RSA public key with an HMAC symmetric key.

**SunBlockCore-LL is not affected.** The only algorithm used here is `HS256` (symmetric HMAC-SHA256). There are no RSA/EC key pairs in the codebase and `python-jose` is never called with an asymmetric key object. The vulnerability requires the server to accept a choice of algorithm from the client, which `python-jose`'s `decode()` call with an explicit `algorithms=["HS256"]` list prevents.

**Status:** Informational only. No action required.

---

### SEC-007 — Python 3.9 CVE backlog in transitive dependencies *(Low)*

**Description:** Some upstream packages (notably `python-dotenv` and `starlette`, which is a transitive dependency of `fastapi`) have released versions that patch low-severity CVEs but those versions require Python ≥ 3.10. Because SunBlockCore-LL targets Raspberry Pi OS (Bullseye/Bookworm) and Ubuntu 22.04 LTS, which ship Python 3.9–3.10, pinning to the latest version may not always be possible across all target platforms.

**Mitigation:** Run on a platform with Python ≥ 3.10 when possible. Keep dependencies updated with `pip install -r requirements.txt --upgrade`. The reported CVEs in `python-dotenv` and `starlette` at the time of writing are informational or low severity (output truncation, header parsing edge cases) and not exploitable in SunBlockCore-LL's threat model (trusted local network, no untrusted `.env` loading).

**Status:** Known limitation. Monitor `pip-audit` output on each deployment.

---

## 11. Known Bugs & Limitations

### BUG-001 — SQLite concurrent write contention under high DATA_MAN load *(Low)*

**Description:** The polling loop holds a persistent write cursor (`DB_CURSOR`). Under extremely high read query volume (many concurrent history/visualize requests), SQLite's default journal mode (DELETE) may produce `database is locked` errors on read queries.

**Workaround:** Enable WAL mode:
```python
config.DB_CONNECTION.execute("PRAGMA journal_mode=WAL")
```
**Status:** Not fixed. Not a problem at the default 1-second poll rate.

---

### BUG-002 — No index on `solardata.Timestamp` *(Medium for large datasets)*

**Description:** Date-range queries perform a full table scan. Above ~10M rows this becomes perceptibly slow.

**Fix:**
```sql
CREATE INDEX IF NOT EXISTS idx_solardata_ts ON solardata(Timestamp);
```
**Status:** Not fixed. Acceptable at current data volumes.

---

### BUG-003 — `POLLING_ACTIVE` is not formally thread-safe *(Very Low)*

**Description:** `config.POLLING_ACTIVE` is a plain Python `bool`. CPython's GIL makes simple assignments safe in practice, but it is not formally thread-safe.

**Fix:** Replace with `asyncio.Event`.  
**Status:** Not fixed. Safe under CPython.

---

### LIMIT-001 — No multi-controller support

The system supports exactly one Epever controller on one serial port.

---

### LIMIT-002 — No alerting

There is no threshold-based alerting (battery low, overtemperature, etc.). All monitoring is passive.

---

### LIMIT-003 — Simulator settling after restart

`_SimState` starts from anchor-table baselines on each server restart, not from the last real reading. A brief (~30 second) settling period occurs on restart.

---

### LIMIT-004 — Audit log is append-only, not tamper-evident

The audit log is a plain text file. A compromised admin account could truncate or overwrite it. For high-security contexts, forward the log to an external syslog service.

---

## 12. Hardening Checklist for Production

Before exposing SunBlockCore-LL outside a trusted local network:

- [x] `SECRET_KEY` — no action needed; the server auto-generates and persists a random 256-bit key on first run (see SEC-001)
- [ ] Generate a strong admin password and set `ADMIN_PASSWORD_HASH` in `.env`
- [ ] **Enable two-factor authentication** (Settings → Two-Factor Authentication) — strongly recommended for any deployment reachable beyond a fully trusted network, since this dashboard controls real solar hardware. Save the backup codes somewhere safe and offline.
- [ ] Generate a random `ADMIN_PATH` slug and keep it private
- [ ] Set `SECURE_COOKIES=true` in `.env`
- [ ] Terminate TLS at a reverse proxy; proxy WebSocket upgrade headers
- [ ] Restrict port 3707 at the firewall to trusted IP ranges
- [ ] Set `chmod 600` on `.env`, `sunblock_settings.db`, `SunBlockAdminAudit.txt`
- [ ] Give each external integration its own named API token with the shortest expiry that's practical, and revoke tokens that are no longer in use (Settings → API Tokens)
- [ ] Add a SQLite WAL-mode `PRAGMA` if write rate is increased significantly
- [ ] Create an index on `solardata(Timestamp)` if the dataset exceeds ~5M rows
- [ ] Consider forwarding `SunBlockAdminAudit.txt` to a remote syslog for tamper resistance
