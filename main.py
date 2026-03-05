import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import Set

import httpx
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

clients: Set[WebSocket] = set()

# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

GRAPHQL_URL = "https://api.digitransit.fi/routing/v2/waltti/gtfs/v1"
GTFSRT_URL = "https://api.digitransit.fi/realtime/gtfs-rt/v2/waltti/vehicle-positions"

GRAPHQL_HEADERS = {
    "digitransit-subscription-key": config.API_KEY,
    "Content-Type": "application/json",
}

GTFSRT_HEADERS = {
    "digitransit-subscription-key": config.API_KEY,
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
    config.ARRIVALS_COUNT,
)

# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------


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
                    "realtime": st["realtime"],
                }
            )

        state["arrivals"] = [a for a in arrivals if a["minutes"] >= 0]
    except Exception as e:
        print(f"Arrivals fetch error: {e}")


async def fetch_vehicles(client: httpx.AsyncClient) -> None:
    try:
        resp = await client.get(GTFSRT_URL, headers=GTFSRT_HEADERS, timeout=10)
        resp.raise_for_status()

        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(resp.content)

        vehicles = []
        for entity in feed.entity:
            if not entity.HasField("vehicle"):
                continue
            v = entity.vehicle
            if not v.HasField("position"):
                continue

            route_id = ""
            if v.HasField("trip"):
                route_id = v.trip.route_id

            bearing = 0.0
            if v.position.HasField("bearing"):
                bearing = v.position.bearing

            vehicles.append(
                {
                    "id": entity.id,
                    "route": route_id,
                    "lat": v.position.latitude,
                    "lon": v.position.longitude,
                    "bearing": bearing,
                }
            )

        state["vehicles"] = vehicles
    except Exception as e:
        print(f"Vehicles fetch error: {e}")


# ---------------------------------------------------------------------------
# WebSocket broadcast
# ---------------------------------------------------------------------------


async def broadcast() -> None:
    if not clients:
        return
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
# Background polling loop
# ---------------------------------------------------------------------------


async def poll_loop() -> None:
    async with httpx.AsyncClient() as client:
        # Initial fetch of both on startup
        await asyncio.gather(fetch_arrivals(client), fetch_vehicles(client))
        await broadcast()

        arrivals_countdown = config.ARRIVALS_INTERVAL
        while True:
            await asyncio.sleep(config.VEHICLES_INTERVAL)
            arrivals_countdown -= config.VEHICLES_INTERVAL

            tasks = [fetch_vehicles(client)]
            if arrivals_countdown <= 0:
                tasks.append(fetch_arrivals(client))
                arrivals_countdown = config.ARRIVALS_INTERVAL

            await asyncio.gather(*tasks)
            await broadcast()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(poll_loop())
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
    # Send current state immediately so the client doesn't wait for next poll
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
        # Keep connection open; server pushes data, client only listens
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        clients.discard(websocket)
    except Exception:
        clients.discard(websocket)
