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
    "vehicles": [],
    "stop_name": "",
    "stop_lat": config.HOME_LAT,
    "stop_lon": config.HOME_LON,
}

# Live vehicle positions keyed by vehicle entity ID.
# Updated directly by the MQTT thread; read by the broadcast loop.
vehicle_state: dict = {}

clients: Set[WebSocket] = set()

# ---------------------------------------------------------------------------
# GraphQL — arrivals
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
    config.STOP_ID,
    config.ARRIVALS_COUNT * 5,
)


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
            print("Stop not found — check STOP_ID in config.py")
            return

        state["stop_name"] = stop["name"]
        state["stop_lat"] = stop["lat"]
        state["stop_lon"] = stop["lon"]

        now = int(time.time())
        arrivals = []
        for st in stop["stoptimesWithoutPatterns"]:
            arrival_secs = st["realtimeArrival"] if st["realtime"] else st["scheduledArrival"]
            departure_epoch = st["serviceDay"] + arrival_secs
            minutes = round((departure_epoch - now) / 60)
            arrivals.append(
                {
                    "route": st["trip"]["routeShortName"],
                    "headsign": st["headsign"] or "",
                    "minutes": minutes,
                    "epoch": departure_epoch,
                    "realtime": st["realtime"],
                }
            )

        allowed = {r.lower() for r in config.ROUTES_FILTER}
        state["arrivals"] = [
            a for a in arrivals
            if a["minutes"] >= 0
            and (not allowed or a["route"].lower() in allowed)
        ][:config.ARRIVALS_COUNT]
    except Exception as e:
        print(f"Arrivals fetch error: {e}")


# ---------------------------------------------------------------------------
# MQTT — vehicle positions
# ---------------------------------------------------------------------------

MQTT_HOST = "mqtt.digitransit.fi"
MQTT_PORT = 1883
VEHICLE_STALE_SECONDS = 120  # drop vehicles not updated within this window


def on_mqtt_connect(client, userdata, flags, rc, props=None):
    feed = config.STOP_ID.split(":")[0]  # e.g. "Lappeenranta"
    for route in config.ROUTES_FILTER:
        topic = f"/gtfsrt/vp/{feed}/+/+/+/{route}/#"
        client.subscribe(topic)
        print(f"MQTT subscribed: {topic}")


def on_mqtt_message(client, userdata, msg):
    try:
        # Route short name is at position 7 of the topic
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

    # Flush stale vehicles before broadcasting
    cutoff = time.time() - VEHICLE_STALE_SECONDS
    state["vehicles"] = [
        v for v in vehicle_state.values() if v["updated"] > cutoff
    ]

    message = json.dumps(
        {
            "arrivals": state["arrivals"],
            "vehicles": state["vehicles"],
            "stop_name": state["stop_name"],
            "stop_lat": state["stop_lat"],
            "stop_lon": state["stop_lon"],
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
# Background polling loop (arrivals only — vehicles come from MQTT)
# ---------------------------------------------------------------------------


async def vehicle_broadcast_loop() -> None:
    """Push vehicle positions to clients every 5 seconds."""
    while True:
        await asyncio.sleep(config.VEHICLES_INTERVAL)
        await broadcast()


async def poll_loop() -> None:
    async with httpx.AsyncClient() as client:
        await fetch_arrivals(client)
        await broadcast()

        while True:
            await asyncio.sleep(config.ARRIVALS_INTERVAL)
            await fetch_arrivals(client)
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
                    "vehicles": state["vehicles"],
                    "stop_name": state["stop_name"],
                    "stop_lat": state["stop_lat"],
                    "stop_lon": state["stop_lon"],
                }
            )
        )
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        clients.discard(websocket)
    except Exception:
        clients.discard(websocket)
