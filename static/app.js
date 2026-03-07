// -----------------------------------------------------------------------
// Clock
// -----------------------------------------------------------------------
function updateClock() {
    const now = new Date();
    const h = String(now.getHours()).padStart(2, "0");
    const m = String(now.getMinutes()).padStart(2, "0");
    document.getElementById("clock").textContent = h + ":" + m;
}
updateClock();
setInterval(updateClock, 10000);

// -----------------------------------------------------------------------
// Map setup
// -----------------------------------------------------------------------
const map = L.map("map", {
    zoomControl: false,
    attributionControl: false,
    dragging: false,
    touchZoom: false,
    scrollWheelZoom: false,
    doubleClickZoom: false,
    boxZoom: false,
    keyboard: false,
}).setView([61.0562866, 28.19533189], 12);

L.tileLayer(
    "https://tiles.stadiamaps.com/tiles/alidade_smooth_dark/{z}/{x}/{y}{r}.png",
    { maxZoom: 19 },
).addTo(map);

const stopIcon = L.divIcon({
    className: "",
    html: '<div class="stop-marker"></div>',
    iconSize: [16, 16],
    iconAnchor: [8, 8],
});

const destIcon = L.divIcon({
    className: "",
    html: '<div class="dest-marker"></div>',
    iconSize: [16, 16],
    iconAnchor: [8, 8],
});

let stopMarker = null;
let destMarker = null;
const vehicleMarkers = {};

function makeBusIcon(route, bearing) {
    const deg = bearing || 0;
    return L.divIcon({
        className: "",
        html: `
      <div class="bus-marker-wrap">
        <div class="bus-arrow" style="transform: rotate(${deg}deg)">
          <svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
            <circle cx="16" cy="16" r="13" fill="#C70067" stroke="#fff" stroke-width="2"/>
            <polygon points="16,5 21,23 16,19 11,23" fill="#fff" opacity="0.9"/>
          </svg>
        </div>
        <div class="bus-label">${route || "?"}</div>
      </div>`,
        iconSize: [32, 46],
        iconAnchor: [16, 16],
    });
}

// -----------------------------------------------------------------------
// State
// -----------------------------------------------------------------------
let selectedDest = null; // null = default view, or destination name string
let fabOpen = false;
let currentData = null;

