"""Bus departures and live bus map, served as a phone-friendly website.

Departures and stop search come from the Digitransit GraphQL API (cached per
stop, fetched on demand). Live vehicle positions stream in from the Digitransit
MQTT broker for the whole feed and are filtered per request.
"""

import asyncio
import logging
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import paho.mqtt.client as mqtt
import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.transit import gtfs_realtime_pb2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bussit")

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", BASE_DIR / "config.yaml"))
API_KEY = os.environ.get("DIGITRANSIT_API_KEY", "")
GRAPHQL_URL = os.environ.get(
    "DIGITRANSIT_URL", "https://api.digitransit.fi/routing/v2/waltti/gtfs/v1"
)
MQTT_HOST = os.environ.get("MQTT_HOST", "mqtt.digitransit.fi")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

STOP_TTL = 15  # seconds a stop's departures are cached
VEHICLE_STALE = 90  # seconds without an update before a vehicle is dropped


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    cfg["stops"] = [s if isinstance(s, dict) else {"id": s} for s in cfg.get("stops") or []]
    cfg.setdefault("departures", 12)
    cfg.setdefault("timezone", "Europe/Helsinki")
    cfg.setdefault("map", {})
    if not cfg.get("feed"):
        if not cfg["stops"]:
            raise SystemExit(f"{CONFIG_PATH}: set 'feed' (e.g. Lappeenranta) or at least one stop")
        cfg["feed"] = cfg["stops"][0]["id"].split(":")[0]
    return cfg


CONFIG = load_config()
FEED = CONFIG["feed"]
TZ = ZoneInfo(CONFIG["timezone"])
STOP_ID_RE = re.compile(rf"^{re.escape(FEED)}:[A-Za-z0-9_.\-]+$")

if not API_KEY:
    log.warning("DIGITRANSIT_API_KEY is not set: departures and stop search will fail")

# ---------------------------------------------------------------------------
# Digitransit GraphQL
# ---------------------------------------------------------------------------

STOP_QUERY = """
query Stop($id: String!, $n: Int!) {
  stop(id: $id) {
    gtfsId name code lat lon
    routes { shortName }
    stoptimesWithoutPatterns(numberOfDepartures: $n, omitNonPickups: true, omitCanceled: false) {
      serviceDay scheduledDeparture realtimeDeparture realtime realtimeState headsign
      trip { routeShortName tripHeadsign }
    }
  }
}
"""

SEARCH_QUERY = """
query Search($q: String!) {
  stops(name: $q, maxResults: 60) { gtfsId name code lat lon routes { shortName } }
}
"""

NEARBY_QUERY = """
query Nearby($lat: Float!, $lon: Float!) {
  stopsByRadius(lat: $lat, lon: $lon, radius: 1000, first: 40) {
    edges { node { distance stop { gtfsId name code lat lon routes { shortName } } } }
  }
}
"""

http: httpx.AsyncClient | None = None


