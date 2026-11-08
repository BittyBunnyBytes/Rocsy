//! HTTP surface.
//!
//! Internal (`/v2/...`): loopback + X-Queue-Internal-Secret, used only by the
//! apps' backends through `shared/bunnylock.py`. Never routed by the gateway.
//!
//! Browser (`{QUEUE_MOUNT_PREFIX}/...`): reached only through bunny-gateway,
//! which checks the session cookie before proxying here.

use std::{convert::Infallible, net::SocketAddr, sync::Arc, time::Duration};

use axum::{
    extract::{ConnectInfo, Path, Query, State},
    http::{header, HeaderMap, HeaderName, HeaderValue, StatusCode},
    response::{
        sse::{Event, KeepAlive, Sse},
        Html, IntoResponse, Response,
    },
    routing::{get, post},
    Json, Router,
};
use futures::{stream, Stream, StreamExt};
use serde::Deserialize;
use serde_json::{json, Value};
use subtle::ConstantTimeEq;
use tokio::sync::oneshot;
use tokio_stream::wrappers::{BroadcastStream, UnboundedReceiverStream};
use tower_http::{limit::RequestBodyLimitLayer, services::ServeDir};

use crate::{
    core::{DequeueOutcome, Entry, NewEntry, State as EntryState},
    metrics, progress,
    state::{lock, short, AppEvent, Reply, Settings, Shared},
};

type S = State<Arc<Shared>>;

const CANCEL_TIMEOUT: Duration = Duration::from_secs(15);
const SUPERVISOR_TIMEOUT: Duration = Duration::from_secs(60);
const MAX_LABEL: usize = 200;

pub struct ApiError(StatusCode, &'static str, String);

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({ "error": self.1, "message": self.2 }))).into_response()
    }
}

fn not_found(message: &str) -> ApiError {
    ApiError(StatusCode::NOT_FOUND, "not_found", message.to_owned())
}

fn invalid(message: impl Into<String>) -> ApiError {
    ApiError(StatusCode::UNPROCESSABLE_ENTITY, "invalid", message.into())
}

fn internal(shared: &Shared, addr: SocketAddr, headers: &HeaderMap) -> Result<(), ApiError> {
    if !addr.ip().is_loopback() {
        return Err(ApiError(
            StatusCode::FORBIDDEN,
            "forbidden",
            "This endpoint is only reachable from localhost.".into(),
        ));
    }
    let given = headers
        .get("x-queue-internal-secret")
        .map(HeaderValue::as_bytes)
        .unwrap_or_default();
    if !bool::from(given.ct_eq(shared.cfg.secret.as_bytes())) {
        return Err(ApiError(
            StatusCode::FORBIDDEN,
            "forbidden",
            "Missing or invalid X-Queue-Internal-Secret.".into(),
        ));
    }
    Ok(())
}

pub fn router(shared: Arc<Shared>) -> Router {
    let prefix = shared.cfg.prefix.clone();
    let internal = Router::new()
        .route("/v2/stream", get(stream_events))
        .route("/v2/enqueue", post(enqueue))
        .route("/v2/ack", post(ack))
        .route("/v2/heartbeat", post(heartbeat))
        .route("/v2/release", post(release))
        .route("/v2/dequeue", post(dequeue))
        .route("/v2/prioritize", post(prioritize))
        .route("/v2/progress", post(push_progress))
        .route("/v2/shutdown_at", post(shutdown_at))
        .route("/v2/reply", post(reply))
        .route("/v2/adopt", post(adopt))
        .route("/health", get(health))
        .route(
            "/metrics",
            get(|| async {
                (
                    [(header::CONTENT_TYPE, "text/plain; version=0.0.4")],
                    metrics::render(),
                )
            }),
        );
    let browser = Router::new()
        .route("/", get(index))
        .route("/api/queue", get(list_queue))
        .route("/api/queue/events", get(browser_events))
        .route("/api/queue/reorder", post(reorder))
        .route("/api/queue/{entry_id}/lock", post(set_locked))
        .route("/api/queue/{entry_id}/cancel", post(cancel))
        .route("/api/settings", get(get_settings).put(put_settings))
        .route("/api/vram", get(get_vram))
        .route("/api/vram/forget", post(forget_vram))
        .route("/api/comfyui/restart", post(restart_comfyui))
        .nest_service("/static", ServeDir::new(&shared.cfg.static_dir));
    // axum's nest() doesn't match "{prefix}/": the gateway proxies the page there.
    let internal = if prefix.is_empty() {
        internal
    } else {
        internal.route(&format!("{prefix}/"), get(index))
    };
    let app = if prefix.is_empty() {
        internal.merge(browser)
    } else {
        internal.nest(&prefix, browser)
    };
    app.layer(RequestBodyLimitLayer::new(256 * 1024))
        .with_state(shared)
}

