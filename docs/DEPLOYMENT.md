# SunBlockCore-LL Deployment Guide

## Automated deployment

For a fresh Ubuntu server, the deploy script handles everything — dependencies, venv, vendoring, `.env` setup (including generating a random `ADMIN_PATH`), systemd service, and sudoers config:

```bash
git clone https://github.com/MC-Bloc/SunBlockCore-LL.git
cd SunBlockCore-LL
bash scripts/deploy.sh
```

The script targets port **3707** and is safe to re-run (idempotent). The final output prints both the public URL and the private admin URL. Manual steps below if you need finer control.

---

## Manual deployment

### Prerequisites

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git curl
```

Confirm the solar controller is visible after plugging in the RS-485 cable:

```bash
sudo dmesg | tail -5   # look for ttyACM0 or similar
```

---

## 1. Clone & Install

```bash
git clone https://github.com/MC-Bloc/SunBlockCore-LL.git
cd SunBlockCore-LL

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

bash scripts/vendor.sh
```

---

## 2. Configure `.env`

```bash
cp sample.env .env
nano .env
```

Fill in your values:

```env
CONTROLLER_PORT=/dev/ttyACM0
CONTROLLER_SLAVE=1

DATA_DIRECTORY=/home/YOUR_USER/SunblockData/
POWER_DRAW_SCRIPT_ADDR=/home/YOUR_USER/power_scripts/powerdraw.sh

DATA_MAN=true
READ_INTERVAL=1
PORT=3707

ADMIN_USERNAME=admin
ADMIN_PASSWORD_HASH=        # see step 3
SECRET_KEY=                 # see step 3
TOKEN_EXPIRE_HOURS=24
SECURE_COOKIES=false        # set true if using HTTPS (see step 7)
ADMIN_PATH=                 # see step 3 — keep this private
```

---

## 3. Generate Secrets

**Password hash:**

```bash
.venv/bin/python3 scripts/gen_password_hash.py
```

Paste the output into `ADMIN_PASSWORD_HASH` in `.env`.

**Secret key:**

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Paste the output into `SECRET_KEY` in `.env`.

**Admin path** (secret URL slug for the login page):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(12))"
```

Paste the output into `ADMIN_PATH` in `.env`. This is the only URL that shows the login form — keep it private. Bots that scan `/admin`, `/login`, and `/dashboard` will find nothing.

---

## 4. Test Run

```bash
.venv/bin/uvicorn sunblock:socket_app --host 0.0.0.0 --port 3707
```

- **Public live view:** `http://YOUR_SERVER_IP:3707` — data cards visible without login
- **Admin login:** `http://YOUR_SERVER_IP:3707/<ADMIN_PATH>` — login form

If the dashboard loads and the controller connects, press `Ctrl+C` and proceed to step 5.

---

## 5. Run as a systemd Service

> **Shortcut:** `Systemd/` in the repo root ships ready-made unit + launcher
> scripts (`SB_RunSunBlockCore-LL.service` / `.sh`) that produce an equivalent
> service to the steps below — see `Systemd/README.md` for copy-paste install
> instructions. Use those if you'd rather not hand-write the unit file, or if
> you're migrating an existing systemd setup from the original
> SunBlockCore/SunBlockExpress two-service split (this single ASGI app
> replaces both).

Create the service file:

```bash
sudo nano /etc/systemd/system/sunblock.service
```

```ini
[Unit]
Description=SunBlockCore-LL Admin Server
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/SunBlockCore-LL
EnvironmentFile=/home/YOUR_USER/SunBlockCore-LL/.env
ExecStart=/home/YOUR_USER/SunBlockCore-LL/.venv/bin/uvicorn sunblock:socket_app --host 0.0.0.0 --port 3707
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable sunblock
sudo systemctl start sunblock
sudo systemctl status sunblock
```

Follow logs:

```bash
# Application log (startup, polling, errors)
journalctl -u sunblock -f

# Admin audit log (login, settings changes, downloads)
tail -f /home/YOUR_USER/SunblockData/SunBlockAdminAudit.txt
```

---

## 6. Passwordless sudo for Power Profiles

```bash
sudo visudo
```

Add at the bottom:

```
YOUR_USER ALL=(ALL) NOPASSWD: /usr/bin/powerprofilesctl
```

---

## 7. Optional: nginx + HTTPS

Required if you want to set `SECURE_COOKIES=true` and access the panel over the internet.

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com
```

Create the nginx site config:

```bash
sudo nano /etc/nginx/sites-available/sunblock
```

```nginx
server {
    listen 443 ssl;
    server_name yourdomain.com;

    location / {
        proxy_pass         http://127.0.0.1:3707;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
    }
}
```

Enable and reload:

```bash
sudo ln -s /etc/nginx/sites-available/sunblock /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Then update `.env`:

```env
SECURE_COOKIES=true
```

And restart the service:

```bash
sudo systemctl restart sunblock
```

---

## 8. File Permissions

Protect files that contain secrets or sensitive operational data:

```bash
chmod 600 /home/YOUR_USER/SunBlockCore-LL/.env
chmod 600 /home/YOUR_USER/SunblockData/sunblock_settings.db
chmod 600 /home/YOUR_USER/SunblockData/SunBlockAdminAudit.txt
```

---

## Updating

```bash
cd /home/YOUR_USER/SunBlockCore-LL
git pull
.venv/bin/pip install -r requirements.txt
bash scripts/vendor.sh
sudo systemctl restart sunblock
```
