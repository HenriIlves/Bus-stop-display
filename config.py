# --- User configuration ---

# Your Digitransit API key from https://portal.digitransit.fi
API_KEY = "YOUR_API_KEY_HERE"

# Your nearest bus stop ID.
# Find it at: https://api.digitransit.fi/graphiql/waltti/v2
# Example query:  { stops(name: "Myllymäki") { gtfsId name } }
STOP_ID = "waltti:STOP_ID_HERE"

# Map center — set to your home coordinates
HOME_LAT = 61.0587
HOME_LON = 28.1873

# How many upcoming departures to show
ARRIVALS_COUNT = 8

# Polling intervals (seconds)
ARRIVALS_INTERVAL = 30
VEHICLES_INTERVAL = 5