// ------------------------------------------------------------------ internal

#[derive(Deserialize)]
struct StreamQuery {
    app: String,
    instance: String,
}

/// Drops with the response stream: the app process went away.
struct ConnGuard {
    shared: Arc<Shared>,
    instance: String,
    conn: u64,
}

impl Drop for ConnGuard {
    fn drop(&mut self) {
        self.shared.disconnect(&self.instance, self.conn);
    }
}

fn app_event(event: AppEvent) -> Event {
    let (name, data) = match &event {
        AppEvent::Hello { instance } => ("hello", json!({ "instance": instance })),
        AppEvent::Grant {
            entry_id,
            job_id,
            token,
            others,
        } => (
            "grant",
            json!({ "entry_id": entry_id, "job_id": job_id, "token": token, "others": others }),
        ),
        AppEvent::Prepare { entry_id, job_id } => {
            ("prepare", json!({ "entry_id": entry_id, "job_id": job_id }))
        }
        AppEvent::Adopt {
            entry_id,
            job_id,
            active,
            token,
        } => (
            "adopt",
            json!({ "entry_id": entry_id, "job_id": job_id, "active": active, "token": token }),
        ),
        AppEvent::Cancel {
            request_id,
            entry_id,
            job_id,
        } => (
            "cancel",
            json!({ "request_id": request_id, "entry_id": entry_id, "job_id": job_id }),
        ),
    };
    Event::default().event(name).data(data.to_string())
}

