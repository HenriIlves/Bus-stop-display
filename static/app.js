// -----------------------------------------------------------------------
// Clock
// -----------------------------------------------------------------------
function updateClock() {
  const now = new Date();
  const h = String(now.getHours()).padStart(2, '0');
  const m = String(now.getMinutes()).padStart(2, '0');
  document.getElementById('clock').textContent = h + ':' + m;
}
updateClock();
setInterval(updateClock, 10000);

// -----------------------------------------------------------------------
// Map setup
// -----------------------------------------------------------------------
const map = L.map('map', {
  zoomControl: false,
  attributionControl: false,
}).setView([61.0587, 28.1873], 14);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
}).addTo(map);

// Stop marker (blue dot)
const stopIcon = L.divIcon({
  className: '',
  html: '<div class="stop-marker"></div>',
  iconSize: [16, 16],
  iconAnchor: [8, 8],
});
let stopMarker = null;

// Vehicle markers keyed by entity id
const vehicleMarkers = {};

function makeBusIcon(route, bearing) {
  const deg = bearing || 0;
  return L.divIcon({
    className: '',
    html: `
      <div class="bus-marker-wrap">
        <div class="bus-arrow" style="transform: rotate(${deg}deg)">
          <svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
            <circle cx="16" cy="16" r="13" fill="#1f6feb" stroke="#fff" stroke-width="2"/>
            <polygon points="16,5 21,23 16,19 11,23" fill="#fff" opacity="0.9"/>
          </svg>
        </div>
        <div class="bus-label">${route || '?'}</div>
      </div>`,
    iconSize: [32, 46],
    iconAnchor: [16, 16],
  });
}

// -----------------------------------------------------------------------
// Render arrivals
// -----------------------------------------------------------------------
function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function renderArrivals(arrivals) {
  const tbody = document.getElementById('arrivals-body');
  if (!arrivals || arrivals.length === 0) {
    tbody.innerHTML = '<tr><td colspan="3" class="no-arrivals">Ei lahtoja</td></tr>';
    return;
  }

  tbody.innerHTML = arrivals.map(a => {
    const realtimeDot = a.realtime ? '<span class="realtime-dot"></span>' : '';
    let timeHtml;
    if (a.minutes === 0) {
      timeHtml = `<span class="time-now">Nyt${a.realtime ? '<span class="realtime-dot"></span>' : ''}</span>`;
    } else {
      timeHtml = `<span class="time-value">${a.minutes}</span><span class="time-unit">min</span>${realtimeDot}`;
    }
    return `
      <tr class="arrival-row">
        <td class="col-route"><span class="route-badge">${escHtml(a.route)}</span></td>
        <td class="col-dest">${escHtml(a.headsign)}</td>
        <td class="col-time">${timeHtml}</td>
      </tr>`;
  }).join('');
}

// -----------------------------------------------------------------------
// Render vehicles
// -----------------------------------------------------------------------
function renderVehicles(vehicles) {
  const seen = new Set();
  for (const v of vehicles) {
    seen.add(v.id);
    const icon = makeBusIcon(v.route, v.bearing);
    if (vehicleMarkers[v.id]) {
      vehicleMarkers[v.id].setLatLng([v.lat, v.lon]);
      vehicleMarkers[v.id].setIcon(icon);
    } else {
      vehicleMarkers[v.id] = L.marker([v.lat, v.lon], { icon }).addTo(map);
    }
  }
  // Remove markers for vehicles no longer in feed
  for (const id of Object.keys(vehicleMarkers)) {
    if (!seen.has(id)) {
      map.removeLayer(vehicleMarkers[id]);
      delete vehicleMarkers[id];
    }
  }
}

// -----------------------------------------------------------------------
// WebSocket
// -----------------------------------------------------------------------
let mapCentered = false;

function connect() {
  const ws = new WebSocket('ws://' + location.host + '/ws');

  ws.onopen = () => {
    document.getElementById('status').textContent = 'Yhdistetty';
    setTimeout(() => {
      document.getElementById('status').textContent = '';
    }, 3000);
  };

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);

    if (data.stop_name) {
      document.getElementById('stop-name').textContent = data.stop_name;
    }

    // Center map on stop once we have coordinates
    if (!mapCentered && data.stop_lat && data.stop_lon) {
      map.setView([data.stop_lat, data.stop_lon], 15);
      if (stopMarker) map.removeLayer(stopMarker);
      stopMarker = L.marker([data.stop_lat, data.stop_lon], { icon: stopIcon }).addTo(map);
      mapCentered = true;
    }

    renderArrivals(data.arrivals);
    renderVehicles(data.vehicles || []);
  };

  ws.onclose = () => {
    document.getElementById('status').textContent = 'Yhteys katkesi — yhdistetaan...';
    setTimeout(connect, 3000);
  };

  ws.onerror = () => {
    ws.close();
  };
}

connect();
