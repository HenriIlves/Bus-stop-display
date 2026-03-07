# --- User configuration ---
# Copy this file to config.py and fill in your values.

# Your Digitransit API key from https://portal.digitransit.fi
API_KEY = "YOUR_API_KEY_HERE"

# Your nearest bus stop ID.
# Find it at: https://api.digitransit.fi/graphiql/waltti/v2
# Example query:  { stops(name: "Myllymäki") { gtfsId name } }
STOP_ID = "YourCity:STOP_ID"

# Map center — set to your home coordinates
HOME_LAT = 60.1699
HOME_LON = 24.9384

# Only show these route numbers (case-insensitive). Set to [] to show all routes.
ROUTES_FILTER = ["7", "14", "23"]

# How many upcoming departures to show
ARRIVALS_COUNT = 8

# Polling intervals (seconds)
ARRIVALS_INTERVAL = 20
VEHICLES_INTERVAL = 5