/// One per app process: grants, prepare notices and cancels arrive here, and
/// the stream closing tells the queue the process is gone.
async fn stream_events(
    State(shared): S,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Query(q): Query<StreamQuery>,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, ApiError> {
    internal(&shared, addr, &headers)?;
    if q.app.is_empty() || q.instance.is_empty() || q.app.len() > 32 || q.instance.len() > 64 {
        return Err(invalid("app and instance are required"));
    }
    let (conn, rx) = shared.connect(&q.app, &q.instance);
    let guard = ConnGuard {
        shared: shared.clone(),
        instance: q.instance,
        conn,
    };
    let events = UnboundedReceiverStream::new(rx).map(move |event| {
        let _keep = &guard;
        Ok(app_event(event))
    });
    // A comment every second: a closed connection is noticed within a second.
    Ok(Sse::new(events).keep_alive(KeepAlive::new().interval(Duration::from_secs(1)).text("")))
}

#[derive(Deserialize)]
struct EnqueueBody {
    app: String,
    job_id: String,
    label: String,
    instance: String,
    #[serde(default)]
    is_continuous: bool,
    #[serde(default)]
    priority: bool,
    #[serde(default)]
    group: Option<String>,
    #[serde(default)]
    uses_llm: Option<bool>,
    #[serde(default)]
    model: Option<String>,
    #[serde(default)]
    config: Option<String>,
    #[serde(default)]
    vram_mb: Option<u64>,
}

async fn enqueue(
    State(shared): S,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(body): Json<EnqueueBody>,
) -> Result<Json<Value>, ApiError> {
    internal(&shared, addr, &headers)?;
    if body.app.is_empty()
        || body.app.len() > 32
        || body.job_id.is_empty()
        || body.job_id.len() > 200
        || body.instance.is_empty()
    {
        return Err(invalid("app, job_id and instance are required"));
    }
    if body.group.as_ref().is_some_and(|g| g.chars().count() > 60) {
        return Err(invalid("group must be at most 60 characters"));
    }
    if body.model.as_ref().is_some_and(|m| m.chars().count() > 120)
        || body
            .config
            .as_ref()
            .is_some_and(|c| c.chars().count() > 120)
    {
        return Err(invalid("model and config must be at most 120 characters"));
    }
    shared.register_instance(&body.app, &body.instance);
    let new = NewEntry {
        app: body.app,
        job_id: body.job_id,
        label: body.label.chars().take(MAX_LABEL).collect(),
        is_continuous: body.is_continuous,
        priority: body.priority,
        group: body.group,
        instance: Some(body.instance),
        uses_llm: body.uses_llm,
        model: body.model,
        config: body.config,
        vram_mb: body.vram_mb,
    };
    let done = shared.with_queue(|q, now| q.enqueue(new, now));
    let e = &done.entry;
    shared.note_enqueued(&e.entry_id);
    tracing::info!(
        "{} ({}/{}) {}{}",
        short(&e.entry_id),
        e.app,
        short(&e.job_id),
        if e.state == EntryState::Active {
            "active at once"
        } else {
            "waiting"
        },
        if body.priority { " (priority)" } else { "" }
    );
    let response =
        json!({ "entry_id": e.entry_id, "state": e.state.as_str(), "position": done.position });
    shared.after_change(done.promoted);
    Ok(Json(response))
}

#[derive(Deserialize)]
struct TokenBody {
    token: String,
    #[serde(default)]
    instance: Option<String>,
    /// release: the holder was an LLM turn that just unloaded its model.
    #[serde(default)]
    llm_settle: bool,
}

async fn ack(
    State(shared): S,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(body): Json<TokenBody>,
) -> Result<Json<Value>, ApiError> {
    internal(&shared, addr, &headers)?;
    let entry = shared
        .with_queue(|q, _| q.ack(&body.token))
        .ok_or_else(|| not_found("No active queue entry matches that token."))?;
    shared.note_acked(&entry.entry_id);
    Ok(Json(json!({ "acked": true })))
}

async fn heartbeat(
    State(shared): S,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(body): Json<TokenBody>,
) -> Result<Json<Value>, ApiError> {
    internal(&shared, addr, &headers)?;
    let entry = shared
        .with_queue(|q, now| q.heartbeat(&body.token, body.instance.as_deref(), now))
        .ok_or_else(|| not_found("No active queue entry matches that token."))?;
    Ok(Json(
        json!({ "token": entry.token, "expires_at": entry.expires_at.map(crate::store::iso) }),
    ))
}

async fn release(
    State(shared): S,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(body): Json<TokenBody>,
) -> Result<Json<Value>, ApiError> {
    internal(&shared, addr, &headers)?;
    let holder = shared.read_queue(|q| q.holder(&body.token).cloned());
    let (released, promoted) = shared.with_queue(|q, now| q.release(&body.token, now));
    if !released {
        return Err(not_found(
            "That token does not match the currently active queue entry.",
        ));
    }
    if let Some(holder) = &holder {
        tracing::info!(
            "{} ({}/{}) released the GPU",
            short(&holder.entry_id),
            holder.app,
            short(&holder.job_id)
        );
        shared.note_freed(Some(holder));
    }
    if body.llm_settle {
        shared.start_llm_guard();
    }
    shared.after_change(promoted);
    Ok(Json(json!({ "released": true })))
}

#[derive(Deserialize)]
struct EntryBody {
    entry_id: String,
}

async fn dequeue(
    State(shared): S,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(body): Json<EntryBody>,
) -> Result<Json<Value>, ApiError> {
    internal(&shared, addr, &headers)?;
    let (outcome, promoted) = shared.with_queue(|q, now| {
        let outcome = q.dequeue(&body.entry_id);
        // The removed entry may have been what kept the next one waiting.
        (outcome, q.fill(now))
    });
    let (dequeued, reason) = match outcome {
        DequeueOutcome::Removed => (true, None),
        DequeueOutcome::NotFound => (true, Some("not_found")),
        DequeueOutcome::Active => (false, Some("active")),
    };
    if outcome == DequeueOutcome::Removed || !promoted.is_empty() {
        shared.after_change(promoted);
    }
    Ok(Json(json!({ "dequeued": dequeued, "reason": reason })))
}

async fn prioritize(
    State(shared): S,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(body): Json<EntryBody>,
) -> Result<Json<Value>, ApiError> {
    internal(&shared, addr, &headers)?;
    let (moved, promoted) = shared.with_queue(|q, now| {
        let moved = q.prioritize(&body.entry_id);
        (moved, if moved { q.fill(now) } else { Vec::new() })
    });
    if moved {
        shared.after_change(promoted);
    }
    Ok(Json(json!({ "prioritized": moved })))
}

#[derive(Deserialize)]
struct ProgressBody {
    token: String,
    /// The app's own job JSON (normalised like the polling fallback) ...
    #[serde(default)]
    job: Option<Value>,
    /// ... or the numbers directly.
    #[serde(default)]
    percent: Option<f64>,
    #[serde(default)]
    stage: Option<String>,
    #[serde(default)]
    remaining_seconds: Option<f64>,
}

async fn push_progress(
    State(shared): S,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(body): Json<ProgressBody>,
) -> Result<Json<Value>, ApiError> {
    internal(&shared, addr, &headers)?;
    let active = shared
        .read_queue(|q| q.holder(&body.token).cloned())
        .ok_or_else(|| not_found("No active queue entry matches that token."))?;
    let job = body
        .job
        .unwrap_or_else(|| json!({ "progress": body.percent, "stage": body.stage }));
    let mut value = progress::normalise(&active.app, &job, active.became_active_at);
    if let (Some(v), Some(r)) = (value.as_mut(), body.remaining_seconds) {
        v.remaining_seconds = Some(r.round() as i64);
    }
    progress::update(&shared, &active.entry_id, value, true);
    Ok(Json(json!({ "ok": true })))
}

#[derive(Deserialize)]
struct ShutdownBody {
    entry_id: String,
    shutdown_at: Option<String>,
}

async fn shutdown_at(
    State(shared): S,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(body): Json<ShutdownBody>,
) -> Result<Json<Value>, ApiError> {
    internal(&shared, addr, &headers)?;
    if !shared.with_queue(|q, _| q.set_shutdown_at(&body.entry_id, body.shutdown_at)) {
        return Err(not_found("No queue entry with that id."));
    }
    shared.publish_queue();
    Ok(Json(json!({ "updated": true })))
}

#[derive(Deserialize)]
struct ReplyBody {
    request_id: String,
    #[serde(flatten)]
    reply: Reply,
}

async fn reply(
    State(shared): S,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(body): Json<ReplyBody>,
) -> Result<Json<Value>, ApiError> {
    internal(&shared, addr, &headers)?;
    if let Some(tx) = lock(&shared.replies).remove(&body.request_id) {
        let _ = tx.send(body.reply);
    }
    Ok(Json(json!({ "ok": true })))
}

#[derive(Deserialize)]
struct AdoptBody {
    entry_id: String,
    keep: bool,
}

/// A restarted app's answer to an `adopt` offer: keep the entry (it saved
/// the job) or drop it.
async fn adopt(
    State(shared): S,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(body): Json<AdoptBody>,
) -> Result<Json<Value>, ApiError> {
    internal(&shared, addr, &headers)?;
    shared.adopted(&body.entry_id, body.keep);
    Ok(Json(json!({ "ok": true })))
}

async fn health(State(shared): S) -> Json<Value> {
    Json(
        json!({ "status": "ok", "version": env!("CARGO_PKG_VERSION"), "apps": shared.connected_apps() }),
    )
}

// ------------------------------------------------------------------- browser

async fn index(State(shared): S) -> Response {
    match tokio::fs::read_to_string(&shared.cfg.template).await {
        Ok(page) => Html(
            page.replace("{{ mount_prefix }}", &shared.cfg.prefix)
                .replace("{{ app_version }}", env!("CARGO_PKG_VERSION")),
        )
        .into_response(),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            format!("queue page missing: {e}"),
        )
            .into_response(),
    }
}

