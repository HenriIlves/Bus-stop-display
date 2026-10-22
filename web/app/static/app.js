"use strict";

const DEPARTURES_EVERY = 15000;
const VEHICLES_EVERY = 4000;
const SAVED_KEY = "bussit.saved";
const CURRENT_KEY = "bussit.stop";

const state = {
    config: null,
    saved: [], // [{id, name, code}]
    stopId: null,
    stop: null, // last /api/stops/{id} response
    clockOffset: 0, // server time - device time, seconds
    focusRoute: null, // route highlighted by tapping a departure
};

// -----------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------
const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
        if (v == null || v === false) continue;
        if (k === "class") node.className = v;
        else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v);
    }
    for (const c of children) {
        if (c != null && c !== false) node.append(c);
    }
    return node;
}

function nowSec() {
    return Date.now() / 1000 + state.clockOffset;
}

function fmtClock(epochSec) {
    return new Date(epochSec * 1000).toLocaleTimeString("fi-FI", {
        hour: "2-digit",
        minute: "2-digit",
    });
}

function store(key, value) {
    try {
        localStorage.setItem(key, JSON.stringify(value));
    } catch {}
}

function load(key) {
    try {
        return JSON.parse(localStorage.getItem(key));
    } catch {
        return null;
    }
}

async function getJSON(url) {
    const resp = await fetch(url, { cache: "no-store" });
    if (!resp.ok) {
        let detail = resp.statusText;
        try {
            detail = (await resp.json()).detail || detail;
        } catch {}
        const err = new Error(detail);
        err.status = resp.status;
        throw err;
    }
    return resp.json();
}

// -----------------------------------------------------------------------
// Clock
// -----------------------------------------------------------------------
function tick() {
    $("clock").textContent = fmtClock(nowSec());
    if (state.stop) renderDepartures();
}

// -----------------------------------------------------------------------
// Map
// -----------------------------------------------------------------------
let map, stopMarker;
const vehicleMarkers = new Map();

function initMap(view) {
    map = L.map("map", { zoomControl: false }).setView([view.lat, view.lon], view.zoom);
    map.attributionControl.setPrefix(false);

    // Same tiles as the wall display. Off localhost, Stadia needs the domain
    // registered in a (free) Stadia account, or an API key in config.yaml.
    const key = view.stadia_api_key ? `?api_key=${encodeURIComponent(view.stadia_api_key)}` : "";
    L.tileLayer(
        `https://tiles.stadiamaps.com/tiles/alidade_smooth_dark/{z}/{x}/{y}{r}.png${key}`,
        {
            maxZoom: 19,
            attribution:
                '© <a href="https://stadiamaps.com/">Stadia</a> © <a href="https://openmaptiles.org/">OpenMapTiles</a> © <a href="https://www.openstreetmap.org/copyright">OSM</a>',
        },
    ).addTo(map);

    // Buses glide between position updates; that must not happen while
    // Leaflet repositions everything for a zoom.
    const container = map.getContainer();
    map.on("zoomstart", () => container.classList.add("no-glide"));
    map.on("zoomend", () => setTimeout(() => container.classList.remove("no-glide"), 50));

    $("recenter").addEventListener("click", centerOnStop);
}

function centerOnStop() {
    if (state.stop) map.setView([state.stop.lat, state.stop.lon], state.config.map.zoom);
}

function showStopOnMap(stop, recenter) {
    const latlng = [stop.lat, stop.lon];
    if (!stopMarker) {
        stopMarker = L.marker(latlng, {
            icon: L.divIcon({ className: "", html: '<div class="stop-marker"></div>', iconSize: [18, 18] }),
            zIndexOffset: -1000,
            keyboard: false,
        }).addTo(map);
    } else {
        stopMarker.setLatLng(latlng);
    }
    if (recenter) map.setView(latlng, state.config.map.zoom);
}

function busIcon(v) {
    const wrap = el(
        "div",
        { class: "bus" },
        el("div", { class: "bus-arrow" }),
        el("div", { class: "bus-dot" }),
        el("div", { class: "bus-label" }, v.route),
    );
    return L.divIcon({ className: "bus-icon", html: wrap, iconSize: [30, 30] });
}

