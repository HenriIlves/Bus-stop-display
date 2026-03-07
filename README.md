# joukoPysakkiTaulu

A real-time bus stop display board for a Raspberry Pi Zero 2 W with a Pimoroni HyperPixel 4.0 (800×480) screen. Fetches upcoming departures and live vehicle positions from the [Digitransit](https://digitransit.fi/en/developers/) API and displays them on a dark map UI in a browser running in kiosk mode.

---

## Versions

This repository contains two versions of the display:

### `non-touch/` — Basic display

The original version. Shows upcoming departures from a single configured stop with live bus positions on a map. Designed to run unattended with no interaction required.

### `touch/` — Touch display with favorites

Extended version for a touchscreen. Adds a floating action button (FAB) that lets you switch between the default arrivals view and named favorite destinations. Each favorite shows a full route plan — when the bus departs and when you arrive — including any walking time at the end of the journey.

---

## Hardware

| Part | Details |
|------|---------|
| Raspberry Pi Zero 2 W | Runs the backend and browser |
| Pimoroni HyperPixel 4.0 | 800×480 display, connects via all 40 GPIO pins |
| MicroSD card | 8 GB or larger |
| USB-C power supply | 5V / 2.5A |

---

## Data sources

**Arrivals** — Digitransit Waltti GraphQL API, polled every 20 seconds:
```
https://api.digitransit.fi/routing/v2/waltti/gtfs/v1
```

**Vehicle positions** — Digitransit MQTT broker, streamed in real time:
```
mqtt.digitransit.fi:1883
Topic: /gtfsrt/vp/{feedId}/+/+/+/{route}/#
```

Messages are GTFS-RT protobufs containing GPS coordinates and bearing for each vehicle.

---

## Getting started

See the README inside each version's folder for setup instructions specific to that version.

- [non-touch/README.md](non-touch/README.md)
- [touch/README.md](touch/README.md)