/// The current snapshot, built once per change. ETag lets pollers (the
/// gateway's home page and notifications) get a 304 when nothing changed.
async fn list_queue(State(shared): S, headers: HeaderMap) -> Response {
    let (version, body) = shared.snapshot();
    let etag = format!("W/\"q{version}\"");
    if headers
        .get(header::IF_NONE_MATCH)
        .and_then(|v| v.to_str().ok())
        == Some(etag.as_str())
    {
        return (StatusCode::NOT_MODIFIED, [(header::ETAG, etag)]).into_response();
    }
    (
        [
            (header::CONTENT_TYPE, "application/json".to_owned()),
            (header::ETAG, etag),
            (header::CACHE_CONTROL, "no-cache".to_owned()),
        ],
        body.to_string(),
    )
        .into_response()
}

async fn browser_events(State(shared): S) -> impl IntoResponse {
    let rx = shared.browser.subscribe();
    let (_, first) = shared.snapshot();
    let head = stream::iter([
        Ok::<_, Infallible>(Event::default().retry(Duration::from_millis(2000))),
        Ok(Event::default().event("queue").data(first.as_ref())),
    ]);
    let live = BroadcastStream::new(rx).map(move |item| {
        Ok(match item {
            Ok((name, data)) => Event::default().event(name).data(data.as_ref()),
            // Fell behind: send the whole current queue instead of the missed events.
            Err(tokio_stream::wrappers::errors::BroadcastStreamRecvError::Lagged(_)) => {
                Event::default()
                    .event("queue")
                    .data(shared.snapshot().1.as_ref())
            }
        })
    });
    let sse = Sse::new(head.chain(live)).keep_alive(
        KeepAlive::new()
            .interval(Duration::from_secs(15))
            .text("keep-alive"),
    );
    (
        [
            (header::CACHE_CONTROL, "no-cache"),
            (HeaderName::from_static("x-accel-buffering"), "no"),
        ],
        sse,
    )
}