function styleBus(marker, v) {
    const node = marker.getElement();
    if (!node) return;
    const arrow = node.querySelector(".bus-arrow");
    arrow.style.display = v.bearing == null ? "none" : "";
    arrow.style.transform = `rotate(${v.bearing || 0}deg)`;
    node.classList.toggle("dimmed", state.focusRoute != null && v.route !== state.focusRoute);
    node.title = `${v.route} ${v.headsign}`;
}

function renderVehicles(vehicles) {
    const seen = new Set();
    for (const v of vehicles) {
        seen.add(v.id);
        let marker = vehicleMarkers.get(v.id);
        if (marker && marker.options.route !== v.route) {
            marker.remove();
            marker = null;
        }
        if (!marker) {
            marker = L.marker([v.lat, v.lon], { icon: busIcon(v), route: v.route, keyboard: false }).addTo(map);
            vehicleMarkers.set(v.id, marker);
        } else {
            marker.setLatLng([v.lat, v.lon]);
        }
        marker.vehicle = v;
        styleBus(marker, v);
    }
    for (const [id, marker] of vehicleMarkers) {
        if (!seen.has(id)) {
            marker.remove();
            vehicleMarkers.delete(id);
        }
    }
}

function restyleVehicles() {
    for (const marker of vehicleMarkers.values()) styleBus(marker, marker.vehicle);
}

// -----------------------------------------------------------------------
// Departures
// -----------------------------------------------------------------------
function whenNode(d, now) {
    const minutes = Math.floor((d.time - now) / 60);
    const rt = d.realtime ? el("i", { class: "rt", title: "Reaaliaikainen" }) : null;
    if (d.cancelled) return el("span", { class: "when cancelled-label" }, "Peruttu");
    if (minutes <= 0) return el("span", { class: "when now" }, "Nyt", rt);
    if (minutes <= 15) return el("span", { class: "when" }, el("b", {}, String(minutes)), el("small", {}, "min"), rt);
    return el("span", { class: "when" }, el("b", {}, fmtClock(d.time)), rt);
}

function renderDepartures() {
    const list = $("departures");
    const now = nowSec();
    const deps = state.stop.departures.filter((d) => d.time >= now - 30);

    list.replaceChildren(
        ...deps.map((d) => {
            const late = d.realtime ? Math.round((d.time - d.scheduled) / 60) : 0;
            const classes = ["dep"];
            if (d.cancelled) classes.push("cancelled");
            if (state.focusRoute === d.route) classes.push("focused");
            return el(
                "li",
                { class: classes.join(" "), onclick: () => toggleFocus(d.route) },
                el("span", { class: "badge" }, d.route),
                el(
                    "span",
                    { class: "headsign" },
                    d.headsign,
                    late >= 2 ? el("small", { class: "late" }, ` +${late} min`) : null,
                ),
                whenNode(d, now),
            );
        }),
    );

    const status = $("list-status");
    if (deps.length === 0) status.textContent = "Ei lähtöjä lähiaikoina";
    else if (state.stop.stale) status.textContent = "Tiedot eivät päivity juuri nyt";
    else status.textContent = "";
}

function toggleFocus(route) {
    state.focusRoute = state.focusRoute === route ? null : route;
    renderDepartures();
    restyleVehicles();
}

// -----------------------------------------------------------------------
// Saved stops & chips
// -----------------------------------------------------------------------
function saveStops() {
    store(SAVED_KEY, state.saved);
    renderChips();
    renderSavedList();
}

function isSaved(id) {
    return state.saved.some((s) => s.id === id);
}

function addStop(stop) {
    if (!isSaved(stop.id)) {
        state.saved.push({ id: stop.id, name: stop.name, code: stop.code || null });
        saveStops();
    }
}

function removeStop(id) {
    state.saved = state.saved.filter((s) => s.id !== id);
    saveStops();
}

function moveStop(id, delta) {
    const i = state.saved.findIndex((s) => s.id === id);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= state.saved.length) return;
    [state.saved[i], state.saved[j]] = [state.saved[j], state.saved[i]];
    saveStops();
}

