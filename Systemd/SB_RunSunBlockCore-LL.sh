#!/bin/bash
cd /home/pc/GitHub/SunBlockCore-LL || exit 1 # Change this directory based on where you cloned the repo
exec /home/pc/GitHub/SunBlockCore-LL/.venv/bin/uvicorn sunblock:socket_app --host 0.0.0.0 --port "${PORT:-3707}"