#[derive(Deserialize)]
struct ReorderBody {
    entry_ids: Vec<String>,
}

async fn reorder(State(shared): S, Json(body): Json<ReorderBody>) -> Response {
    let result = shared.with_queue(|q, now| q.reorder(&body.entry_ids).map(|()| q.fill(now)));
    match result {
        Err(e) => {
            let message = if e.code() == "queue_changed" {
                "The queue changed. Refresh and try again."
            } else {
                "A locked job cannot change position."
            };
            (
                StatusCode::CONFLICT,
                Json(json!({ "error": e.code(), "message": message })),
            )
                .into_response()
        }
        Ok(promoted) => {
            shared.after_change(promoted);
            (
                [(header::CONTENT_TYPE, "application/json")],
                shared.snapshot().1.to_string(),
            )
                .into_response()
        }
    }
}

#[derive(Deserialize)]
struct LockBody {
    is_locked: bool,
}

async fn set_locked(
    State(shared): S,
    Path(entry_id): Path<String>,
    Json(body): Json<LockBody>,
) -> Result<Json<Value>, ApiError> {
    if !shared.with_queue(|q, _| q.set_locked(&entry_id, body.is_locked)) {
        return Err(not_found("No queue entry with that id."));
    }
    shared.publish_queue();
    Ok(Json(
        json!({ "entry_id": entry_id, "is_locked": body.is_locked }),
    ))
}

/// Drop an entry its app will never release (the app is down or no longer
/// knows the job).
fn remove_orphan(shared: &Shared, entry: &Entry, reason: &'static str) -> Value {
    let (removed, promoted) = shared.with_queue(|q, now| q.drop_entry(&entry.entry_id, now));
    let Some(removed) = removed else {
        return json!({ "cancelled": true, "removed": "already_gone" });
    };
    if removed.state == EntryState::Active {
        shared.note_freed(Some(&removed));
    }
    tracing::info!(
        "removed {} ({}/{}): {}",
        short(&entry.entry_id),
        entry.app,
        short(&entry.job_id),
        if reason == "app_down" {
            "its app is not running"
        } else {
            "its app no longer knows the job"
        }
    );
    shared.after_change(promoted);
    json!({ "cancelled": true, "removed": reason })
}

/// The queue page's Cancel: forwarded to the owning app over its stream; the
/// app cancels the job the same way its own Cancel button does.
async fn cancel(State(shared): S, Path(entry_id): Path<String>) -> Response {
    let Some(entry) = shared.read_queue(|q| q.get(&entry_id).cloned()) else {
        return not_found("No queue entry with that id.").into_response();
    };
    let request_id = crate::core::new_token();
    let (tx, rx) = oneshot::channel();
    lock(&shared.replies).insert(request_id.clone(), tx);
    let event = AppEvent::Cancel {
        request_id: request_id.clone(),
        entry_id: entry.entry_id.clone(),
        job_id: entry.job_id.clone(),
    };
    let sent = entry
        .instance
        .as_deref()
        .is_some_and(|i| shared.send(i, event));
    if !sent {
        lock(&shared.replies).remove(&request_id);
        return Json(remove_orphan(&shared, &entry, "app_down")).into_response();
    }
    match tokio::time::timeout(CANCEL_TIMEOUT, rx).await {
        Ok(Ok(reply)) if reply.status == 404 => {
            Json(remove_orphan(&shared, &entry, "orphan")).into_response()
        }
        Ok(Ok(reply)) => {
            let status = StatusCode::from_u16(reply.status).unwrap_or(StatusCode::OK);
            let body = if reply.body.is_null() {
                json!({ "status": reply.status })
            } else {
                reply.body
            };
            (status, Json(body)).into_response()
        }
        _ => {
            lock(&shared.replies).remove(&request_id);
            ApiError(
                StatusCode::BAD_GATEWAY,
                "forward_failed",
                format!("{} did not answer the cancel request.", entry.app),
            )
            .into_response()
        }
    }
}