async def gql(query: str, variables: dict) -> dict:
    resp = await http.post(
        GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers={"digitransit-subscription-key": API_KEY},
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        raise RuntimeError(body["errors"][0].get("message", "GraphQL error"))
    return body["data"]


def route_sort_key(name: str):
    """Sort route names naturally: 2, 7, 14, 14A, N1."""
    m = re.match(r"\d+", name)
    return (int(m.group()) if m else 10**9, name)


def route_names(routes) -> list:
    return sorted({r["shortName"] for r in routes or [] if r.get("shortName")}, key=route_sort_key)


def stop_summary(stop: dict) -> dict:
    return {
        "id": stop["gtfsId"],
        "name": stop["name"],
        "code": stop.get("code"),
        "lat": stop["lat"],
        "lon": stop["lon"],
        "routes": route_names(stop.get("routes")),
    }


def route_filter(stop_id: str) -> set:
    """Routes to show for a stop (lowercased); empty means all.

    A configured stop's own `routes` list wins over the top-level one.
    """
    for s in CONFIG["stops"]:
        if s["id"] == stop_id and "routes" in s:
            return {str(r).lower() for r in s["routes"] or []}
    return {str(r).lower() for r in CONFIG.get("routes") or []}


async def fetch_stop(stop_id: str) -> dict | None:
    allowed = route_filter(stop_id)
    # Filtering throws departures away, so ask for more of them.
    n = CONFIG["departures"] * (5 if allowed else 2)
    data = await gql(STOP_QUERY, {"id": stop_id, "n": n})
    stop = data.get("stop")
    if not stop:
        return None

    now = time.time()
    departures = []
    for st in stop["stoptimesWithoutPatterns"] or []:
        offset = st["realtimeDeparture"] if st["realtime"] else st["scheduledDeparture"]
        t = st["serviceDay"] + offset
        if t < now - 30:
            continue
        trip = st.get("trip") or {}
        route = trip.get("routeShortName") or "?"
        if allowed and route.lower() not in allowed:
            continue
        departures.append(
            {
                "route": route,
                "headsign": st.get("headsign") or trip.get("tripHeadsign") or "",
                "time": t,
                "scheduled": st["serviceDay"] + st["scheduledDeparture"],
                "realtime": bool(st["realtime"]),
                "cancelled": st.get("realtimeState") == "CANCELED",
            }
        )
    departures.sort(key=lambda d: d["time"])

    summary = stop_summary(stop)
    if allowed:
        # Also limits which buses the map shows for this stop.
        summary["routes"] = [r for r in summary["routes"] if r.lower() in allowed]
    return {
        **summary,
        "departures": departures[: CONFIG["departures"]],
        "updated": int(now),
    }


_stop_cache: dict[str, tuple[float, dict]] = {}
_stop_locks: dict[str, asyncio.Lock] = {}


async def get_stop(stop_id: str) -> dict:
    """Return a stop with departures, at most STOP_TTL seconds old.

    Concurrent requests for the same stop share one upstream fetch. If
    Digitransit fails, the last good result is returned marked as stale.
    """
    if not STOP_ID_RE.match(stop_id):
        raise HTTPException(404, f"Unknown stop id (stops must belong to the {FEED} feed)")

    cached = _stop_cache.get(stop_id)
    if cached and time.time() - cached[0] < STOP_TTL:
        return cached[1]

    lock = _stop_locks.setdefault(stop_id, asyncio.Lock())
    async with lock:
        cached = _stop_cache.get(stop_id)
        if cached and time.time() - cached[0] < STOP_TTL:
            return cached[1]
        try:
            data = await fetch_stop(stop_id)
        except Exception as e:
            log.warning("Stop fetch failed for %s: %s", stop_id, e)
            if cached:
                return {**cached[1], "stale": True}
            raise HTTPException(502, "Digitransit request failed")
        if data is None:
            raise HTTPException(404, "Stop not found")

        _stop_cache[stop_id] = (time.time(), data)
        # Forget stops nobody has looked at in a while.
        cutoff = time.time() - 600
        for sid in [s for s, (ts, _) in _stop_cache.items() if ts < cutoff]:
            _stop_cache.pop(sid, None)
            _stop_locks.pop(sid, None)
        return data


# ---------------------------------------------------------------------------
# MQTT vehicle positions
# ---------------------------------------------------------------------------

_vehicles: dict[str, dict] = {}
_vehicles_lock = threading.Lock()
mqtt_connected = False


def on_mqtt_connect(client, userdata, flags, reason_code, properties=None):
    global mqtt_connected
    if reason_code.is_failure:
        log.warning("MQTT connect failed: %s", reason_code)
        return
    mqtt_connected = True
    topic = f"/gtfsrt/vp/{FEED}/#"
    client.subscribe(topic)
    log.info("MQTT subscribed: %s", topic)


def on_mqtt_disconnect(client, userdata, flags, reason_code, properties=None):
    global mqtt_connected
    mqtt_connected = False
    log.warning("MQTT disconnected: %s", reason_code)


def on_mqtt_message(client, userdata, msg):
    # Topic: /gtfsrt/vp/<feed>/<agency_id>/<agency>/<mode>/<route_id>/<direction>/
    #        <headsign>/<trip_id>/<next_stop>/<start_time>/<vehicle_id>/
    #        <geohash, 4 segments>/<short_name>/<color>/
    # The route short name is not in the protobuf, so read it from the end of the topic.
    parts = msg.topic.split("/")
    if len(parts) < 12:
        return
    tail = parts[:-1] if parts[-1] == "" else parts
    route = tail[-2]
    headsign = parts[9]

    try:
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(msg.payload)
    except Exception as e:
        log.debug("Bad MQTT payload on %s: %s", msg.topic, e)
        return

    now = time.time()
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        v = entity.vehicle
        if not v.HasField("position"):
            continue
        record = {
            "id": entity.id,
            "route": route,
            "headsign": headsign,
            "lat": round(v.position.latitude, 6),
            "lon": round(v.position.longitude, 6),
            "bearing": v.position.bearing if v.position.HasField("bearing") else None,
            "seen": now,
        }
        with _vehicles_lock:
            _vehicles[entity.id] = record


def run_mqtt() -> None:
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, client_id=f"bussit-{secrets.token_hex(4)}"
    )
    client.on_connect = on_mqtt_connect
    client.on_disconnect = on_mqtt_disconnect
    client.on_message = on_mqtt_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    # Reconnects on its own after network drops, including the first connect.
    client.loop_forever(retry_first_connection=True)


