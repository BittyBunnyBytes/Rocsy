//! bunny-lock: Rocsy's shared GPU queue.
//!
//! One job holds the GPU at a time, or several when concurrent jobs are on
//! and their VRAM fits (vram.rs). Each app process keeps one lease stream
//! open (`GET /v2/stream`); turns, prepare-early notices and cancels arrive on
//! it, and the stream closing frees that process's turn. The protocol is
//! described in PROTOCOL.md; apps use `BunnyBytes/shared/bunnylock.py`.

mod config;
mod core;
mod llm;
mod metrics;
mod progress;
mod routes;
mod state;
mod store;
mod vram;

use std::{net::SocketAddr, path::Path, sync::Arc, time::Duration};

use crate::{
    config::Config,
    core::{Queue, State},
    metrics::METRICS,
    state::{short, Settings, Shared},
    store::Store,
};

const TICK: Duration = Duration::from_millis(250);

fn settings_path(cfg: &Config) -> std::path::PathBuf {
    cfg.data_dir.join("settings.json")
}

fn load_settings(cfg: &Config) -> Settings {
    std::fs::read_to_string(settings_path(cfg))
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

pub fn save_settings(cfg: &Config, settings: &Settings) -> std::io::Result<()> {
    let path = settings_path(cfg);
    let tmp = path.with_extension("json.tmp");
    std::fs::write(
        &tmp,
        serde_json::to_vec_pretty(settings).unwrap_or_default(),
    )?;
    std::fs::rename(tmp, path)
}

/// Every 250 ms: expire dead leases and handle app processes whose stream
/// closed (GPU turn moves on, waiting entries kept for adoption).
async fn tick_loop(shared: Arc<Shared>) {
    let mut tick = tokio::time::interval(TICK);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        tick.tick().await;
        let due = shared.read_queue(|q| {
            q.actives()
                .any(|a| a.expires_at.is_none_or(|t| t <= chrono::Utc::now()))
        });
        if due {
            let (expired, promoted) = shared.with_queue(|q, now| q.reap(now));
            for e in &expired {
                METRICS.expired.inc();
                tracing::warn!(
                    "{} ({}/{}) lease ran out without a release",
                    short(&e.entry_id),
                    e.app,
                    short(&e.job_id)
                );
                shared.note_freed(Some(e));
            }
            shared.after_change(promoted);
        }
        shared.check_instances();
    }
}

async fn shutdown_signal() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let term = async {
        if let Ok(mut s) = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            s.recv().await;
        }
    };
    #[cfg(not(unix))]
    let term = std::future::pending::<()>();
    tokio::select! { _ = ctrl_c => {}, _ = term => {} }
    tracing::info!("shutting down");
    // Lease streams never end on their own; every write is already on disk,
    // so don't let them hold the shutdown open.
    tokio::spawn(async {
        tokio::time::sleep(Duration::from_secs(2)).await;
        std::process::exit(0);
    });
}

fn open(cfg: &Config) -> Result<(Queue, Store), Box<dyn std::error::Error>> {
    std::fs::create_dir_all(&cfg.data_dir)?;
    let store = Store::open(&Path::new(&cfg.data_dir).join("queue.sqlite3"))?;
    let ttl = chrono::Duration::from_std(cfg.ttl)?;
    let mut queue = Queue::restore(store.load_all()?, ttl);
    // An active holder from before a restart is only reaped once its lease
    // really ran out: a job still rendering must keep its slot.
    let (expired, _) = queue.reap(chrono::Utc::now());
    for e in expired {
        tracing::info!(
            "startup: {} ({}/{}) lease had run out",
            short(&e.entry_id),
            e.app,
            short(&e.job_id)
        );
    }
    let waiting = queue
        .entries()
        .iter()
        .filter(|e| e.state == State::Waiting)
        .count();
    tracing::info!(
        "restored {} entries ({waiting} waiting)",
        queue.entries().len()
    );
    Ok((queue, store))
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .with_target(false)
        .with_ansi(false)
        .init();
    let cfg = match Config::from_env() {
        Ok(cfg) => cfg,
        Err(e) => {
            tracing::error!("{e}");
            std::process::exit(2);
        }
    };
    let (mut queue, mut store) = match open(&cfg) {
        Ok(v) => v,
        Err(e) => {
            tracing::error!("could not open the queue database: {e}");
            std::process::exit(1);
        }
    };
    let dirty = queue.take_dirty();
    if let Err(e) = store.sync(&dirty, |id| queue.get(id).cloned()) {
        tracing::error!("could not save the queue: {e}");
    }
    let settings = load_settings(&cfg);
    let addr: SocketAddr = match format!("{}:{}", cfg.host, cfg.port).parse() {
        Ok(a) => a,
        Err(e) => {
            tracing::error!("bad LOCK_HOST/LOCK_PORT: {e}");
            std::process::exit(2);
        }
    };
    let vram = vram::Vram::open(&cfg.data_dir, cfg.vram_device.as_deref());
    let shared = Shared::new(cfg, queue, store, settings, vram);
    // A promotion that happened during startup reaping is delivered once the
    // owning app reconnects (connect re-sends unacked grants).
    tokio::spawn(tick_loop(shared.clone()));
    tokio::spawn(progress::poll_loop(shared.clone()));
    tokio::spawn(llm::guard_loop(shared.clone()));
    tokio::spawn(llm::hermes_loop(shared.clone()));
    tokio::spawn(vram::monitor_loop(shared.clone()));

    let app = routes::router(shared.clone());
    let listener = match tokio::net::TcpListener::bind(addr).await {
        Ok(l) => l,
        Err(e) => {
            tracing::error!("cannot listen on {addr}: {e}");
            std::process::exit(1);
        }
    };
    tracing::info!("bunny-lock {} on {addr}", env!("CARGO_PKG_VERSION"));
    let served = axum::serve(
        listener,
        app.into_make_service_with_connect_info::<SocketAddr>(),
    )
    .with_graceful_shutdown(shutdown_signal())
    .await;
    if let Err(e) = served {
        tracing::error!("server error: {e}");
    }
}
