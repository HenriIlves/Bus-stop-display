# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Run the backend:**
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

**Install dependencies:**
```bash
pip install -r requirements.txt
```

**Format Python:**
```bash
python3 -m black main.py config.example.py
```

**Format JS/CSS/HTML:**
```bash
npx prettier --write static/app.js static/style.css static/index.html
```
Prettier is configured to use 4-space indentation via `.prettierrc`.

## Architecture

The app has two data flows feeding a single WebSocket broadcast:

**1. Arrivals (GraphQL polling)**
`main.py:fetch_arrivals()` posts to the Digitransit Waltti GraphQL endpoint every `ARRIVALS_INTERVAL` seconds. Results are filtered by `config.ROUTES_FILTER` and capped at `config.ARRIVALS_COUNT`.

**2. Vehicle positions (MQTT)**
`main.py:start_mqtt()` runs in a daemon thread and subscribes to `/gtfsrt/vp/{feed}/+/+/+/{route}/#` on `mqtt.digitransit.fi:1883` for each route in `ROUTES_FILTER`. Each message is a GTFS-RT protobuf (`FeedMessage`). Parsed positions are stored in the module-level `vehicle_state` dict keyed by vehicle entity ID, with a timestamp for staleness filtering.

**3. WebSocket broadcast**
Two asyncio tasks push data to connected browser clients:
- `poll_loop()` — fires after every arrivals fetch (every `ARRIVALS_INTERVAL` seconds)
- `vehicle_broadcast_loop()` — fires every `VEHICLES_INTERVAL` seconds

Both call `broadcast()`, which also flushes stale vehicles (older than `VEHICLE_STALE_SECONDS = 120`) from `vehicle_state` before serialising.

**Frontend**
`static/app.js` opens a WebSocket to `ws://{host}/ws` and on each message calls `renderArrivals()` and `renderVehicles()`. The Leaflet map is fully locked (no pan/zoom). Bus markers are `L.divIcon` with an SVG arrow rotated by bearing. The map tile provider is Stadia Alidade Smooth Dark.

## Key configuration

`config.py` (gitignored — copy from `config.example.py`):
- `STOP_ID` — Waltti GTFS stop ID, format `Lappeenranta:XXXXXX`
- `ROUTES_FILTER` — list of route short names; controls both arrivals filter and MQTT subscriptions
- `HOME_LAT` / `HOME_LON` — initial map centre in `app.js` (hardcoded at line 25; must be updated manually when changed in config)

## Known constraints

- Lappeenranta's operator (LSL) does **not** publish vehicle positions via the Digitransit GraphQL `vehiclePositions` field — MQTT is the only working source.
- The HyperPixel 4.0 display uses all 40 GPIO pins; a physical button must connect via the HyperPixel breakout header or USB OTG.
- The display is fixed at 800×480 in CSS (`body` width/height).