// -----------------------------------------------------------------------
// Utilities
// -----------------------------------------------------------------------
function escHtml(str) {
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function fmtClock(epochSec) {
    const d = new Date(epochSec * 1000);
    return (
        String(d.getHours()).padStart(2, "0") +
        ":" +
        String(d.getMinutes()).padStart(2, "0")
    );
}

// -----------------------------------------------------------------------
// FAB
// -----------------------------------------------------------------------
function toggleFab() {
    fabOpen = !fabOpen;
    document.getElementById("fab-menu").classList.toggle("open", fabOpen);
}

function selectDest(name) {
    selectedDest = name;
    fabOpen = false;
    document.getElementById("fab-menu").classList.remove("open");
    updateFabBtn();
    rebuildPanel();
    updateDestMarker();
}

function clearDest() {
    selectedDest = null;
    fabOpen = false;
    document.getElementById("fab-menu").classList.remove("open");
    updateFabBtn();
    rebuildPanel();
    updateDestMarker();
}

function updateFabBtn() {
    document.getElementById("fab-btn").textContent = selectedDest || "Koti";
}

function renderFabMenu(favoritesMeta) {
    let html = "";
    for (const fav of favoritesMeta) {
        const active = selectedDest === fav.name ? " active" : "";
        html += `<button class="fab-dest-btn${active}" onclick="selectDest('${escHtml(fav.name)}')">${escHtml(fav.name)}</button>`;
    }
    if (selectedDest) {
        html += `<button class="fab-dest-btn koti" onclick="clearDest()">✕ Koti</button>`;
    }
    document.getElementById("fab-menu").innerHTML = html;
}

function updateDestMarker() {
    if (destMarker) {
        map.removeLayer(destMarker);
        destMarker = null;
    }
    if (!selectedDest || !currentData) return;
    const fav = (currentData.favorites_meta || []).find(
        (f) => f.name === selectedDest,
    );
    if (fav && fav.dest_lat && fav.dest_lon) {
        destMarker = L.marker([fav.dest_lat, fav.dest_lon], {
            icon: destIcon,
        }).addTo(map);
    }
}

// -----------------------------------------------------------------------
// Arrivals panel
// -----------------------------------------------------------------------
function rebuildPanel() {
    const panel = document.getElementById("arrivals-panel");

    if (!selectedDest) {
        panel.innerHTML = `
            <table id="arrivals-table" class="mode-default">
                <colgroup><col><col></colgroup>
                <tbody id="arrivals-body">
                    <tr><td colspan="2" class="no-arrivals">Ladataan...</td></tr>
                </tbody>
            </table>`;
        if (currentData) renderArrivals(currentData.arrivals);
    } else {
        panel.innerHTML = `
            <div id="dest-header">→ ${escHtml(selectedDest)}</div>
            <table id="arrivals-table" class="mode-dest">
                <colgroup><col><col><col></colgroup>
                <tbody id="arrivals-body">
                    <tr><td colspan="3" class="no-arrivals">Ladataan...</td></tr>
                </tbody>
            </table>`;
        if (currentData)
            renderItineraries(
                (currentData.itineraries || {})[selectedDest] || [],
            );
    }
}

function renderArrivals(arrivals) {
    const tbody = document.getElementById("arrivals-body");
    if (!arrivals || arrivals.length === 0) {
        tbody.innerHTML =
            '<tr><td colspan="2" class="no-arrivals">Ei lahtoja</td></tr>';
        return;
    }
    tbody.innerHTML = arrivals
        .map((a) => {
            const dot = a.realtime
                ? '<span class="realtime-dot"></span>'
                : "";
            let timeHtml;
            if (a.minutes === 0) {
                timeHtml = `<span class="time-now">Nyt${a.realtime ? '<span class="realtime-dot"></span>' : ""}</span>`;
            } else if (a.minutes <= 10) {
                timeHtml = `<span class="time-value">${a.minutes}</span><span class="time-unit">min</span>${dot}`;
            } else {
                timeHtml = `<span class="time-value">${fmtClock(a.epoch)}</span>${dot}`;
            }
            return `<tr class="arrival-row">
                <td class="col-route"><span class="route-badge">${escHtml(a.route)}</span></td>
                <td class="col-time">${timeHtml}</td>
            </tr>`;
        })
        .join("");
}

function renderItineraries(itineraries) {
    const tbody = document.getElementById("arrivals-body");
    if (!itineraries || itineraries.length === 0) {
        tbody.innerHTML =
            '<tr><td colspan="3" class="no-arrivals">Ei reittejä</td></tr>';
        return;
    }
    tbody.innerHTML = itineraries
        .map((it) => {
            const dot = it.realtime
                ? '<span class="realtime-dot"></span>'
                : "";
            let leavesHtml;
            if (it.minutes === 0) {
                leavesHtml = `<span class="time-now">Nyt</span>`;
            } else if (it.minutes <= 10) {
                leavesHtml = `<span class="time-value">${it.minutes}</span><span class="time-unit">min</span>${dot}`;
            } else {
                leavesHtml = `<span class="time-value">${fmtClock(it.departs)}</span>${dot}`;
            }
            const walkHtml =
                it.walk_after > 0
                    ? `<span class="walk-after">(+${it.walk_after})</span>`
                    : "";
            const arrivesHtml = `<span class="time-value">${fmtClock(it.arrives)}</span>${walkHtml}`;
            return `<tr class="arrival-row">
                <td class="col-route"><span class="route-badge">${escHtml(it.route)}</span></td>
                <td class="col-time">${leavesHtml}</td>
                <td class="col-time">${arrivesHtml}</td>
            </tr>`;
        })
        .join("");
}

// -----------------------------------------------------------------------
// Vehicles
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
            vehicleMarkers[v.id] = L.marker([v.lat, v.lon], { icon }).addTo(
                map,
            );
        }
    }
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
function connect() {
    const ws = new WebSocket("ws://" + location.host + "/ws");

    ws.onopen = () => {
        document.getElementById("status").textContent = "Yhdistetty";
        setTimeout(() => {
            document.getElementById("status").textContent = "";
        }, 3000);
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        currentData = data;

        if (data.stop_name) {
            document.getElementById("stop-name").textContent = data.stop_name;
        }

        if (data.stop_lat && data.stop_lon) {
            if (stopMarker) map.removeLayer(stopMarker);
            stopMarker = L.marker([data.stop_lat, data.stop_lon], {
                icon: stopIcon,
            }).addTo(map);
        }

        if (data.favorites_meta) {
            renderFabMenu(data.favorites_meta);
        }

        // Update arrivals or itineraries depending on current mode
        if (!selectedDest) {
            renderArrivals(data.arrivals);
        } else {
            renderItineraries((data.itineraries || {})[selectedDest] || []);
        }

        renderVehicles(data.vehicles || []);
    };

    ws.onclose = () => {
        document.getElementById("status").textContent =
            "Yhteys katkesi — yhdistetaan...";
        setTimeout(connect, 3000);
    };

    ws.onerror = () => {
        ws.close();
    };
}

connect();
