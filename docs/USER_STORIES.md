# SunBlockCore-LL — User Stories

> Covers all implemented features as of the current codebase.  
> Format: As a [role], I want to [action], so that [benefit].

---

## Roles

| Role | Description |
|---|---|
| **Viewer** | Anyone with network access to the admin panel. Can see live data and history. Cannot change anything. |
| **Admin** | Authenticated operator. Can modify settings, parameters, and power profiles. |
| **Operator** | Person deploying or maintaining the server. Interacts via `.env`, CLI, and deployment scripts. |
| **Developer** | Person building or extending SunBlockCore-LL. |

---

## Live Monitoring

**US-001**  
As a **Viewer**, I want to see the current battery percentage, voltage, temperature, charge power, and current on a live-updating dashboard, so that I can understand the state of the battery at a glance.

**US-002**  
As a **Viewer**, I want to see the current solar panel voltage, current, and power, so that I can understand how much energy the panels are generating right now.

**US-003**  
As a **Viewer**, I want to see the current load power, CPU power draw, connected users, and last update timestamp, so that I have a complete system picture.

**US-004**  
As a **Viewer**, I want to see a rolling 5-minute chart of battery percentage and PV power, so that I can spot short-term trends without reading individual numbers.

**US-005**  
As a **Viewer**, I want the dashboard to update automatically every second without refreshing the page, so that I get a real-time view with no manual action.

**US-006**  
As a **Viewer**, I want a clear connected/disconnected indicator in the header, so that I immediately know if the live feed is active or has dropped.

**US-007**  
As a **Viewer**, I want a visible "SIM" badge in the header when the system is running in simulator mode, so that I am not confused by synthetic data into thinking I am seeing real hardware readings.

---

## Historical Data

**US-008**  
As a **Viewer**, I want to browse historical solar readings from the database in a table, so that I can review past performance without querying SQLite directly.

**US-009**  
As a **Viewer**, I want to filter the history table by a date range (from / to), so that I can focus on a specific period of interest.

**US-010**  
As a **Viewer**, I want to choose how many rows to load per page (50, 100, 250, 500), so that I can balance detail and page load time.

**US-011**  
As a **Viewer**, I want to sort the history table with newest-first or oldest-first ordering, so that I can approach an incident from either direction.

**US-012**  
As a **Viewer**, I want Prev / Next pagination buttons with a "Showing X–Y of N records" summary, so that I can navigate large datasets without losing my place.

**US-013**  
As a **Viewer**, I want the History tab to auto-load the most recent 100 rows when I first open it, so that I immediately see recent data without having to click Load.

---

## Authentication

**US-014**  
As an **Admin**, I want to log in with a username and password from the admin panel, so that I can access protected operations.

**US-015**  
As an **Admin**, I want my session to persist across page refreshes via an HttpOnly cookie, so that I don't have to log in every time.

**US-016**  
As an **Admin**, I want to log out explicitly, so that I can revoke my session when using a shared computer.

**US-017**  
As an **Admin**, I want my session to expire automatically after a configurable number of hours, so that an unattended session cannot be hijacked indefinitely.

**US-018**  
As an **Admin**, I want the login endpoint to be rate-limited, so that brute-force attacks against my password are slowed.

---

## Two-Factor Authentication

**US-069**  
As an **Admin**, I want to enable two-factor authentication using a standard authenticator app (Google Authenticator, Authy, 1Password, etc.), so that a stolen or guessed password alone cannot grant access to a dashboard that controls real solar hardware.

**US-070**  
As an **Admin**, I want enrollment to require me to prove I can already generate valid codes before 2FA is switched on, so that a typo or misconfigured app can never lock me out of my own account.

**US-071**  
As an **Admin**, I want to be shown a set of one-time backup codes when I enable 2FA, so that I can still get in if I lose my phone or authenticator app.

