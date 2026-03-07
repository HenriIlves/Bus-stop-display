# Joukopysakkitaulu

A real-time bus stop display for a Raspberry Pi Zero 2 W with a Pimoroni HyperPixel 4.0 screen. Shows upcoming departures and live bus positions on a map for a chosen stop in Lappeenranta, Finland. Data is sourced from the [Digitransit](https://digitransit.fi/en/developers/) API.

![Display layout: arrival list on the left, live map on the right]

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
joukoPysakkiTaulu/
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

**Arrivals** are fetched from the Digitransit GraphQL API every 20 seconds:

```
https://api.digitransit.fi/routing/v2/waltti/gtfs/v1
```

**Vehicle positions** are received in real time via MQTT from the Digitransit broker:

```
mqtt.digitransit.fi:1883
Topic: /gtfsrt/vp/{feedId}/+/+/+/{route}/#
```

The backend runs a FastAPI server that pushes both arrivals and vehicle positions to the browser over a WebSocket. Chromium runs in kiosk mode pointing to `http://localhost:8000`.

---

## Setup

### 1. Get a Digitransit API key

Register at [portal.digitransit.fi](https://portal.digitransit.fi) to get a free API key.

### 2. Find your stop ID

Open the [Waltti GraphQL explorer](https://api.digitransit.fi/graphiql/waltti/v2), add your API key as a header, and run:

```graphql
{
    stopsByRadius(lat: 61.0587, lon: 28.1873, radius: 300) {
        edges {
            node {
                stop { gtfsId name lat lon }
                distance
            }
        }
    }
}
```

Copy the `gtfsId` of your nearest stop (e.g. `Lappeenranta:205325`).

### 3. Configure

```bash
cp config.example.py config.py
```

Edit `config.py`:

```python
API_KEY       = "your_key_here"
STOP_ID       = "Lappeenranta:205325"
HOME_LAT      = 61.0551
HOME_LON      = 28.1992
ROUTES_FILTER = ["1", "1x", "5"]  # empty list = show all routes
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
cd joukoPysakkiTaulu
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
WorkingDirectory=/home/pi/joukoPysakkiTaulu
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

### Deploying updates

```bash
# From your Mac — push, pull on Pi, restart service
git push && ssh pi@<PI_IP> "cd ~/joukoPysakkiTaulu && git pull && sudo systemctl restart pysakkitaulu"
```

---

## Configuration reference

| Key | Description | Default |
|-----|-------------|---------|
| `API_KEY` | Digitransit API key | — |
| `STOP_ID` | GTFS stop ID | — |
| `HOME_LAT` / `HOME_LON` | Initial map center | Lappeenranta center |
| `ROUTES_FILTER` | List of route short names to show. Empty = all | `["1", "1x", "5"]` |
| `ARRIVALS_COUNT` | Number of departures to display | `8` |
| `ARRIVALS_INTERVAL` | How often to fetch arrivals (seconds) | `20` |
| `VEHICLES_INTERVAL` | How often to push vehicle positions to browser (seconds) | `5` |
