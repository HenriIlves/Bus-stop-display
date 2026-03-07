# --- User configuration ---
# Copy this file to config.py and fill in your values.

# Your Digitransit API key from https://portal.digitransit.fi
API_KEY = "YOUR_API_KEY_HERE"

# Default stop shown on startup (all routes departing from here)
DEFAULT_STOP_ID = "YourCity:STOP_ID"

# Routes to show in both default view and destination routing
ROUTES_FILTER = ["7", "14", "23"]

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
        "name": "Work",
        "from_stop": "YourCity:STOP_ID",
        "to_stop": "YourCity:STOP_ID_2",
    },
    {
        "name": "School",
        "from_stop": "YourCity:STOP_ID",
        # Two destination stops for different routes. Both are queried and merged.
        "to_stops": ["YourCity:STOP_ID_3", "YourCity:STOP_ID_4"],
    },
    {
        "name": "Mall",
        "from_stop": "YourCity:STOP_ID",
        "to_stop": "YourCity:STOP_ID_5",
        "dest_lat": 60.1710,  # replace with actual destination latitude
        "dest_lon": 24.9420,  # replace with actual destination longitude
    },
    {
        "name": "Station",
        "from_stop": "YourCity:STOP_ID_6",
        "to_stop": "YourCity:STOP_ID_7",
    },
]

# Map center — set to a position that shows approaching buses well.
# NOTE: this is NOT used as the routing origin; stop coordinates are fetched
# from the API automatically.
MAP_LAT = 60.1699
MAP_LON = 24.9384

# Polling intervals (seconds)
ARRIVALS_INTERVAL = 20
VEHICLES_INTERVAL = 5