def current_vehicles(routes: set | None) -> list:
    cutoff = time.time() - VEHICLE_STALE
    with _vehicles_lock:
        for vid in [k for k, v in _vehicles.items() if v["seen"] < cutoff]:
            del _vehicles[vid]
        vehicles = list(_vehicles.values())
    if routes:
        vehicles = [v for v in vehicles if v["route"] in routes]
    return [{k: v for k, v in veh.items() if k != "seen"} for veh in vehicles]


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http
    http = httpx.AsyncClient()
    threading.Thread(target=run_mqtt, daemon=True, name="mqtt").start()
    yield
    await http.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/healthz")
async def healthz():
    with _vehicles_lock:
        n = len(_vehicles)
    return {"ok": True, "mqtt": mqtt_connected, "vehicles": n}


@app.get("/api/config")
async def api_config():
    """Default quick-pick stops (named) and the initial map view."""
    stops = []
    results = await asyncio.gather(
        *(get_stop(s["id"]) for s in CONFIG["stops"]), return_exceptions=True
    )
    for s, res in zip(CONFIG["stops"], results):
        name = res["name"] if isinstance(res, dict) else None
        stops.append(
            {
                "id": s["id"],
                "name": s.get("label") or name or s["id"],
                "code": res.get("code") if isinstance(res, dict) else None,
            }
        )
    m = CONFIG["map"]
    return {
        "feed": FEED,
        "stops": stops,
        "map": {
            "lat": m.get("lat", 61.0587),
            "lon": m.get("lon", 28.1887),
            "zoom": m.get("zoom", 14),
            "stadia_api_key": m.get("stadia_api_key"),
        },
    }


@app.get("/api/stops/search")
async def api_search(q: str = Query(min_length=2, max_length=60)):
    try:
        data = await gql(SEARCH_QUERY, {"q": q})
    except Exception as e:
        log.warning("Stop search failed: %s", e)
        raise HTTPException(502, "Digitransit request failed")
    stops = [stop_summary(s) for s in data.get("stops") or [] if STOP_ID_RE.match(s["gtfsId"])]
    return {"stops": stops[:25]}


@app.get("/api/stops/nearby")
async def api_nearby(lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180)):
    try:
        data = await gql(NEARBY_QUERY, {"lat": lat, "lon": lon})
    except Exception as e:
        log.warning("Nearby search failed: %s", e)
        raise HTTPException(502, "Digitransit request failed")
    stops = []
    for edge in (data.get("stopsByRadius") or {}).get("edges") or []:
        node = edge["node"]
        if node["stop"] and STOP_ID_RE.match(node["stop"]["gtfsId"]):
            stops.append({**stop_summary(node["stop"]), "distance": node["distance"]})
    return {"stops": stops[:25]}


@app.get("/api/stops/{stop_id}")
async def api_stop(stop_id: str):
    return {**await get_stop(stop_id), "now": time.time()}


@app.get("/api/vehicles")
async def api_vehicles(routes: str = ""):
    wanted = {r for r in routes.split(",") if r}
    return {"vehicles": current_vehicles(wanted), "now": time.time()}


def format_when(t: float, now: float) -> str:
    minutes = int((t - now) // 60)
    if minutes <= 0:
        return "Nyt"
    if minutes <= 15:
        return f"{minutes} min"
    return datetime.fromtimestamp(t, TZ).strftime("%H:%M")


@app.get("/api/homepage")
async def api_homepage(stop: str | None = None, count: int = Query(5, ge=1, le=20)):
    """Pre-formatted departures for a gethomepage.dev customapi widget."""
    stop_id = stop or (CONFIG["stops"][0]["id"] if CONFIG["stops"] else None)
    if not stop_id:
        raise HTTPException(400, "No stop given and none configured")
    data = await get_stop(stop_id)
    now = time.time()
    departures = [
        {
            "title": f"{d['route']} {d['headsign']}".strip(),
            "when": "Peruttu" if d["cancelled"] else format_when(d["time"], now),
            "route": d["route"],
            "headsign": d["headsign"],
            "time": d["time"],
        }
        for d in data["departures"]
        if d["time"] >= now - 30
    ][:count]
    return {"stop": data["name"], "code": data.get("code"), "departures": departures}