**US-072**  
As an **Admin**, when 2FA is enabled, I want the login flow to ask for my password first and then for a current code (or a backup code) before granting access, so that both factors are independently required — not just checked as an afterthought.

**US-073**  
As an **Admin**, I want to be able to regenerate my backup codes (invalidating the old ones) by proving I still have access to my authenticator, so that I can recover from a situation where my saved codes were exposed or used up.

**US-074**  
As an **Admin**, I want disabling 2FA to require both my current password and a valid code, so that a hijacked browser session alone can't strip away the account's strongest protection.

**US-075**  
As an **Operator**, I want every 2FA-related event (enrollment, enable, disable, successful/failed challenges, backup code use) written to the admin audit log with a timestamp and IP, so that I can detect attempts to brute-force or bypass the second factor.

---

## Settings Management

**US-019**  
As an **Admin**, I want to change the hardware polling interval (in seconds) at runtime from the admin panel without restarting the server, so that I can tune data resolution without downtime.

**US-020**  
As an **Admin**, I want to toggle data management (database writes) on or off at runtime, so that I can stop collecting data during maintenance without stopping the server.

**US-021**  
As an **Admin**, I want to toggle simulator mode on or off at runtime, so that I can switch between live hardware and synthetic data for testing without restarting.

**US-022**  
As an **Admin**, I want to change the session token expiry duration at runtime, so that I can enforce shorter or longer sessions depending on the security context.

**US-023**  
As an **Admin**, I want all settings changes to persist across server restarts, so that my configuration is not lost when the server is updated or rebooted.

**US-024**  
As an **Admin**, I want a reset button next to each setting that restores it to the value defined in `.env`, so that I have a reliable rollback point without editing files.

**US-025**  
As an **Admin**, I want each setting's reset button to show the current `.env` value as a hint (e.g. `env: 1s`, `env: on`), so that I know what the reset target will be before clicking.

**US-026**  
As an **Admin**, I want to change the admin password from the admin panel, so that I can rotate credentials without SSH access to the server.

---

## Power Profile Management

**US-027**  
As an **Admin**, I want to switch the system between Performance, Balanced, and Power Saver profiles from the live dashboard, so that I can tune power draw to match current conditions.

**US-028**  
As a **Viewer**, I want to see which power profile is currently active, so that I understand the current operating mode of the system.

---

## Controller Parameters

**US-029**  
As an **Admin**, I want to view all current battery configuration parameters (type, capacity, charging mode, voltage thresholds), so that I can verify the controller is configured correctly.

**US-030**  
As an **Admin**, I want to edit battery capacity, temperature compensation, and all voltage threshold registers from the admin panel, so that I can tune the controller without a Windows configuration tool.

**US-031**  
As a **Viewer**, I want to see the controller status (temperatures, load voltage/current, day/night state, RTC), so that I have full operational visibility.

**US-032**  
As an **Admin**, I want to sync the controller's real-time clock to the server's current time, so that the controller's logged timestamps stay accurate.

---

## Energy Statistics

**US-033**  
As a **Viewer**, I want to see today's generated and consumed energy in kWh, so that I can track daily solar yield.

**US-034**  
As a **Viewer**, I want to see this month's and this year's energy totals, so that I can assess medium and long-term performance.

**US-035**  
As a **Viewer**, I want to see all-time totals and today's PV voltage min/max and battery voltage min/max, so that I can detect anomalies.

---

## Simulator / Development

**US-036**  
As a **Developer**, I want to run the server in simulator mode with no hardware attached, so that I can develop and test the UI and API on any machine.

**US-037**  
As a **Developer**, I want the simulator to produce data that follows realistic solar curves (zero at night, peak at noon, smooth transitions), so that the UI looks plausible during demos.

**US-038**  
As a **Developer**, I want simulated data to be based on real recorded values from the actual deployment site, so that baselines, noise levels, and units match production.

---

## Operations / Deployment

