import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from typing import Set

import httpx
import paho.mqtt.client as mqtt
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.transit import gtfs_realtime_pb2

import config

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

state = {
    "arrivals": [],
    "stop_name": "",
    "stop_lat": config.MAP_LAT,
    "stop_lon": config.MAP_LON,
    "itineraries": {fav["name"]: [] for fav in config.FAVORITES},
    "favorites_meta": [],
    "vehicles": [],
}

# Stop coordinates fetched at startup: stop_id -> {"lat", "lon", "name"}
stop_coords: dict = {}

# Live vehicle positions keyed by vehicle entity ID
vehicle_state: dict = {}

clients: Set[WebSocket] = set()

# ---------------------------------------------------------------------------
# GraphQL — stop time arrivals (default view)
# ---------------------------------------------------------------------------

GRAPHQL_URL = "https://api.digitransit.fi/routing/v2/waltti/gtfs/v1"

GRAPHQL_HEADERS = {
    "digitransit-subscription-key": config.API_KEY,
    "Content-Type": "application/json",
}

ARRIVALS_QUERY = """
{
  stop(id: "%s") {
    name
    lat
    lon
    stoptimesWithoutPatterns(numberOfDepartures: %d, omitCanceled: false) {
        scheduledArrival
        realtimeArrival
        realtime
        serviceDay
        trip {
            routeShortName
        }
        headsign
    }
  }
}
""" % (
    config.DEFAULT_STOP_ID,
    config.ARRIVALS_COUNT * 5,
)


def _fav_to_stops(fav: dict) -> list:
    """Return the list of destination stop IDs for a favorite."""
    if "to_stops" in fav:
        return fav["to_stops"]
    return [fav["to_stop"]]