async fn get_settings(State(shared): S) -> Json<Value> {
    let settings = shared
        .settings
        .read()
        .map(|s| s.clone())
        .unwrap_or_default();
    Json(json!(settings))
}

#[derive(Deserialize)]
struct SettingsPatch {
    #[serde(default)]
    prepare_early: Option<bool>,
    #[serde(default)]
    concurrent: Option<bool>,
    #[serde(default)]
    vram_reserve_mb: Option<u64>,
    #[serde(default)]
    vram_total_mb: Option<u64>,
    /// key -> MiB to set, or null to go back to the recorded peak.
    #[serde(default)]
    vram_overrides: Option<std::collections::BTreeMap<String, Option<u64>>>,
}

async fn put_settings(
    State(shared): S,
    Json(patch): Json<SettingsPatch>,
) -> Result<Json<Value>, ApiError> {
    let settings: Settings = {
        let mut s = shared.settings.write().map_err(|_| {
            ApiError(
                StatusCode::INTERNAL_SERVER_ERROR,
                "settings",
                "settings unavailable".into(),
            )
        })?;
        if let Some(v) = patch.prepare_early {
            s.prepare_early = v;
        }
        if let Some(v) = patch.concurrent {
            s.concurrent = v;
        }
        if let Some(v) = patch.vram_reserve_mb {
            s.vram_reserve_mb = v.min(1 << 20);
        }
        if let Some(v) = patch.vram_total_mb {
            s.vram_total_mb = v.min(1 << 22);
        }
        for (key, value) in patch.vram_overrides.unwrap_or_default() {
            if key.is_empty() || key.chars().count() > 300 {
                continue;
            }
            match value.filter(|v| *v > 0) {
                Some(v) => {
                    s.vram_overrides.insert(key, v.min(1 << 22));
                }
                None => {
                    s.vram_overrides.remove(&key);
                }
            }
        }
        s.clone()
    };
    crate::save_settings(&shared.cfg, &settings)
        .map_err(|e| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "settings", e.to_string()))?;
    tracing::info!(
        "settings: prepare_early = {}, concurrent = {}, reserve = {} MiB",
        settings.prepare_early,
        settings.concurrent,
        settings.vram_reserve_mb
    );
    // Turning concurrent jobs on (or a bigger budget) may start waiting jobs now.
    shared.apply_admission();
    shared.publish_queue();
    Ok(Json(json!(settings)))
}

async fn get_vram(State(shared): S) -> Json<Value> {
    Json(shared.vram_status())
}

#[derive(Deserialize)]
struct ForgetBody {
    key: String,
}

/// Drop a recorded peak (it was measured while something unusual held VRAM).
async fn forget_vram(
    State(shared): S,
    Json(body): Json<ForgetBody>,
) -> Result<Json<Value>, ApiError> {
    if !lock(&shared.vram).forget(&body.key) {
        return Err(not_found("No recorded peak under that name."));
    }
    tracing::info!("VRAM: forgot the peak of {}", body.key);
    shared.apply_admission();
    Ok(Json(shared.vram_status()))
}

/// Ask the host supervisor to restart ComfyUI and its own systemd unit.
async fn restart_comfyui(State(shared): S) -> Response {
    let url = format!("{}/restart", shared.cfg.supervisor_url);
    let result = shared
        .http
        .post(&url)
        .timeout(SUPERVISOR_TIMEOUT)
        .send()
        .await;
    let (status, body) = match result {
        Ok(r) => {
            let status = r.status();
            (status, r.json::<Value>().await.unwrap_or(Value::Null))
        }
        Err(e) => {
            tracing::warn!("ComfyUI restart request failed: {e}");
            return (
                StatusCode::BAD_GATEWAY,
                Json(json!({ "message": "Could not reach the ComfyUI supervisor." })),
            )
                .into_response();
        }
    };
    if status != StatusCode::OK || body.get("status").and_then(Value::as_str) != Some("restarting")
    {
        let message = body
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("ComfyUI restart failed.")
            .to_owned();
        let code = if status == StatusCode::CONFLICT {
            StatusCode::CONFLICT
        } else {
            StatusCode::BAD_GATEWAY
        };
        return (code, Json(json!({ "message": message }))).into_response();
    }
    Json(json!({ "message": "ComfyUI restarted; supervisor restart requested." })).into_response()
}