**US-039**  
As an **Operator**, I want a single shell script that installs dependencies, creates the systemd service, and starts the server, so that I can deploy a fresh instance in one command.

**US-040**  
As an **Operator**, I want all secrets (password hash, secret key, controller port) to be in a `.env` file that is not committed to version control, so that credentials are not exposed in the repository.

**US-041**  
As an **Operator**, I want the data directory (DB files, logs) to be configurable via an environment variable, so that I can point it to a mounted drive or a non-default path.

**US-042**  
As an **Operator**, I want a `sample.env` committed to the repository that documents every available environment variable with sensible defaults, so that new deployments have a clear starting template.

---

## External Integrations

**US-043**  
As a **Developer** building an external dashboard, I want to connect a Socket.IO client to the server and listen for `solar_data` events, so that I can receive live readings in any environment without polling REST.

**US-044**  
As a **Developer** building an external tool, I want to query `GET /api/data` for the current reading as plain JSON, so that I can integrate SunBlockCore-LL into other systems with a simple HTTP call.

**US-045**  
As a **Developer** building an analysis tool, I want to paginate through historical readings via `GET /api/data/history`, so that I can export or process the full dataset programmatically.

---

---

## Data Export

**US-046**  
As an **Admin**, I want to download the full telemetry database as a CSV file, so that I can open it in Excel or a data tool without needing SQLite client software.

**US-047**  
As an **Admin**, I want to download the full telemetry database as an Excel (.xlsx) file with formatted headers, so that I can share it with stakeholders who prefer spreadsheets.

**US-048**  
As an **Admin**, I want to download the raw SQLite database file, so that I can import it into analysis tools (Jupyter, DBeaver) that speak SQLite directly.

---

## Visualize Tab

**US-049**  
As an **Admin**, I want to select a date range and plot one or more telemetry variables on an interactive chart, so that I can reproduce the analysis I previously did in a Jupyter notebook without leaving the browser.

**US-050**  
As an **Admin**, I want to set the sampling rate (e.g. every 5 seconds) for the visualize chart, so that I can reduce data density and see broad trends on long time ranges without loading millions of points.

**US-051**  
As an **Admin**, I want to enable a moving-average smoother with a configurable window, so that I can remove high-frequency noise and see the underlying signal.

**US-052**  
As an **Admin**, I want to enable spike filtering, so that known hardware measurement glitches are replaced with forward-filled values rather than visible outliers in the chart.

**US-053**  
As an **Admin**, I want to plot multiple variables on the same chart axis, so that I can compare PV power, load power, and battery percentage on a single view.

---

## Configurable Live Charts

**US-054**  
As an **Admin**, I want to add extra live rolling charts for any telemetry variable from a dropdown, so that I can monitor variables beyond the default battery % and PV power during an active session.

**US-055**  
As an **Admin**, I want to remove an extra chart with a single click, so that I can quickly clean up the live view without refreshing the page.

---

## Access Tiers & Admin Path

**US-056**  
As a **Viewer** (unauthenticated), I want to see live data cards and rolling charts without logging in, so that the solar output is visible to anyone on the local network without credentials.

**US-057**  
As a **Viewer** (unauthenticated), I want all configuration tabs (Settings, History, Visualize, Parameters, Energy, Logs) to be hidden until I log in, so that the panel's capabilities are not exposed to casual visitors.

**US-058**  
As an **Operator**, I want the admin login page to be at a secret, randomly generated URL rather than `/admin` or `/login`, so that automated bots cannot find it by guessing common paths.

**US-059**  
As an **Operator**, I want the secret admin URL to never appear in server logs or HTTP Referer headers, so that it cannot be leaked via log files or browser navigation.

---

## Audit & Security

**US-060**  
As an **Operator**, I want every admin login attempt (successful and failed) to be logged with timestamp and IP address, so that I can detect credential stuffing or unauthorized access attempts.

**US-061**  
As an **Operator**, I want all settings changes, downloads, power profile switches, and controller writes to be logged in a structured audit file, so that I have a complete record of who changed what and when.

