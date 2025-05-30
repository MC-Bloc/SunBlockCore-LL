# Sunblock Core

The core interfaces with the Solar Controller through an RS45 Cable. It logs historic data in a SQLite database and instantaneous data in a json file. This instantenous data is read by an express.js server that publishes data to the web API. 

All data is stored in the `~/SunblockData/` directory.

## Data logging and Interfacing (RS45 Cable)

The script reads data directly from the EPEVER solar controller using the `epevermodbus` python library ([find here](https://github.com/rosswarren/epevermodbus?tab=readme-ov-file)).