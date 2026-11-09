//! Bus departures and live bus map, served as a phone-friendly website.
//!
//! Departures and stop search come from the Digitransit GraphQL API (cached per
//! stop, fetched on demand). Live vehicle positions stream in from the Digitransit
//! MQTT broker for the whole feed and are filtered per request.

use std::collections::hash_map::RandomState;
use std::collections::{BTreeSet, HashMap, HashSet};
use std::env;
use std::hash::BuildHasher;
use std::path::Path as FilePath;
use std::str::FromStr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, anyhow, bail};
use axum::extract::{Path, Query, State};
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, get_service};
use axum::{Json, Router};
use chrono::DateTime;
use chrono_tz::Tz;
use futures_util::future::join_all;
use prost::Message;
use rumqttc::{AsyncClient, Event, MqttOptions, Packet, QoS};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{Value, json};
use tokio::signal::unix::{SignalKind, signal};
use tokio::sync::Mutex as AsyncMutex;
use tower_http::services::{ServeDir, ServeFile};
use tower_http::set_header::SetResponseHeaderLayer;
use tracing::{debug, info, warn};

const STOP_TTL: f64 = 15.0; // seconds a stop's departures are cached
const VEHICLE_STALE: f64 = 90.0; // seconds without an update before a vehicle is dropped

fn env_or(name: &str, default: &str) -> String {
    env::var(name).unwrap_or_else(|_| default.to_string())
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before 1970")
        .as_secs_f64()
}

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct RawConfig {
    feed: Option<String>,
    stops: Option<Vec<StopEntry>>,
    routes: Option<Vec<RouteName>>,
    departures: Option<usize>,
    timezone: Option<String>,
    map: Option<MapConfig>,
}

/// A stop is either a bare id or a mapping with an id and options.
#[derive(Deserialize)]
#[serde(untagged)]
enum StopEntry {
    Id(String),
    Full(StopConfig),
}

#[derive(Deserialize)]
struct StopConfig {
    id: String,
    label: Option<String>,
    // None when the key is left out; an empty `routes:` still overrides the top-level list.
    #[serde(default, deserialize_with = "present_routes")]
    routes: Option<HashSet<String>>,
}

/// YAML reads `routes: [7, 14A]` as a number and a string.
#[derive(Deserialize)]
#[serde(untagged)]
enum RouteName {
    Text(String),
    Number(serde_json::Number),
}

#[derive(Default, Deserialize)]
struct MapConfig {
    lat: Option<Value>,
    lon: Option<Value>,
    zoom: Option<Value>,
    stadia_api_key: Option<String>,
}

struct Config {
    feed: String,
    stops: Vec<StopConfig>,
    routes: HashSet<String>,
    departures: usize,
    timezone: Tz,
    map: MapConfig,
}

fn lowercase_routes(routes: Vec<RouteName>) -> HashSet<String> {
    routes
        .into_iter()
        .map(|r| match r {
            RouteName::Text(s) => s.to_lowercase(),
            RouteName::Number(n) => n.to_string(),
        })
        .collect()
}

fn present_routes<'de, D: Deserializer<'de>>(d: D) -> Result<Option<HashSet<String>>, D::Error> {
    let routes = Option::<Vec<RouteName>>::deserialize(d)?;
    Ok(Some(lowercase_routes(routes.unwrap_or_default())))
}

fn load_config(path: &str) -> anyhow::Result<Config> {
    let text = std::fs::read_to_string(path).with_context(|| format!("{path}: cannot read"))?;
    let raw: RawConfig = serde_norway::from_str(&text).with_context(|| format!("{path}: invalid config"))?;

    let stops: Vec<StopConfig> = raw
        .stops
        .unwrap_or_default()
        .into_iter()
        .map(|s| match s {
            StopEntry::Id(id) => StopConfig { id, label: None, routes: None },
            StopEntry::Full(stop) => stop,
        })
        .collect();

    let feed = match raw.feed.filter(|f| !f.is_empty()) {
        Some(feed) => feed,
        None => match stops.first() {
            Some(stop) => stop.id.split(':').next().unwrap_or_default().to_string(),
            None => bail!("{path}: set 'feed' (e.g. Lappeenranta) or at least one stop"),
        },
    };

    let timezone = raw.timezone.unwrap_or_else(|| "Europe/Helsinki".to_string());
    Ok(Config {
        feed,
        stops,
        routes: lowercase_routes(raw.routes.unwrap_or_default()),
        departures: raw.departures.unwrap_or(12),
        timezone: timezone.parse().map_err(|_| anyhow!("{path}: unknown timezone {timezone}"))?,
        map: raw.map.unwrap_or_default(),
    })
}