**US-062**  
As an **Operator**, I want the system to reject any attempt to set the data directory to a system path (`/etc`, `/bin`, `/sys`, etc.) — even via symlink traversal — so that a compromised admin session cannot weaponise file writes against the OS.

**US-063**  
As an **Operator**, I want data access endpoints (history, visualize, downloads) to be rate-limited per IP, so that automated tools cannot exhaust server resources by hammering large database queries.

---

## API Tokens (External / Programmatic Access)

**US-064**  
As a **Developer**, I want to generate a bearer token from the admin panel with a custom name and a configurable expiry (1 day to 1 year, or never), so that I can call SunBlockCore-LL's API endpoints from scripts, dashboards, and automations without a browser session.

**US-065**  
As an **Admin**, I want the raw token value to be shown to me exactly once at creation time, with a one-click copy button and a clear warning that it cannot be retrieved again, so that I understand it must be saved immediately and securely.

**US-066**  
As an **Admin**, I want to see a list of all my API tokens — name, creation date, expiry, and last-used time — and revoke any of them instantly, so that I can audit which integrations are active and cut off access the moment a token is no longer needed or may have leaked.

**US-067**  
As an **Operator**, I want every token creation and revocation to be written to the admin audit log with a timestamp and IP address, so that token lifecycle events are traceable alongside every other administrative action.

**US-068**  
As an **Operator**, I want token management itself (`/api/tokens`) and the password-change endpoint to require a real browser session — not accept a bearer token — so that a leaked API token can never be used to mint new tokens, revoke the admin's own tokens, or take over the account.

---

## Logs Tab

**US-076**  
As an **Admin**, I want a Logs tab in the admin panel that shows the most recent lines of `SunBlockCoreLogs.txt`, so that I can troubleshoot startup errors, hardware read failures, and polling issues without an SSH session.

**US-077**  
As an **Admin**, I want to choose how many trailing log lines to load (100 to 2000), so that I can balance how much history I see against how long the request takes.

**US-078**  
As an **Admin**, I want an optional auto-refresh toggle that polls for new log lines every 5 seconds, so that I can watch the log update live while reproducing an issue, without manually clicking refresh.

**US-079**  
As an **Admin**, I want error and warning lines in the log viewer to be visually highlighted, so that I can spot failures at a glance in a long scroll of routine output.

**US-080**  
As an **Admin**, I want to download the full `SunBlockCoreLogs.txt` file, so that I can share it with another developer or archive it for a longer investigation than the in-browser viewer supports.

---

## Acceptance Criteria Summary

| Story | Status |
|---|---|
| US-001 – US-007 (Live monitoring + SIM badge) | Implemented |
| US-008 – US-013 (History tab + pagination) | Implemented |
| US-014 – US-018 (Auth + rate limiting) | Implemented |
| US-019 – US-026 (Settings management + reset + password change) | Implemented |
| US-027 – US-028 (Power profiles) | Implemented |
| US-029 – US-032 (Controller parameters + RTC sync) | Implemented |
| US-033 – US-035 (Energy statistics) | Implemented |
| US-036 – US-038 (Simulator) | Implemented |
| US-039 – US-042 (Operations / deployment) | Implemented |
| US-043 – US-045 (External integrations) | Implemented |
| US-046 – US-048 (Data export — SQLite, CSV, XLSX) | Implemented |
| US-049 – US-053 (Visualize tab) | Implemented |
| US-054 – US-055 (Configurable extra live charts) | Implemented |
| US-056 – US-059 (Access tiers + secret admin path) | Implemented |
| US-060 – US-063 (Audit logging + DoS + path protection) | Implemented |
| US-064 – US-068 (API tokens for external/programmatic access) | Implemented |
| US-069 – US-075 (Two-factor authentication) | Implemented |
| US-076 – US-080 (Logs tab) | Implemented |
