# SunBlockCore-LL: Linux Scripts

`Systemd` scripts make sure SunBlockCore-LL starts automatically when the system boots.

Run all commands from this directory. The `.service` file depends on the corresponding `.sh` file being in the right location.

> **Note:** SunBlockCore-LL is a single unified ASGI server (FastAPI + python-socketio) that
> replaces the original two-process **SunBlockCore** (Python logical layer) +
> **SunBlockExpress** (JS web server) split. Only one service is needed.

## SunBlockCore-LL

Edit `SB_RunSunBlockCore-LL.sh` first and update the `cd` path (and the path to
`.venv/bin/uvicorn`) to match where you cloned the repo — the default assumes
`/home/pc/GitHub/SunBlockCore-LL`. Also update `EnvironmentFile=` in
`SB_RunSunBlockCore-LL.service` to point at your `.env`.

```bash
sudo cp SB_RunSunBlockCore-LL.sh /usr/local/bin/
sudo chmod a+x /usr/local/bin/SB_RunSunBlockCore-LL.sh
```

```bash
sudo cp SB_RunSunBlockCore-LL.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable SB_RunSunBlockCore-LL.service
sudo systemctl start SB_RunSunBlockCore-LL.service
```

This is the same service `scripts/deploy.sh` installs automatically (under the
name `sunblock`) — use these files if you'd rather wire it up by hand or are
migrating an existing systemd setup from the original SunBlockCore/SunBlockExpress
services.

---

## Troubleshooting

To see the output of the script as it's starting up:

```bash
sudo journalctl -b -u SB_RunSunBlockCore-LL.service
```

To see what the service is doing *now*:

```bash
sudo systemctl status SB_RunSunBlockCore-LL.service
```

To restart after changing `.env` or pulling new code:

```bash
sudo systemctl restart SB_RunSunBlockCore-LL.service
```
