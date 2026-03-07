# joukoPysakkiTaulu — non-touch

The basic version of the bus stop display. Shows upcoming departures from a single configured stop and live bus positions on a map. No interaction required — designed to run unattended on a fixed display.

---

## Features

- Upcoming departures with route number and arrival time
- Times under 10 minutes shown as minutes remaining, over 10 minutes shown as clock time (e.g. 18:05)
- Green dot indicator for real-time vs scheduled arrivals
- Live bus positions on a Leaflet map via Digitransit MQTT
- Configurable route filter — only show the routes you care about
- Dark UI optimised for a small display
- Auto-reconnects if the network drops

---

## Hardware

| Part | Details |
|------|---------|
| Raspberry Pi Zero 2 W | Runs the backend and browser |
| Pimoroni HyperPixel 4.0 | 800×480 display, connects via GPIO |
| MicroSD card | 8 GB or larger |
| USB-C power supply | 5V / 2.5A |

---

## Project structure

```
non-touch/
├── main.py              # FastAPI backend — arrivals + MQTT vehicle positions
├── config.py            # Your local configuration (not committed)
├── config.example.py    # Template — copy this to config.py
├── requirements.txt     # Python dependencies
└── static/
    ├── index.html       # Page structure
    ├── style.css        # All styles
    └── app.js           # Map, arrivals rendering, WebSocket client
```

---

## How it works

On startup, the backend connects to the Digitransit MQTT broker and subscribes to vehicle position topics for each configured route. It also starts polling the GraphQL API for upcoming departures every `ARRIVALS_INTERVAL` seconds.

Both data streams are merged and pushed to connected browser clients over a single WebSocket. The browser renders the arrivals table and updates bus marker positions on the map in place.

---

## Setup

### 1. Get a Digitransit API key

Register at [portal.digitransit.fi](https://portal.digitransit.fi) to get a free API key.

### 2. Find your stop ID

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

Copy the `gtfsId` of your nearest stop (e.g. `YourCity:123456`).

### 3. Configure

```bash
cp config.example.py config.py
```

Edit `config.py`:

```python
API_KEY        = "your_key_here"
STOP_ID        = "YourCity:123456"
HOME_LAT       = 60.1699
HOME_LON       = 24.9384
ROUTES_FILTER  = ["7", "14", "23"]  # empty list = show all routes
ARRIVALS_COUNT = 8
```

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
cd joukoPysakkiTaulu/non-touch
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
WorkingDirectory=/home/pi/joukoPysakkiTaulu/non-touch
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
| `STOP_ID` | GTFS stop ID | — |
| `HOME_LAT` / `HOME_LON` | Initial map center | — |
| `ROUTES_FILTER` | List of route short names to show. Empty = all | `["7", "14", "23"]` |
| `ARRIVALS_COUNT` | Number of departures to display | `8` |
| `ARRIVALS_INTERVAL` | How often to fetch arrivals (seconds) | `20` |
| `VEHICLES_INTERVAL` | How often to push vehicle positions to browser (seconds) | `5` |