async def fetch_stop_coords(client: httpx.AsyncClient) -> None:
    """Batch-fetch lat/lon for all stop IDs referenced in config."""
    stop_ids = {config.DEFAULT_STOP_ID}
    for fav in config.FAVORITES:
        stop_ids.add(fav["from_stop"])
        for sid in _fav_to_stops(fav):
            stop_ids.add(sid)

    aliases = {f"s{i}": sid for i, sid in enumerate(sorted(stop_ids))}
    fields = "\n".join(
        f'{alias}: stop(id: "{sid}") {{ lat lon name }}'
        for alias, sid in aliases.items()
    )
    query = "{ " + fields + " }"

    try:
        resp = await client.post(
            GRAPHQL_URL, json={"query": query}, headers=GRAPHQL_HEADERS, timeout=10
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        for alias, sid in aliases.items():
            stop = data.get(alias)
            if stop:
                stop_coords[sid] = stop
                print(f"Stop coords: {sid} → {stop['name']} ({stop['lat']}, {stop['lon']})")
            else:
                print(f"Stop not found: {sid}")
    except Exception as e:
        print(f"Stop coord fetch error: {e}")

    # Build favorites_meta for the frontend (destination pin coordinates)
    state["favorites_meta"] = [
        {
            "name": fav["name"],
            "dest_lat": fav.get("dest_lat") or stop_coords.get(_fav_to_stops(fav)[0], {}).get("lat"),
            "dest_lon": fav.get("dest_lon") or stop_coords.get(_fav_to_stops(fav)[0], {}).get("lon"),
        }
        for fav in config.FAVORITES
    ]


async def fetch_arrivals(client: httpx.AsyncClient) -> None:
    try:
        resp = await client.post(
            GRAPHQL_URL,
            json={"query": ARRIVALS_QUERY},
            headers=GRAPHQL_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        stop = resp.json()["data"]["stop"]
        if not stop:
            print("Default stop not found — check DEFAULT_STOP_ID in config.py")
            return

        state["stop_name"] = stop["name"]
        state["stop_lat"] = stop["lat"]
        state["stop_lon"] = stop["lon"]

        now = int(time.time())
        allowed = {r.lower() for r in config.ROUTES_FILTER}
        arrivals = []
        for st in stop["stoptimesWithoutPatterns"]:
            arrival_secs = st["realtimeArrival"] if st["realtime"] else st["scheduledArrival"]
            departure_epoch = st["serviceDay"] + arrival_secs
            minutes = round((departure_epoch - now) / 60)
            if minutes < 0:
                continue
            if allowed and st["trip"]["routeShortName"].lower() not in allowed:
                continue
            arrivals.append(
                {
                    "route": st["trip"]["routeShortName"],
                    "headsign": st["headsign"] or "",
                    "minutes": minutes,
                    "epoch": departure_epoch,
                    "realtime": st["realtime"],
                }
            )
        state["arrivals"] = arrivals[: config.ARRIVALS_COUNT]
    except Exception as e:
        print(f"Arrivals fetch error: {e}")


# ---------------------------------------------------------------------------
# Routing — itinerary planning (destination view)
# ---------------------------------------------------------------------------

ROUTING_QUERY = """
{
  planConnection(
    origin:      {location: {coordinate: {latitude: %f, longitude: %f}}}
    destination: {location: {coordinate: {latitude: %f, longitude: %f}}}
    first: 20
    searchWindow: "PT4H"
  ) {
    edges {
      node {
        start
        end
        legs {
          mode
          end { scheduledTime }
          realtimeState
          trip { routeShortName }
        }
      }
    }
  }
}
"""


def _parse_dt(val) -> int:
    """Convert OTP2 DateTime (ISO string or Unix-ms integer) to epoch seconds."""
    if isinstance(val, (int, float)):
        return int(val) // 1000
    from datetime import datetime
    return int(datetime.fromisoformat(str(val)).timestamp())


async def fetch_itineraries(client: httpx.AsyncClient) -> None:
    now = int(time.time())
    allowed = {r.lower() for r in config.ROUTES_FILTER}

    TRANSIT_MODES = {"BUS", "TRAM", "RAIL", "SUBWAY", "FERRY"}

    for fav in config.FAVORITES:
        from_c = stop_coords.get(fav["from_stop"])
        if not from_c:
            continue

        results = []
        for to_sid in _fav_to_stops(fav):
            to_c = stop_coords.get(to_sid)
            if not to_c:
                continue

            dest_lat = fav.get("dest_lat") or to_c["lat"]
            dest_lon = fav.get("dest_lon") or to_c["lon"]

            query = ROUTING_QUERY % (
                from_c["lat"], from_c["lon"],
                dest_lat, dest_lon,
            )

            try:
                resp = await client.post(
                    GRAPHQL_URL,
                    json={"query": query},
                    headers=GRAPHQL_HEADERS,
                    timeout=10,
                )
                resp.raise_for_status()
                edges = resp.json()["data"]["planConnection"]["edges"]

                for edge in edges:
                    node = edge["node"]
                    transit_legs = [l for l in node["legs"] if l["mode"] in TRANSIT_MODES]
                    if not transit_legs:
                        continue

                    first_leg = transit_legs[0]
                    last_leg = transit_legs[-1]
                    route_name = (first_leg.get("trip") or {}).get("routeShortName", "")
                    if route_name.lower() not in allowed:
                        continue

                    departs_epoch = _parse_dt(node["start"])
                    bus_arrives_epoch = _parse_dt(last_leg["end"]["scheduledTime"])
                    total_arrives_epoch = _parse_dt(node["end"])
                    walk_after = max(0, round((total_arrives_epoch - bus_arrives_epoch) / 60))
                    minutes = round((departs_epoch - now) / 60)
                    if minutes < 0:
                        continue

                    results.append(
                        {
                            "route": route_name,
                            "departs": departs_epoch,
                            "arrives": bus_arrives_epoch,
                            "walk_after": walk_after,
                            "minutes": minutes,
                            "realtime": first_leg.get("realtimeState", "SCHEDULED") != "SCHEDULED",
                        }
                    )
            except Exception as e:
                print(f"Routing fetch error for '{fav['name']}' → {to_sid}: {e}")

        # Merge results from all destination stops: deduplicate by (route, departs),
        # sort by departure time
        seen = set()
        merged = []
        for r in sorted(results, key=lambda x: x["departs"]):
            key = (r["route"], r["departs"])
            if key not in seen:
                seen.add(key)
                merged.append(r)

        state["itineraries"][fav["name"]] = merged[: config.ARRIVALS_COUNT]


# ---------------------------------------------------------------------------
# MQTT — vehicle positions
# ---------------------------------------------------------------------------

MQTT_HOST = "mqtt.digitransit.fi"
MQTT_PORT = 1883
VEHICLE_STALE_SECONDS = 120


def on_mqtt_connect(client, userdata, flags, rc, props=None):
    feed = config.DEFAULT_STOP_ID.split(":")[0]
    for route in config.ROUTES_FILTER:
        topic = f"/gtfsrt/vp/{feed}/+/+/+/{route}/#"
        client.subscribe(topic)
        print(f"MQTT subscribed: {topic}")


def on_mqtt_message(client, userdata, msg):
    try:
        parts = msg.topic.split("/")
        route = parts[7] if len(parts) > 7 else ""

        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(msg.payload)

        for entity in feed.entity:
            if not entity.HasField("vehicle"):
                continue
            v = entity.vehicle
            if not v.HasField("position"):
                continue
            bearing = v.position.bearing if v.position.HasField("bearing") else 0.0
            vehicle_state[entity.id] = {
                "id": entity.id,
                "route": route,
                "lat": v.position.latitude,
                "lon": v.position.longitude,
                "bearing": bearing,
                "updated": time.time(),
            }
    except Exception as e:
        print(f"MQTT message error: {e}")


def start_mqtt() -> None:
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"pysakkitaulu-{int(time.time())}",
    )
    client.on_connect = on_mqtt_connect
    client.on_message = on_mqtt_message
    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
            client.loop_forever()
        except Exception as e:
            print(f"MQTT connection error: {e} — retrying in 30s")
            time.sleep(30)


# ---------------------------------------------------------------------------
# WebSocket broadcast
# ---------------------------------------------------------------------------


async def broadcast() -> None:
    if not clients:
        return

    cutoff = time.time() - VEHICLE_STALE_SECONDS
    state["vehicles"] = [v for v in vehicle_state.values() if v["updated"] > cutoff]

    message = json.dumps(
        {
            "arrivals": state["arrivals"],
            "stop_name": state["stop_name"],
            "stop_lat": state["stop_lat"],
            "stop_lon": state["stop_lon"],
            "itineraries": state["itineraries"],
            "favorites_meta": state["favorites_meta"],
            "vehicles": state["vehicles"],
        }
    )
    dead: Set[WebSocket] = set()
    for ws in list(clients):
        try:
            await ws.send_text(message)
        except Exception:
            dead.add(ws)
    clients.difference_update(dead)


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------


async def vehicle_broadcast_loop() -> None:
    while True:
        await asyncio.sleep(config.VEHICLES_INTERVAL)
        await broadcast()


async def poll_loop() -> None:
    async with httpx.AsyncClient() as client:
        await fetch_stop_coords(client)
        await fetch_arrivals(client)
        await fetch_itineraries(client)
        await broadcast()

        while True:
            await asyncio.sleep(config.ARRIVALS_INTERVAL)
            await fetch_arrivals(client)
            await fetch_itineraries(client)
            await broadcast()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    mqtt_thread = threading.Thread(target=start_mqtt, daemon=True)
    mqtt_thread.start()
    asyncio.create_task(poll_loop())
    asyncio.create_task(vehicle_broadcast_loop())
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)
    try:
        await websocket.send_text(
            json.dumps(
                {
                    "arrivals": state["arrivals"],
                    "stop_name": state["stop_name"],
                    "stop_lat": state["stop_lat"],
                    "stop_lon": state["stop_lon"],
                    "itineraries": state["itineraries"],
                    "favorites_meta": state["favorites_meta"],
                    "vehicles": state["vehicles"],
                }
            )
        )
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        clients.discard(websocket)
    except Exception:
        clients.discard(websocket)