function renderChips() {
    const chips = state.saved.map((s) =>
        el(
            "button",
            {
                type: "button",
                class: "chip" + (s.id === state.stopId ? " active" : ""),
                onclick: () => selectStop(s.id),
            },
            s.name,
        ),
    );
    // A stop opened from search or a shared link, but not saved.
    if (state.stopId && !isSaved(state.stopId)) {
        chips.unshift(
            el("button", { type: "button", class: "chip active unsaved", onclick: () => openDialog() },
                state.stop?.name || "…"),
        );
    }
    chips.push(
        el("button", { type: "button", class: "chip add", onclick: () => openDialog(), "aria-label": "Muokkaa pysäkkejä" }, "＋"),
    );
    $("stop-chips").replaceChildren(...chips);
    $("stop-chips").querySelector(".active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
}

// -----------------------------------------------------------------------
// Stop dialog: search, nearby, saved list
// -----------------------------------------------------------------------
function stopRow(stop, actions) {
    const meta = [stop.code, stop.distance != null ? `${Math.round(stop.distance)} m` : null]
        .filter(Boolean)
        .join(" · ");
    return el(
        "li",
        {},
        el(
            "button",
            { type: "button", class: "stop-main", onclick: () => { selectStop(stop.id); $("stop-dialog").close(); } },
            el("span", { class: "stop-name" }, stop.name, meta ? el("small", {}, meta) : null),
            stop.routes?.length ? el("span", { class: "stop-routes" }, stop.routes.join(" ")) : null,
        ),
        ...actions,
    );
}

function renderSearchResults(stops) {
    $("search-results").replaceChildren(
        ...stops.map((s) => {
            const saved = isSaved(s.id);
            return stopRow(s, [
                el(
                    "button",
                    {
                        type: "button",
                        class: "icon-btn star" + (saved ? " on" : ""),
                        "aria-label": saved ? "Poista omista" : "Lisää omiin",
                        onclick: () => {
                            isSaved(s.id) ? removeStop(s.id) : addStop(s);
                            renderSearchResults(stops);
                        },
                    },
                    saved ? "★" : "☆",
                ),
            ]);
        }),
    );
}

function renderSavedList() {
    const n = state.saved.length;
    $("saved-list").replaceChildren(
        ...state.saved.map((s, i) =>
            stopRow(s, [
                el("button", { type: "button", class: "icon-btn", "aria-label": "Ylös", disabled: i === 0 ? "" : null, onclick: () => moveStop(s.id, -1) }, "↑"),
                el("button", { type: "button", class: "icon-btn", "aria-label": "Poista", onclick: () => removeStop(s.id) }, "✕"),
            ]),
        ),
    );
    if (n === 0) $("saved-list").append(el("li", { class: "empty" }, "Lisää pysäkki hakemalla ja tähdellä ☆"));
}

let searchSeq = 0;
async function runSearch(url) {
    const seq = ++searchSeq;
    $("search-status").textContent = "Haetaan…";
    try {
        const data = await getJSON(url);
        if (seq !== searchSeq) return;
        $("search-status").textContent = data.stops.length ? "" : "Ei pysäkkejä";
        renderSearchResults(data.stops);
    } catch (e) {
        if (seq === searchSeq) $("search-status").textContent = "Haku epäonnistui: " + e.message;
    }
}

let searchTimer;
function onSearchInput() {
    clearTimeout(searchTimer);
    const q = $("stop-search").value.trim();
    if (q.length < 2) {
        searchSeq++;
        $("search-status").textContent = "";
        $("search-results").replaceChildren();
        return;
    }
    searchTimer = setTimeout(() => runSearch(`/api/stops/search?q=${encodeURIComponent(q)}`), 300);
}

function searchNearby() {
    if (!navigator.geolocation) {
        $("search-status").textContent = "Sijainti ei ole käytettävissä";
        return;
    }
    $("search-status").textContent = "Haetaan sijaintia…";
    navigator.geolocation.getCurrentPosition(
        (pos) => runSearch(`/api/stops/nearby?lat=${pos.coords.latitude}&lon=${pos.coords.longitude}`),
        (err) => ($("search-status").textContent = "Sijaintia ei saatu: " + err.message),
        { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 },
    );
}

function openDialog() {
    renderSavedList();
    $("stop-dialog").showModal();
}

// -----------------------------------------------------------------------
// Data loading
// -----------------------------------------------------------------------
async function loadDepartures(recenter = false) {
    const id = state.stopId;
    try {
        const data = await getJSON(`/api/stops/${encodeURIComponent(id)}`);
        if (id !== state.stopId) return; // switched stop while loading
        state.clockOffset = data.now - Date.now() / 1000;
        const first = !state.stop || state.stop.id !== data.id;
        state.stop = data;

        $("stop-name").textContent = data.name;
        $("stop-code").textContent = data.code || "";
        document.title = `${data.name} – Bussit`;
        renderDepartures();
        showStopOnMap(data, recenter || first);

        // Saved stops from config may lack a code until first loaded.
        const saved = state.saved.find((s) => s.id === id);
        if (saved && !saved.code && data.code) {
            saved.code = data.code;
            store(SAVED_KEY, state.saved);
        }
        if (first) {
            renderChips();
            loadVehicles();
        }
    } catch (e) {
        if (id !== state.stopId) return;
        if (e.status === 404) {
            $("stop-name").textContent = "Pysäkkiä ei löydy";
            $("departures").replaceChildren();
        }
        $("list-status").textContent = "Lähtöjä ei saatu: " + e.message;
    }
}

async function loadVehicles() {
    const routes = state.stop?.routes;
    if (!routes || routes.length === 0) {
        renderVehicles([]);
        return;
    }
    try {
        const data = await getJSON(`/api/vehicles?routes=${encodeURIComponent(routes.join(","))}`);
        renderVehicles(data.vehicles);
        $("map-status").textContent = data.vehicles.length ? "" : "Ei busseja liikkeellä";
    } catch {
        $("map-status").textContent = "Bussien sijainnit eivät päivity";
    }
}

function selectStop(id) {
    if (!id) return;
    if (id !== state.stopId) {
        state.stopId = id;
        state.stop = null;
        state.focusRoute = null;
        store(CURRENT_KEY, id);
        history.replaceState(null, "", "#" + encodeURIComponent(id));
        $("stop-name").textContent = state.saved.find((s) => s.id === id)?.name || "Ladataan…";
        $("stop-code").textContent = "";
        $("departures").replaceChildren();
        $("list-status").textContent = "Ladataan…";
        renderVehicles([]);
        renderChips();
    }
    loadDepartures(true);
}

// Poll while the page is visible; refresh at once when it comes back.
function every(ms, fn) {
    setInterval(() => {
        if (!document.hidden && state.stopId) fn();
    }, ms);
}

document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.stopId) {
        loadDepartures();
        loadVehicles();
    }
});

