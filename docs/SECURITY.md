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

### Authenticated endpoints (require valid JWT cookie)

| Endpoint | Notes |
|---|---|
| `GET /api/data/history` | Historical readings — rate-limited 60 req/min/IP |
| `GET /api/data/visualize/fields` | Field metadata — rate-limited 60 req/min/IP |
| `GET /api/data/visualize` | Time-series query — rate-limited 20 req/min/IP |
| `GET /api/data/download` | SQLite download |
| `GET /api/data/download/csv` | CSV export |
| `GET /api/data/download/xlsx` | XLSX export |
| `GET /api/settings` | Runtime config |
| `PATCH /api/settings` | Mutate settings |
| `DELETE /api/settings/{key}` | Reset to `.env` |
| `POST /api/settings/password` | Change password |

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

In addition, the visualize query has a hard SQL `LIMIT 5_000_000` cap on rows fetched before Python resampling, preventing OOM on very large databases regardless of the rate limit.

---

## 7. Content Security Policy

Every HTML page response is generated by `_page_response()` in `sunblock.py`, which:

1. Generates a cryptographically random nonce with `secrets.token_urlsafe(16)` per request
2. Sets a `Content-Security-Policy` response header:
   ```
   default-src 'self';
   script-src 'self' 'nonce-<nonce>' 'unsafe-eval';
   style-src 'self' 'unsafe-inline';
   img-src 'self' data: blob:;
   connect-src 'self';
   frame-ancestors 'none'
   ```
3. Injects the nonce into the Jinja2 template context; the inline Alpine.js bootstrap script carries `<script nonce="...">` so it executes while injected scripts (which lack the nonce) are blocked

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
| `DOWNLOAD` | Database downloaded (format=sqlite / csv / xlsx) |
| `POWER_PROFILE` | Power profile switched |
| `CONTROLLER_PARAMS_UPDATE` | Controller register write |
| `RTC_SYNC` | RTC synchronised to server time |

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
| `SECRET_KEY` | `.env` | Attacker can forge arbitrary JWT sessions (full admin access) |
| `ADMIN_PASSWORD_HASH` | `.env` / `sunblock_settings.db` | Enables offline brute-force of the admin password |
| `ADMIN_PATH` | `.env` | Reveals the admin login URL; enables targeted login brute-force |
| `ADMIN_USERNAME` | `.env` | Low risk alone; reduces brute-force search space |

### `.env` security

`.env` is in `.gitignore`. `sample.env` contains only placeholder values and is safe to commit.

The `SECRET_KEY` default value (`changeme-secret-key`) is intentionally weak to trigger attention. **It must be replaced before any non-local deployment.** A warning is logged at startup if the default is detected.

### `sunblock_settings.db`

This file can contain the `admin_password_hash` key (after a password change via the panel). Protect it:

```bash
chmod 600 /opt/sunblock/data/sunblock_settings.db
```

---

## 10. Known Security Issues

### SEC-001 — Weak default `SECRET_KEY` *(High)*

**Description:** The default `SECRET_KEY` is `"changeme-secret-key"`. If an operator forgets to set this, an attacker who knows this default can forge valid JWT tokens.

**Mitigation:** Replace with a cryptographically random value:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```
**Status:** Not fixed — deployment configuration responsibility. Server logs a warning at startup if the default is detected.

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

**Mitigation:** Rotate `SECRET_KEY` in `.env` immediately if compromise is suspected. All existing sessions will be invalidated.  
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

- [ ] Replace `SECRET_KEY` with a cryptographically random 32-byte hex value
- [ ] Generate a strong admin password and set `ADMIN_PASSWORD_HASH` in `.env`
- [ ] Generate a random `ADMIN_PATH` slug and keep it private
- [ ] Set `SECURE_COOKIES=true` in `.env`
- [ ] Terminate TLS at a reverse proxy; proxy WebSocket upgrade headers
- [ ] Restrict port 3707 at the firewall to trusted IP ranges
- [ ] Set `chmod 600` on `.env`, `sunblock_settings.db`, `SunBlockAdminAudit.txt`
- [ ] Add a SQLite WAL-mode `PRAGMA` if write rate is increased significantly
- [ ] Create an index on `solardata(Timestamp)` if the dataset exceeds ~5M rows
- [ ] Consider forwarding `SunBlockAdminAudit.txt` to a remote syslog for tamper resistance
