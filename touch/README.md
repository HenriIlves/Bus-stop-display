# joukoPysakkiTaulu — touch

The touchscreen version of the bus stop display. In addition to showing upcoming departures from a home stop, it adds a floating button that lets you switch to a **destination view** — showing when each bus departs and when you arrive, with optional walk time after the bus.

---

## Features

- Upcoming departures with route number and arrival time
- Times under 10 minutes shown as minutes remaining, over 10 minutes shown as clock time (e.g. 18:05)
- Green dot indicator for real-time vs scheduled arrivals
- Live bus positions on a Leaflet map via Digitransit MQTT
- Configurable route filter — only show the routes you care about
- **Favorite destinations** — tap to switch to a route plan view for any saved destination
- **Destination view** shows departure time and bus arrival time, e.g. `14:10 → 14:28`
- If walking is required after the bus, arrival time shown as `14:28(+5)`
- Dark UI optimised for a small display
- Auto-reconnects if the network drops

---

## Hardware

| Part | Details |
|------|---------|
| Raspberry Pi Zero 2 W | Runs the backend and browser |
| Pimoroni HyperPixel 4.0 | 800×480 touchscreen display, connects via all 40 GPIO pins |
| MicroSD card | 8 GB or larger |
| USB-C power supply | 5V / 2.5A |

> The HyperPixel uses all 40 GPIO pins, so physical buttons must connect via the HyperPixel breakout header or USB OTG.

---

## Project structure

```
touch/
├── main.py              # FastAPI backend — arrivals, routing, MQTT vehicle positions
├── config.py            # Your local configuration (not committed)
├── config.example.py    # Template — copy this to config.py
├── requirements.txt     # Python dependencies
└── static/
    ├── index.html       # Page structure
    ├── style.css        # All styles
    └── app.js           # Map, arrivals/itinerary rendering, FAB, WebSocket client
```

---

## How it works

### Default view

The home stop arrivals work the same as the non-touch version: the backend polls the GraphQL API for departures every `ARRIVALS_INTERVAL` seconds and streams results to the browser over WebSocket.

### Destination view

When a favorite is selected via the FAB button, the backend switches to showing **itineraries** — full route plans from the departure stop to the destination. These are fetched from the Digitransit routing API (`planConnection`) and filtered to only show trips on the configured routes.

Each itinerary shows:
- **Departs** — when the bus leaves the home stop (minutes countdown or clock time)
- **Arrives** — when the bus reaches the destination stop
- **(+N)** — minutes of walking after the bus, if the destination is set to a coordinate past the stop

A favorite can have multiple destination stops (e.g. when different routes stop at different nearby stops) — results from all stops are merged and deduplicated by `(route, departure time)`.

### Vehicle positions

The backend subscribes to the Digitransit MQTT broker for each route in `ROUTES_FILTER` and parses incoming GTFS-RT protobufs. Positions older than 120 seconds are dropped before each broadcast.

---

## Setup

### 1. Get a Digitransit API key

Register at [portal.digitransit.fi](https://portal.digitransit.fi) to get a free API key.

### 2. Find your stop IDs

Open the [Waltti GraphQL explorer](https://api.digitransit.fi/graphiql/waltti/v2), add your API key as a header (`digitransit-subscription-key`), and run:

```graphql
{
    stopsByRadius(lat: YOUR_LAT, lon: YOUR_LON, radius: 300) {
        edges {
            node {
                stop { gtfsId name lat lon }
                distance
            }
        }
    }
}
```

Run this for both your home stop and each destination stop. Copy the `gtfsId` values (e.g. `YourCity:123456`).

### 3. Configure

```bash
cp config.example.py config.py
```

Edit `config.py`:

```python
API_KEY         = "your_key_here"
DEFAULT_STOP_ID = "YourCity:123456"
ROUTES_FILTER   = ["7", "14", "23"]
ARRIVALS_COUNT  = 8
MAP_LAT         = 60.1699
MAP_LON         = 24.9384

FAVORITES = [
    {
        "name": "Work",
        "from_stop": "YourCity:123456",
        "to_stop":   "YourCity:654321",
    },
    {
        "name": "Mall",
        "from_stop": "YourCity:123456",
        "to_stop":   "YourCity:111111",
        "dest_lat":  60.1710,   # walking destination past the stop
        "dest_lon":  24.9420,
    },
]
```

**`dest_lat` / `dest_lon`** are optional. Set them when the actual destination (e.g. a building entrance) is a walk away from the nearest stop. The routing API will plan to that coordinate, and the arrival time shown will be the bus arrival at the stop — the walk time appears as `(+N)` next to it.

### 4. Install dependencies and run

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` in a browser.

---

## Deploying to the Raspberry Pi

### First time

```bash
# On the Pi
git clone https://github.com/YOUR_USERNAME/joukoPysakkiTaulu.git
cd joukoPysakkiTaulu/touch
pip install -r requirements.txt
cp config.example.py config.py
nano config.py
```

### Auto-start with systemd

Create `/etc/systemd/system/pysakkitaulu.service`:

```ini
[Unit]
Description=Pysakkitaulu
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/home/pi/joukoPysakkiTaulu/touch
ExecStart=/home/pi/.local/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=10
User=pi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable pysakkitaulu
sudo systemctl start pysakkitaulu
```

### Kiosk browser on boot

Create `/etc/xdg/autostart/pysakkitaulu-browser.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=Pysakkitaulu
Exec=chromium-browser --kiosk --noerrdialogs --disable-infobars http://localhost:8000
```

---

## Configuration reference

| Key | Description | Default |
|-----|-------------|---------|
| `API_KEY` | Digitransit API key | — |
| `DEFAULT_STOP_ID` | GTFS stop ID for the home stop | — |
| `MAP_LAT` / `MAP_LON` | Initial map center | — |
| `ROUTES_FILTER` | List of route short names to show. Empty = all | `["7", "14", "23"]` |
| `ARRIVALS_COUNT` | Number of departures / itineraries to display | `8` |
| `ARRIVALS_INTERVAL` | How often to fetch arrivals and itineraries (seconds) | `20` |
| `VEHICLES_INTERVAL` | How often to push vehicle positions to browser (seconds) | `5` |
| `FAVORITES` | List of named destinations for the FAB menu | `[]` |

### FAVORITES entry fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Label shown on the FAB button and destination header |
| `from_stop` | Yes | GTFS stop ID to depart from |
| `to_stop` | One of | GTFS stop ID of the destination stop |
| `to_stops` | One of | List of GTFS stop IDs (when different routes use different stops) |
| `dest_lat` / `dest_lon` | No | Actual destination coordinates if a walk is needed after the bus |
