# Sunblock Core

The core manages the system's power states, power profiles, interfaces with the ESP32 Controller, logs raw data, and logs power states.
All data is stored in the `~/SunblockData/` directory.

## Power Management and Logging 

Power logging is in the file named `SunblockPowerlogs.txt` in the `SunblockData` Directory

### Power States 
- Server refers to the Minecraft server instance. 
- System refers to the MiniPC that all this is running on. 
- Sunblock refers to the whole rig with the solar controller, panel, battery, ESP32, etc. you get the idea 

Power states are useful abstractions that refer to certain system properties under an umberella term:

1. HOT: System is on and MC Server is on. Power profile varies.
2. WARM: System is on and MC Server is off. Power profile sticks to power-saver.
3. COOL: System is off. Minimal battery remaining
4. COLD: Sunblock as a whole is now off - battery is dead.

Heatup means Sunblock is going from a lower state to a higher state.

Cooldown means Sunblock is going from a higher state to a lower state. 

I noticed the power consumption when the system switched from the linux "performance" profile to the "power-saver" profile was very signficant, the power consumption almost halved. And so, the core also manages power states. (This automation is deprecated and will be removed. THe idea is now to let the player's decide and keep the system on power saver or performance; this will be an in-game affordance) 

When sunblock is warm, the system will stay in power-saving. When sunblock is hot, the system will transition between power-saver and performance as decided by the players.

And all these changes are logged in the SunblockPowerlogs.txt as they happen. If the service crashes, that is where you will find out if it has crashed.


## Data logging and Interfacing with the ESP32 Controller

The script reads from the serial data provided by an attached ESP32 device (attached via usb), every second.

The script writes `JSON` data to a file, called `solar_data.json`, in the `SunblockData` folder.

The script also logs raw data to an `SQLite` database called `sunblock.db` in the `SunblockData` folder. This database is used by dat

### SQLite Database creation: 
Database name: `sunblockone.db`

Open database with `sqlite3 sunblockone.db` (if it exists, it will be opened. If it doesnt, it will be created). 

**If you are creating a new database, run the following commmand inside SQLite:**

```
CREATE TABLE solardata(timestamp text, PVVoltage real, PVCurrent real, PVPower real, BattVoltage real, BattChargeCurrent real, BattChargePower real, LoadPower real, BattPercentage int, BattOverallCurrent real, CPUPowerDraw real);
```

## Installation:

1. Clone the repo
2. Add a bash script in `/usr/local/bin/` named `runsunblockcore.sh`
3. Add a systemd service named `runsunblockcore.service` as show below 
4. run `sudo systemctl daemon-reload`
5. run `sudo systemctl enable runsunblockcore.service` 
6. run `sudo systemctl start runsunblockcore.service` 
7. This should now have the system running smoothly 

### Bash Script

```
#!/bin/bash
cd /home/pc/GitHub/solar_server/SunblockCore
/usr/bin/python3 ./core.py
exit 0
```

### Systemd Service

```
[Unit]
Description=This service runs the system manager for sunblock
After=network.target

[Service]
Type=simple
User=pc
ExecStart=/usr/local/bin/runsunblockcore.sh
TimeoutStartSec=0


[Install]
WantedBy=default.target
```

Make sure the SunblockData directory is writable by the main system user (likely named 'pc')