// ---------------------------------------------------------------------------
// Shared state and API errors
// ---------------------------------------------------------------------------

struct App {
    config: Config,
    api_key: String,
    graphql_url: String,
    http: reqwest::Client,
    stop_cache: Mutex<HashMap<String, (f64, StopData)>>,
    stop_locks: Mutex<HashMap<String, Arc<AsyncMutex<()>>>>,
    vehicles: Mutex<HashMap<String, Vehicle>>,
    mqtt_connected: AtomicBool,
}

type Shared = Arc<App>;

struct ApiError(StatusCode, String);

impl ApiError {
    fn new(status: StatusCode, detail: impl Into<String>) -> Self {
        Self(status, detail.into())
    }

    fn upstream() -> Self {
        Self::new(StatusCode::BAD_GATEWAY, "Digitransit request failed")
    }

    fn invalid(detail: impl Into<String>) -> Self {
        Self::new(StatusCode::UNPROCESSABLE_ENTITY, detail)
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({ "detail": self.1 }))).into_response()
    }
}

type Params = HashMap<String, String>;

fn parse_param<T: FromStr>(params: &Params, name: &str) -> Result<Option<T>, ApiError> {
    params
        .get(name)
        .map(|v| v.parse().map_err(|_| ApiError::invalid(format!("Invalid value for {name}"))))
        .transpose()
}

fn coordinate(params: &Params, name: &str, limit: f64) -> Result<f64, ApiError> {
    parse_param(params, name)?
        .filter(|v: &f64| (-limit..=limit).contains(v))
        .ok_or_else(|| ApiError::invalid(format!("{name} must be between -{limit} and {limit}")))
}

// ---------------------------------------------------------------------------
// Digitransit GraphQL
// ---------------------------------------------------------------------------

const STOP_QUERY: &str = r#"
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
"#;

const SEARCH_QUERY: &str = r#"
query Search($q: String!) {
  stops(name: $q) { gtfsId name code lat lon routes { shortName } }
}
"#;

const NEARBY_QUERY: &str = r#"
query Nearby($lat: Float!, $lon: Float!) {
  stopsByRadius(lat: $lat, lon: $lon, radius: 1000, first: 40) {
    edges { node { distance stop { gtfsId name code lat lon routes { shortName } } } }
  }
}
"#;

#[derive(Deserialize)]
struct GqlResponse<T> {
    data: Option<T>,
    errors: Option<Vec<GqlError>>,
}

