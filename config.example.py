# --- User configuration ---
# Copy this file to config.py and fill in your values.

# Your Digitransit API key from https://portal.digitransit.fi
API_KEY = "YOUR_API_KEY_HERE"

# Default stop shown on startup (all routes departing from here)
DEFAULT_STOP_ID = "Lappeenranta:205325"

# Routes to show in both default view and destination routing
ROUTES_FILTER = ["1", "1x", "5"]

# How many upcoming departures / itineraries to show
ARRIVALS_COUNT = 8

# Favorite destinations — each shows a routing plan from from_stop to the destination.
# from_stop:            the stop you depart from (lat/lon fetched automatically from API)
# to_stop:              nearest destination stop  (lat/lon fetched automatically from API,
#                       used as routing target if dest_lat/dest_lon are not set)
# dest_lat / dest_lon:  optional — actual destination coordinates. Use this when the
#                       destination is not at the bus stop itself (e.g. a 1km walk away).
#                       The routing API then includes the final walk in the arrival time.
FAVORITES = [
    {
        "name": "Yliopisto",
        "from_stop": "Lappeenranta:205325",
        "to_stop": "Lappeenranta:205390",
    },
    {
        "name": "Lavola",
        "from_stop": "Lappeenranta:205325",
        # Two destination stops: 205301 for routes 1/1x (50m away),
        # 205393 for route 5 (~500m away). Both are queried and merged.
        "to_stops": ["Lappeenranta:205301", "Lappeenranta:205393"],
    },
    {
        "name": "Sako",
        "from_stop": "Lappeenranta:205325",
        "to_stop": "Lappeenranta:205393",
        "dest_lat": 0.0,  # replace with actual Sako latitude
        "dest_lon": 0.0,  # replace with actual Sako longitude
    },
    {
        "name": "Matkakeskus",
        "from_stop": "Lappeenranta:205324",
        "to_stop": "Lappeenranta:205363",
    },
]

# Map center — set to a position that shows approaching buses well.
# NOTE: this is NOT used as the routing origin; stop coordinates are fetched
# from the API automatically. Rename HOME_LAT/HOME_LON to MAP_LAT/MAP_LON
# in your config.py when updating.
MAP_LAT = 61.0562866
MAP_LON = 28.19533189

# Polling intervals (seconds)
ARRIVALS_INTERVAL = 20
VEHICLES_INTERVAL = 5
