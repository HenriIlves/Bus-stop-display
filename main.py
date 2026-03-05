import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import Set

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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

# route short name -> gtfsId, populated at startup
route_ids: dict[str, str] = {}

clients: Set[WebSocket] = set()

# ---------------------------------------------------------------------------
# API
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
    config.ARRIVALS_COUNT * 5,  # fetch extra so filtering still yields ARRIVALS_COUNT results
)

# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------


async def resolve_route_ids(client: httpx.AsyncClient) -> None:
    """Fetch gtfsIds for the configured routes once at startup."""
    feed = config.STOP_ID.split(":")[0]  # e.g. "Lappeenranta"
    query = '{ routes(feeds:["%s"]) { gtfsId shortName } }' % feed
    try:
        resp = await client.post(
            GRAPHQL_URL, json={"query": query}, headers=GRAPHQL_HEADERS, timeout=10
        )
        resp.raise_for_status()
        routes = resp.json()["data"]["routes"]
        allowed = {r.lower() for r in config.ROUTES_FILTER}
        for r in routes:
            if not allowed or r["shortName"].lower() in allowed:
                route_ids[r["shortName"]] = r["gtfsId"]
        print(f"Resolved route IDs: {route_ids}")
    except Exception as e:
        print(f"Route ID resolution error: {e}")


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


def build_vehicles_query() -> str:
    """Build a GraphQL query that fetches vehiclePositions for all tracked routes."""
    parts = []
    for short_name, gtfs_id in route_ids.items():
        alias = "r_" + short_name.replace(" ", "_").replace("-", "_")
        parts.append(
            f'{alias}: route(id: "{gtfs_id}") {{'
            f'  shortName'
            f'  patterns {{'
            f'    vehiclePositions {{'
            f'      lat lon heading lastUpdated'
            f'    }}'
            f'  }}'
            f'}}'
        )
    return "{ " + " ".join(parts) + " }"


async def fetch_vehicles(client: httpx.AsyncClient) -> None:
    if not route_ids:
        return
    try:
        query = build_vehicles_query()
        resp = await client.post(
            GRAPHQL_URL, json={"query": query}, headers=GRAPHQL_HEADERS, timeout=10
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})

        vehicles = []
        for route_data in data.values():
            if not route_data:
                continue
            short_name = route_data["shortName"]
            for i, pattern in enumerate(route_data["patterns"]):
                for j, vp in enumerate(pattern["vehiclePositions"]):
                    if vp["lat"] is None or vp["lon"] is None:
                        continue
                    vehicles.append(
                        {
                            "id": f"{short_name}_{i}_{j}",
                            "route": short_name,
                            "lat": vp["lat"],
                            "lon": vp["lon"],
                            "bearing": vp["heading"] or 0,
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
        await resolve_route_ids(client)
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