window.addEventListener("hashchange", () => {
    const id = decodeURIComponent(location.hash.slice(1));
    if (id && id !== state.stopId) selectStop(id);
});

// -----------------------------------------------------------------------
// Start
// -----------------------------------------------------------------------
async function start() {
    tick();
    setInterval(tick, 5000);

    $("stop-search").addEventListener("input", onSearchInput);
    $("nearby-btn").addEventListener("click", searchNearby);
    $("stop-dialog").addEventListener("click", (e) => {
        if (e.target === $("stop-dialog")) $("stop-dialog").close(); // backdrop tap
    });

    try {
        state.config = await getJSON("/api/config");
    } catch (e) {
        $("stop-name").textContent = "Palvelin ei vastaa";
        $("list-status").textContent = e.message;
        setTimeout(start, 10000);
        return;
    }

    initMap(state.config.map);

    const saved = load(SAVED_KEY);
    state.saved = Array.isArray(saved) ? saved : state.config.stops;
    renderChips();

    const fromHash = decodeURIComponent(location.hash.slice(1));
    const initial = fromHash || load(CURRENT_KEY) || state.saved[0]?.id || state.config.stops[0]?.id;
    if (initial) {
        selectStop(initial);
    } else {
        $("stop-name").textContent = "Valitse pysäkki";
        $("list-status").textContent = "";
        openDialog();
    }

    every(DEPARTURES_EVERY, () => loadDepartures());
    every(VEHICLES_EVERY, loadVehicles);
}

start();