#[derive(Deserialize)]
struct GqlError {
    message: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct GqlStop {
    gtfs_id: String,
    name: String,
    code: Option<String>,
    lat: Option<f64>,
    lon: Option<f64>,
    routes: Option<Vec<GqlRoute>>,
    stoptimes_without_patterns: Option<Vec<GqlStopTime>>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct GqlRoute {
    short_name: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct GqlStopTime {
    service_day: i64,
    scheduled_departure: i64,
    realtime_departure: Option<i64>,
    realtime: Option<bool>,
    realtime_state: Option<String>,
    headsign: Option<String>,
    trip: Option<GqlTrip>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct GqlTrip {
    route_short_name: Option<String>,
    trip_headsign: Option<String>,
}

#[derive(Deserialize)]
struct StopResult {
    stop: Option<GqlStop>,
}

#[derive(Deserialize)]
struct SearchResult {
    stops: Option<Vec<GqlStop>>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct NearbyResult {
    stops_by_radius: Option<NearbyConnection>,
}

#[derive(Deserialize)]
struct NearbyConnection {
    edges: Option<Vec<NearbyEdge>>,
}

#[derive(Deserialize)]
struct NearbyEdge {
    node: NearbyNode,
}

#[derive(Deserialize)]
struct NearbyNode {
    distance: Value,
    stop: Option<GqlStop>,
}

#[derive(Clone, Serialize)]
struct StopSummary {
    id: String,
    name: String,
    code: Option<String>,
    lat: Option<f64>,
    lon: Option<f64>,
    routes: Vec<String>,
}

#[derive(Clone, Serialize)]
struct Departure {
    route: String,
    headsign: String,
    time: i64,
    scheduled: i64,
    realtime: bool,
    cancelled: bool,
}

#[derive(Clone, Serialize)]
struct StopData {
    #[serde(flatten)]
    summary: StopSummary,
    departures: Vec<Departure>,
    updated: i64,
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    stale: bool,
}

/// Sort route names naturally: 2, 7, 14, 14A, N1.
fn route_sort_key(name: &str) -> (u64, &str) {
    let digits = name.len() - name.trim_start_matches(|c: char| c.is_ascii_digit()).len();
    (name[..digits].parse().unwrap_or(1_000_000_000), name)
}

fn route_names(routes: Option<Vec<GqlRoute>>) -> Vec<String> {
    let unique: BTreeSet<String> = routes
        .unwrap_or_default()
        .into_iter()
        .filter_map(|r| r.short_name.filter(|n| !n.is_empty()))
        .collect();
    let mut names: Vec<String> = unique.into_iter().collect();
    names.sort_by(|a, b| route_sort_key(a).cmp(&route_sort_key(b)));
    names
}

fn stop_summary(stop: GqlStop) -> StopSummary {
    StopSummary {
        id: stop.gtfs_id,
        name: stop.name,
        code: stop.code,
        lat: stop.lat,
        lon: stop.lon,
        routes: route_names(stop.routes),
    }
}

impl App {
    async fn gql<T: DeserializeOwned>(&self, query: &str, variables: Value) -> anyhow::Result<T> {
        let body: GqlResponse<T> = self
            .http
            .post(&self.graphql_url)
            .json(&json!({ "query": query, "variables": variables }))
            .header("digitransit-subscription-key", &self.api_key)
            .timeout(Duration::from_secs(10))
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        if let Some(error) = body.errors.as_deref().and_then(<[GqlError]>::first) {
            bail!("{}", error.message.as_deref().unwrap_or("GraphQL error"));
        }
        body.data.context("GraphQL response has no data")
    }

    fn valid_stop_id(&self, stop_id: &str) -> bool {
        stop_id
            .strip_prefix(self.config.feed.as_str())
            .and_then(|rest| rest.strip_prefix(':'))
            .is_some_and(|local| {
                !local.is_empty()
                    && local.chars().all(|c| c.is_ascii_alphanumeric() || "_.-".contains(c))
            })
    }

    /// Routes to show for a stop (lowercased); empty means all.
    ///
    /// A configured stop's own `routes` list wins over the top-level one.
    fn route_filter(&self, stop_id: &str) -> &HashSet<String> {
        self.config
            .stops
            .iter()
            .filter(|s| s.id == stop_id)
            .find_map(|s| s.routes.as_ref())
            .unwrap_or(&self.config.routes)
    }

    async fn fetch_stop(&self, stop_id: &str) -> anyhow::Result<Option<StopData>> {
        let allowed = self.route_filter(stop_id);
        // Filtering throws departures away, so ask for more of them.
        let n = self.config.departures * if allowed.is_empty() { 2 } else { 5 };
        let data: StopResult = self.gql(STOP_QUERY, json!({ "id": stop_id, "n": n })).await?;
        let Some(mut stop) = data.stop else {
            return Ok(None);
        };

        let now = now();
        let mut departures = Vec::new();
        for st in stop.stoptimes_without_patterns.take().unwrap_or_default() {
            let realtime = st.realtime.unwrap_or(false);
            let offset = st.realtime_departure.filter(|_| realtime).unwrap_or(st.scheduled_departure);
            let time = st.service_day + offset;
            if (time as f64) < now - 30.0 {
                continue;
            }
            let (trip_route, trip_headsign) = match st.trip {
                Some(trip) => (trip.route_short_name, trip.trip_headsign),
                None => (None, None),
            };
            let route = trip_route.filter(|r| !r.is_empty()).unwrap_or_else(|| "?".to_string());
            if !allowed.is_empty() && !allowed.contains(&route.to_lowercase()) {
                continue;
            }
            departures.push(Departure {
                route,
                headsign: st.headsign.filter(|h| !h.is_empty()).or(trip_headsign).unwrap_or_default(),
                time,
                scheduled: st.service_day + st.scheduled_departure,
                realtime,
                cancelled: st.realtime_state.as_deref() == Some("CANCELED"),
            });
        }
        departures.sort_by_key(|d| d.time);
        departures.truncate(self.config.departures);

        let mut summary = stop_summary(stop);
        if !allowed.is_empty() {
            // Also limits which buses the map shows for this stop.
            summary.routes.retain(|r| allowed.contains(&r.to_lowercase()));
        }
        Ok(Some(StopData { summary, departures, updated: now as i64, stale: false }))
    }

    fn fresh_stop(&self, stop_id: &str) -> Option<StopData> {
        let cache = self.stop_cache.lock().unwrap();
        cache
            .get(stop_id)
            .filter(|(fetched, _)| now() - fetched < STOP_TTL)
            .map(|(_, data)| data.clone())
    }

    /// Return a stop with departures, at most STOP_TTL seconds old.
    ///
    /// Concurrent requests for the same stop share one upstream fetch. If
    /// Digitransit fails, the last good result is returned marked as stale.
    async fn get_stop(&self, stop_id: &str) -> Result<StopData, ApiError> {
        if !self.valid_stop_id(stop_id) {
            return Err(ApiError::new(
                StatusCode::NOT_FOUND,
                format!("Unknown stop id (stops must belong to the {} feed)", self.config.feed),
            ));
        }
        if let Some(data) = self.fresh_stop(stop_id) {
            return Ok(data);
        }

        let lock = self.stop_locks.lock().unwrap().entry(stop_id.to_string()).or_default().clone();
        let _guard = lock.lock().await;
        if let Some(data) = self.fresh_stop(stop_id) {
            return Ok(data);
        }

        let data = match self.fetch_stop(stop_id).await {
            Ok(Some(data)) => data,
            Ok(None) => return Err(ApiError::new(StatusCode::NOT_FOUND, "Stop not found")),
            Err(e) => {
                warn!("Stop fetch failed for {stop_id}: {e:#}");
                let cache = self.stop_cache.lock().unwrap();
                return match cache.get(stop_id) {
                    Some((_, data)) => Ok(StopData { stale: true, ..data.clone() }),
                    None => Err(ApiError::upstream()),
                };
            }
        };

        let mut cache = self.stop_cache.lock().unwrap();
        cache.insert(stop_id.to_string(), (now(), data.clone()));
        // Forget stops nobody has looked at in a while.
        let cutoff = now() - 600.0;
        cache.retain(|_, (fetched, _)| *fetched >= cutoff);
        self.stop_locks
            .lock()
            .unwrap()
            .retain(|id, lock| cache.contains_key(id) || Arc::strong_count(lock) > 1);
        Ok(data)
    }
}

// ---------------------------------------------------------------------------
// MQTT vehicle positions
// ---------------------------------------------------------------------------

// The parts of the GTFS-RT FeedMessage this app reads.
#[derive(Clone, PartialEq, Message)]
struct FeedMessage {
    #[prost(message, repeated, tag = "2")]
    entity: Vec<FeedEntity>,
}

#[derive(Clone, PartialEq, Message)]
struct FeedEntity {
    #[prost(string, required, tag = "1")]
    id: String,
    #[prost(message, optional, tag = "4")]
    vehicle: Option<VehiclePosition>,
}

#[derive(Clone, PartialEq, Message)]
struct VehiclePosition {
    #[prost(message, optional, tag = "2")]
    position: Option<Position>,
}

#[derive(Clone, PartialEq, Message)]
struct Position {
    #[prost(float, required, tag = "1")]
    latitude: f32,
    #[prost(float, required, tag = "2")]
    longitude: f32,
    #[prost(float, optional, tag = "3")]
    bearing: Option<f32>,
}

#[derive(Clone, Serialize)]
struct Vehicle {
    id: String,
    route: String,
    headsign: String,
    lat: f64,
    lon: f64,
    bearing: Option<f64>,
    #[serde(skip)]
    seen: f64,
}

fn round6(value: f32) -> f64 {
    (f64::from(value) * 1e6).round() / 1e6
}

impl App {
    fn on_mqtt_message(&self, topic: &str, payload: &[u8]) {
        // Topic: /gtfsrt/vp/<feed>/<agency_id>/<agency>/<mode>/<route_id>/<direction>/
        //        <headsign>/<trip_id>/<next_stop>/<start_time>/<vehicle_id>/
        //        <geohash, 4 segments>/<short_name>/<color>/
        // The route short name is not in the protobuf, so read it from the end of the topic.
        let parts: Vec<&str> = topic.split('/').collect();
        if parts.len() < 12 {
            return;
        }
        let tail = parts.strip_suffix(&[""]).unwrap_or(&parts);
        let route = tail[tail.len() - 2];
        let headsign = parts[9];

        let feed = match FeedMessage::decode(payload) {
            Ok(feed) => feed,
            Err(e) => {
                debug!("Bad MQTT payload on {topic}: {e}");
                return;
            }
        };

        let now = now();
        for entity in feed.entity {
            let Some(position) = entity.vehicle.and_then(|v| v.position) else {
                continue;
            };
            let record = Vehicle {
                id: entity.id.clone(),
                route: route.to_string(),
                headsign: headsign.to_string(),
                lat: round6(position.latitude),
                lon: round6(position.longitude),
                bearing: position.bearing.map(f64::from),
                seen: now,
            };
            self.vehicles.lock().unwrap().insert(entity.id, record);
        }
    }

    fn current_vehicles(&self, routes: &HashSet<&str>) -> Vec<Vehicle> {
        let cutoff = now() - VEHICLE_STALE;
        let mut vehicles = self.vehicles.lock().unwrap();
        vehicles.retain(|_, v| v.seen >= cutoff);
        vehicles
            .values()
            .filter(|v| routes.is_empty() || routes.contains(v.route.as_str()))
            .cloned()
            .collect()
    }
}

async fn run_mqtt(app: Shared, host: String, port: u16) {
    const MIN_DELAY: Duration = Duration::from_secs(1);
    const MAX_DELAY: Duration = Duration::from_secs(60);

    let client_id = format!("bussit-{:08x}", RandomState::new().hash_one(()) as u32);
    let mut options = MqttOptions::new(client_id, host, port);
    options.set_keep_alive(Duration::from_secs(60));
    options.set_max_packet_size(1 << 20, 1 << 16);
    let (client, mut events) = AsyncClient::new(options, 16);

    let topic = format!("/gtfsrt/vp/{}/#", app.config.feed);
    let mut delay = MIN_DELAY;
    // Polling reconnects after an error, so each new connection subscribes again.
    loop {
        match events.poll().await {
            Ok(Event::Incoming(Packet::ConnAck(_))) => {
                delay = MIN_DELAY;
                app.mqtt_connected.store(true, Ordering::Relaxed);
                match client.subscribe(topic.as_str(), QoS::AtMostOnce).await {
                    Ok(()) => info!("MQTT subscribed: {topic}"),
                    Err(e) => warn!("MQTT subscribe failed: {e}"),
                }
            }
            Ok(Event::Incoming(Packet::Publish(msg))) => app.on_mqtt_message(&msg.topic, &msg.payload),
            Ok(_) => {}
            Err(e) => {
                app.mqtt_connected.store(false, Ordering::Relaxed);
                warn!("MQTT disconnected: {e}");
                tokio::time::sleep(delay).await;
                delay = (delay * 2).min(MAX_DELAY);
            }
        }
    }
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

async fn healthz(State(app): State<Shared>) -> Json<Value> {
    let vehicles = app.vehicles.lock().unwrap().len();
    Json(json!({
        "ok": true,
        "mqtt": app.mqtt_connected.load(Ordering::Relaxed),
        "vehicles": vehicles,
    }))
}

/// Default quick-pick stops (named) and the initial map view.
async fn api_config(State(app): State<Shared>) -> Json<Value> {
    let config = &app.config;
    let results = join_all(config.stops.iter().map(|s| app.get_stop(&s.id))).await;
    let stops: Vec<Value> = config
        .stops
        .iter()
        .zip(results)
        .map(|(s, res)| {
            let summary = res.ok().map(|data| data.summary);
            let name = s
                .label
                .clone()
                .filter(|l| !l.is_empty())
                .or_else(|| summary.as_ref().map(|d| d.name.clone()).filter(|n| !n.is_empty()))
                .unwrap_or_else(|| s.id.clone());
            json!({ "id": s.id, "name": name, "code": summary.and_then(|d| d.code) })
        })
        .collect();
    let m = &config.map;
    Json(json!({
        "feed": config.feed,
        "stops": stops,
        "map": {
            "lat": m.lat.clone().unwrap_or(json!(61.0587)),
            "lon": m.lon.clone().unwrap_or(json!(28.1887)),
            "zoom": m.zoom.clone().unwrap_or(json!(14)),
            "stadia_api_key": m.stadia_api_key,
        },
    }))
}

async fn api_search(
    State(app): State<Shared>,
    Query(params): Query<Params>,
) -> Result<Json<Value>, ApiError> {
    let q = params
        .get("q")
        .filter(|q| (2..=60).contains(&q.chars().count()))
        .ok_or_else(|| ApiError::invalid("q must be 2 to 60 characters"))?;
    let data: SearchResult = app.gql(SEARCH_QUERY, json!({ "q": q })).await.map_err(|e| {
        warn!("Stop search failed: {e:#}");
        ApiError::upstream()
    })?;
    let stops: Vec<StopSummary> = data
        .stops
        .unwrap_or_default()
        .into_iter()
        .filter(|s| app.valid_stop_id(&s.gtfs_id))
        .map(stop_summary)
        .take(25)
        .collect();
    Ok(Json(json!({ "stops": stops })))
}

#[derive(Serialize)]
struct NearbyStop {
    #[serde(flatten)]
    summary: StopSummary,
    distance: Value,
}

async fn api_nearby(
    State(app): State<Shared>,
    Query(params): Query<Params>,
) -> Result<Json<Value>, ApiError> {
    let lat = coordinate(&params, "lat", 90.0)?;
    let lon = coordinate(&params, "lon", 180.0)?;
    let data: NearbyResult = app.gql(NEARBY_QUERY, json!({ "lat": lat, "lon": lon })).await.map_err(|e| {
        warn!("Nearby search failed: {e:#}");
        ApiError::upstream()
    })?;
    let edges = data.stops_by_radius.and_then(|c| c.edges).unwrap_or_default();
    let stops: Vec<NearbyStop> = edges
        .into_iter()
        .filter_map(|edge| {
            let stop = edge.node.stop.filter(|s| app.valid_stop_id(&s.gtfs_id))?;
            Some(NearbyStop { summary: stop_summary(stop), distance: edge.node.distance })
        })
        .take(25)
        .collect();
    Ok(Json(json!({ "stops": stops })))
}

#[derive(Serialize)]
struct StopResponse {
    #[serde(flatten)]
    data: StopData,
    now: f64,
}

async fn api_stop(
    State(app): State<Shared>,
    Path(stop_id): Path<String>,
) -> Result<Json<StopResponse>, ApiError> {
    let data = app.get_stop(&stop_id).await?;
    Ok(Json(StopResponse { data, now: now() }))
}

async fn api_vehicles(State(app): State<Shared>, Query(params): Query<Params>) -> Json<Value> {
    let routes = params.get("routes").map_or("", String::as_str);
    let wanted: HashSet<&str> = routes.split(',').filter(|r| !r.is_empty()).collect();
    Json(json!({ "vehicles": app.current_vehicles(&wanted), "now": now() }))
}

fn format_when(time: i64, now: f64, tz: Tz) -> String {
    let minutes = ((time as f64 - now) / 60.0).floor() as i64;
    if minutes <= 0 {
        return "Nyt".to_string();
    }
    if minutes <= 15 {
        return format!("{minutes} min");
    }
    DateTime::from_timestamp(time, 0)
        .map(|t| t.with_timezone(&tz).format("%H:%M").to_string())
        .unwrap_or_default()
}

/// Pre-formatted departures for a gethomepage.dev customapi widget.
async fn api_homepage(
    State(app): State<Shared>,
    Query(params): Query<Params>,
) -> Result<Json<Value>, ApiError> {
    let count: i64 = parse_param(&params, "count")?.unwrap_or(5);
    if !(1..=20).contains(&count) {
        return Err(ApiError::invalid("count must be between 1 and 20"));
    }
    let stop_id = params
        .get("stop")
        .filter(|s| !s.is_empty())
        .or_else(|| app.config.stops.first().map(|s| &s.id))
        .ok_or_else(|| ApiError::new(StatusCode::BAD_REQUEST, "No stop given and none configured"))?;

    let data = app.get_stop(stop_id).await?;
    let now = now();
    let departures: Vec<Value> = data
        .departures
        .iter()
        .filter(|d| d.time as f64 >= now - 30.0)
        .take(count as usize)
        .map(|d| {
            let when = if d.cancelled {
                "Peruttu".to_string()
            } else {
                format_when(d.time, now, app.config.timezone)
            };
            json!({
                "title": format!("{} {}", d.route, d.headsign).trim(),
                "when": when,
                "route": d.route,
                "headsign": d.headsign,
                "time": d.time,
            })
        })
        .collect();
    Ok(Json(json!({
        "stop": data.summary.name,
        "code": data.summary.code,
        "departures": departures,
    })))
}

async fn shutdown_signal() {
    let mut terminate = signal(SignalKind::terminate()).expect("cannot listen for SIGTERM");
    tokio::select! {
        _ = terminate.recv() => {}
        _ = tokio::signal::ctrl_c() => {}
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt().with_max_level(tracing::Level::INFO).init();

    let config = load_config(&env_or("CONFIG_PATH", "config.yaml"))?;
    let api_key = env_or("DIGITRANSIT_API_KEY", "");
    if api_key.is_empty() {
        warn!("DIGITRANSIT_API_KEY is not set: departures and stop search will fail");
    }

    let app: Shared = Arc::new(App {
        config,
        api_key,
        graphql_url: env_or("DIGITRANSIT_URL", "https://api.digitransit.fi/routing/v2/waltti/gtfs/v1"),
        http: reqwest::Client::new(),
        stop_cache: Mutex::default(),
        stop_locks: Mutex::default(),
        vehicles: Mutex::default(),
        mqtt_connected: AtomicBool::new(false),
    });

    let mqtt_port = env_or("MQTT_PORT", "1883").parse().context("MQTT_PORT is not a port number")?;
    tokio::spawn(run_mqtt(app.clone(), env_or("MQTT_HOST", "mqtt.digitransit.fi"), mqtt_port));

    let static_dir = env_or("STATIC_DIR", "static");
    let static_dir = FilePath::new(&static_dir);
    let index = get_service(ServeFile::new(static_dir.join("index.html"))).layer(
        SetResponseHeaderLayer::overriding(header::CACHE_CONTROL, HeaderValue::from_static("no-cache")),
    );
    let manifest = get_service(ServeFile::new_with_mime(
        static_dir.join("manifest.webmanifest"),
        &"application/manifest+json".parse().expect("valid media type"),
    ));

    let router = Router::new()
        .route("/", index)
        .route("/manifest.webmanifest", manifest)
        .nest_service("/static", ServeDir::new(static_dir))
        .route("/healthz", get(healthz))
        .route("/api/config", get(api_config))
        .route("/api/stops/search", get(api_search))
        .route("/api/stops/nearby", get(api_nearby))
        .route("/api/stops/{stop_id}", get(api_stop))
        .route("/api/vehicles", get(api_vehicles))
        .route("/api/homepage", get(api_homepage))
        .with_state(app);

    let address = format!("0.0.0.0:{}", env_or("PORT", "8000"));
    let listener = tokio::net::TcpListener::bind(&address).await.with_context(|| format!("cannot bind {address}"))?;
    info!("Listening on {address}");
    axum::serve(listener, router).with_graceful_shutdown(shutdown_signal()).await?;
    Ok(())
}
