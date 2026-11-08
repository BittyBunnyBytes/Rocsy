//! Shared server state: the queue (+ its SQLite store), the app processes
//! connected over lease streams, and the browser snapshot.
//!
//! Lock order: never hold two of these mutexes at once. Every queue call
//! takes `queue`, writes the changed rows and drops it before anything is
//! sent to an app or a browser.

use std::{
    collections::{BTreeMap, HashMap, HashSet, VecDeque},
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex, MutexGuard, RwLock,
    },
    time::{Duration, Instant},
};

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::{broadcast, mpsc, oneshot};

use crate::{
    config::Config,
    core::{Entry, Queue, State},
    metrics::METRICS,
    store::{iso, Store},
};

/// What the queue sends down an app's lease stream.
#[derive(Clone, Debug, Serialize)]
#[serde(tag = "event", content = "data", rename_all = "lowercase")]
pub enum AppEvent {
    Hello {
        instance: String,
    },
    Grant {
        entry_id: String,
        job_id: String,
        token: String,
        /// Apps of the jobs already on the GPU (concurrent jobs): don't free
        /// VRAM or unload models they use.
        others: Vec<String>,
    },
    Prepare {
        entry_id: String,
        job_id: String,
    },
    Cancel {
        request_id: String,
        entry_id: String,
        job_id: String,
    },
    /// The app restarted: an entry of its previous process. Answer POST /v2/adopt.
    Adopt {
        entry_id: String,
        job_id: String,
        active: bool,
        token: Option<String>,
    },
}

/// An app's answer to a forwarded cancel (POST /v2/reply).
#[derive(Debug, Deserialize)]
pub struct Reply {
    pub status: u16,
    #[serde(default)]
    pub body: Value,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Settings {
    /// Tell the next job's app to get its CPU-side work done while the
    /// current job still renders.
    #[serde(default = "yes")]
    pub prepare_early: bool,
    /// Start the next job next to the running ones when its VRAM fits.
    #[serde(default)]
    pub concurrent: bool,
    /// Always kept free when jobs share the GPU, MiB.
    #[serde(default = "default_reserve")]
    pub vram_reserve_mb: u64,
    /// 0 = the card's own size.
    #[serde(default)]
    pub vram_total_mb: u64,
    /// vram_key -> MiB set by hand (replaces the recorded peak).
    #[serde(default)]
    pub vram_overrides: BTreeMap<String, u64>,
}

fn yes() -> bool {
    true
}

fn default_reserve() -> u64 {
    1536
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            prepare_early: true,
            concurrent: false,
            vram_reserve_mb: default_reserve(),
            vram_total_mb: 0,
            vram_overrides: BTreeMap::new(),
        }
    }
}

pub struct Conn {
    id: u64,
    tx: mpsc::UnboundedSender<AppEvent>,
}

pub struct Instance {
    pub app: String,
    conns: Vec<Conn>,
    /// Set while no stream is open (see check_instances for what happens then).
    disconnected_at: Option<Instant>,
    /// Added to the grace periods: time for every app to reconnect after a queue restart.
    grace: Duration,
}

impl Instance {
    fn connected(&self) -> bool {
        !self.conns.is_empty()
    }
}

#[derive(Default)]
pub struct Marks {
    /// When the GPU last became free with someone waiting.
    pub freed_at: Option<Instant>,
    pub enqueued: HashMap<String, Instant>,
    pub granted: HashMap<String, Instant>,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Progress {
    pub percent: f64,
    pub remaining_seconds: Option<i64>,
    pub stage: Option<String>,
}

#[derive(Default)]
pub struct ProgressState {
    pub by_entry: HashMap<String, Progress>,
    /// The app does not know the job (Hermes turns etc.): stop polling it.
    pub unknown: HashSet<String>,
    /// Entries whose app pushes progress itself, with the last push time.
    pub pushed_at: HashMap<String, Instant>,
    pub published_at: Option<Instant>,
    pub unpublished: bool,
    pub flush_scheduled: bool,
}

pub struct QueueBox {
    pub queue: Queue,
    pub store: Store,
}

pub struct Shared {
    pub cfg: Config,
    queue: Mutex<QueueBox>,
    instances: Mutex<HashMap<String, Instance>>,
    pub replies: Mutex<HashMap<String, oneshot::Sender<Reply>>>,
    pub settings: RwLock<Settings>,
    pub progress: Mutex<ProgressState>,
    pub hermes_card: Mutex<Option<Value>>,
    pub vram: Mutex<crate::vram::Vram>,
    prepared: Mutex<HashSet<String>>,
    /// Entries offered to a restarted app, waiting for its answer (deadline).
    adoptions: Mutex<HashMap<String, Instant>>,
    pub llm_guard_until: Mutex<Option<Instant>>,
    pub marks: Mutex<Marks>,
    pub http: reqwest::Client,
    pub browser: broadcast::Sender<(&'static str, Arc<str>)>,
    snapshot: RwLock<(u64, Arc<str>)>,
    version: AtomicU64,
    next_conn: AtomicU64,
    pub started: Instant,
    /// Seconds since start of the last browser/gateway read (lazy status polling).
    pub last_viewed: AtomicU64,
}

/// Lock a mutex; a poisoned one (impossible with panic = "abort") is still usable.
pub fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// How long a restarted app has to answer an adoption offer.
const ADOPT_REPLY: Duration = Duration::from_secs(30);

pub fn short(id: &str) -> &str {
    id.get(..8).unwrap_or(id)
}

impl Shared {
    pub fn new(
        cfg: Config,
        queue: Queue,
        store: Store,
        settings: Settings,
        vram: crate::vram::Vram,
    ) -> Arc<Self> {
        let (browser, _) = broadcast::channel(64);
        let now = Instant::now();
        // Every app process that owned entries before a restart gets time to reconnect.
        let instances = queue
            .entries()
            .iter()
            .filter_map(|e| e.instance.clone().map(|i| (i, e.app.clone())))
            .map(|(instance, app)| {
                (
                    instance,
                    Instance {
                        app,
                        conns: Vec::new(),
                        disconnected_at: Some(now),
                        grace: cfg.startup_grace,
                    },
                )
            })
            .collect();
        let shared = Arc::new(Shared {
            http: reqwest::Client::builder()
                .pool_idle_timeout(Duration::from_secs(90))
                .build()
                .unwrap_or_default(),
            cfg,
            queue: Mutex::new(QueueBox { queue, store }),
            instances: Mutex::new(instances),
            replies: Mutex::new(HashMap::new()),
            settings: RwLock::new(settings),
            progress: Mutex::new(ProgressState::default()),
            hermes_card: Mutex::new(None),
            vram: Mutex::new(vram),
            prepared: Mutex::new(HashSet::new()),
            adoptions: Mutex::new(HashMap::new()),
            llm_guard_until: Mutex::new(None),
            marks: Mutex::new(Marks::default()),
            browser,
            snapshot: RwLock::new((0, Arc::from("{\"entries\":[]}"))),
            version: AtomicU64::new(0),
            next_conn: AtomicU64::new(1),
            started: now,
            last_viewed: AtomicU64::new(0),
        });
        shared.rebuild_snapshot();
        shared
    }

    // ------------------------------------------------------------------ queue

    /// Run one queue call, then write exactly the rows it changed.
    pub fn with_queue<R>(&self, f: impl FnOnce(&mut Queue, DateTime<Utc>) -> R) -> R {
        let mut guard = lock(&self.queue);
        let QueueBox { queue, store } = &mut *guard;
        let result = f(queue, Utc::now());
        let dirty = queue.take_dirty();
        if let Err(e) = store.sync(&dirty, |id| queue.get(id).cloned()) {
            tracing::error!("could not save the queue: {e}");
        }
        result
    }

    pub fn read_queue<R>(&self, f: impl FnOnce(&Queue) -> R) -> R {
        f(&lock(&self.queue).queue)
    }

    /// After any change: hand the GPU to whoever became active, tell the next
    /// job to prepare, and push the new queue to browsers.
    pub fn after_change(&self, promoted: Vec<Entry>) {
        let mut pending: VecDeque<Entry> = promoted.into();
        while let Some(entry) = pending.pop_front() {
            pending.extend(self.grant(entry));
        }
        self.maybe_prepare();
        self.publish_queue();
    }

    /// Apps of the other jobs holding the GPU.
    fn others_on_gpu(&self, entry_id: &str) -> Vec<String> {
        self.read_queue(|q| {
            q.actives()
                .filter(|e| e.entry_id != entry_id)
                .map(|e| e.app.clone())
                .collect()
        })
    }

    /// Send a grant to the entry's app process. An entry no live process can
    /// ever take (no instance recorded) is dropped and whatever that starts returned.
    fn grant(&self, entry: Entry) -> Vec<Entry> {
        let token = entry.token.clone().unwrap_or_default();
        {
            let mut marks = lock(&self.marks);
            if let Some(t) = marks.enqueued.remove(&entry.entry_id) {
                METRICS.wait.observe(t.elapsed());
            }
            marks.granted.insert(entry.entry_id.clone(), Instant::now());
        }
        METRICS.grants.inc();
        let Some(instance) = entry.instance.clone() else {
            tracing::warn!(
                "entry {} ({}/{}) has no app connection; dropping it",
                short(&entry.entry_id),
                entry.app,
                short(&entry.job_id)
            );
            return self.with_queue(|q, now| q.drop_entry(&entry.entry_id, now).1);
        };
        // A waiting entry left behind by an app that has been gone too long:
        // don't park the GPU on it, hand it straight on.
        if self
            .gone_for(&instance)
            .is_some_and(|g| g >= self.cfg.active_grace)
        {
            tracing::warn!(
                "{} ({}/{}): its app is gone; skipping it",
                short(&entry.entry_id),
                entry.app,
                short(&entry.job_id)
            );
            return self.with_queue(|q, now| q.drop_entry(&entry.entry_id, now).1);
        }
        let others = self.others_on_gpu(&entry.entry_id);
        let sharing = if others.is_empty() {
            String::new()
        } else {
            format!(", next to {}", others.join(", "))
        };
        let event = AppEvent::Grant {
            entry_id: entry.entry_id.clone(),
            job_id: entry.job_id.clone(),
            token,
            others,
        };
        if self.send(&instance, event) {
            tracing::info!(
                "GPU -> {} ({}/{}){sharing}",
                short(&entry.entry_id),
                entry.app,
                short(&entry.job_id)
            );
        } else {
            // Its process is reconnecting (or gone): hello re-sends the grant,
            // the liveness check drops it after the grace period.
            tracing::info!(
                "GPU -> {} ({}/{}), delivered on reconnect",
                short(&entry.entry_id),
                entry.app,
                short(&entry.job_id)
            );
        }
        Vec::new()
    }

    fn maybe_prepare(&self) {
        if !self
            .settings
            .read()
            .map(|s| s.prepare_early)
            .unwrap_or(true)
        {
            return;
        }
        let (ids, next) = self.read_queue(|q| {
            let ids: HashSet<String> = q.entries().iter().map(|e| e.entry_id.clone()).collect();
            (
                ids,
                q.has_active().then(|| q.first_waiting().cloned()).flatten(),
            )
        });
        {
            let mut prepared = lock(&self.prepared);
            prepared.retain(|id| ids.contains(id));
            if next.as_ref().is_none_or(|n| prepared.contains(&n.entry_id)) {
                return;
            }
        }
        let Some(next) = next else { return };
        let Some(instance) = next.instance.clone() else {
            return;
        };
        if self.send(
            &instance,
            AppEvent::Prepare {
                entry_id: next.entry_id.clone(),
                job_id: next.job_id.clone(),
            },
        ) {
            lock(&self.prepared).insert(next.entry_id);
            METRICS.prepares.inc();
        }
    }

    pub fn note_enqueued(&self, entry_id: &str) {
        lock(&self.marks)
            .enqueued
            .insert(entry_id.to_owned(), Instant::now());
    }

    /// The GPU became free; the next ack measures the handoff gap.
    pub fn note_freed(&self, released: Option<&Entry>) {
        let mut marks = lock(&self.marks);
        if let Some(e) = released {
            if let Some(t) = marks.granted.remove(&e.entry_id) {
                METRICS.lease.observe(t.elapsed());
            }
            marks.enqueued.remove(&e.entry_id);
        }
        marks.freed_at = Some(Instant::now());
    }

    pub fn note_acked(&self, entry_id: &str) {
        let mut marks = lock(&self.marks);
        if let Some(t) = marks.granted.get(entry_id) {
            METRICS.grant_to_ack.observe(t.elapsed());
        }
        if let Some(t) = marks.freed_at.take() {
            METRICS.handoff_gap.observe(t.elapsed());
        }
    }

    // -------------------------------------------------------------- instances

    /// A lease stream opened. A different, disconnected process of the same
    /// app means that app restarted: its old entries are dropped at once.
    pub fn connect(&self, app: &str, instance: &str) -> (u64, mpsc::UnboundedReceiver<AppEvent>) {
        let (tx, rx) = mpsc::unbounded_channel();
        let id = self.next_conn.fetch_add(1, Ordering::Relaxed);
        let _ = tx.send(AppEvent::Hello {
            instance: instance.to_owned(),
        });
        let stale: Vec<String> = {
            let mut instances = lock(&self.instances);
            let inst = instances
                .entry(instance.to_owned())
                .or_insert_with(|| Instance {
                    app: app.to_owned(),
                    conns: Vec::new(),
                    disconnected_at: None,
                    grace: Duration::ZERO,
                });
            inst.conns.push(Conn { id, tx: tx.clone() });
            inst.disconnected_at = None;
            inst.grace = Duration::ZERO;
            instances
                .iter()
                .filter(|(k, v)| k.as_str() != instance && v.app == app && !v.connected())
                .map(|(k, _)| k.clone())
                .collect()
        };
        // The same app restarted: offer it its old process's entries. It keeps
        // the ones it saved (they keep their place and any GPU turn) and drops
        // the rest; an entry it doesn't answer for is dropped after ADOPT_REPLY.
        for old in stale {
            lock(&self.instances).remove(&old);
            let moved = self.with_queue(|q, _| q.rebind_instance(&old, instance));
            if moved.is_empty() {
                continue;
            }
            tracing::info!(
                "{app} restarted: offering it {} queued entr{}",
                moved.len(),
                if moved.len() == 1 { "y" } else { "ies" }
            );
            let deadline = Instant::now() + ADOPT_REPLY;
            for e in moved {
                lock(&self.adoptions).insert(e.entry_id.clone(), deadline);
                let active = e.state == State::Active;
                let _ = tx.send(AppEvent::Adopt {
                    entry_id: e.entry_id,
                    job_id: e.job_id,
                    active,
                    token: e.token.filter(|_| active),
                });
            }
        }
        // Re-send what this process missed while it was away.
        let (unacked, first) = self.read_queue(|q| {
            let unacked: Vec<Entry> = q
                .actives()
                .filter(|e| !e.acked && e.instance.as_deref() == Some(instance))
                .cloned()
                .collect();
            (unacked, q.first_waiting().map(|e| e.entry_id.clone()))
        });
        for e in unacked {
            let others = self.others_on_gpu(&e.entry_id);
            let _ = tx.send(AppEvent::Grant {
                entry_id: e.entry_id,
                job_id: e.job_id,
                token: e.token.unwrap_or_default(),
                others,
            });
        }
        if first.is_some() {
            lock(&self.prepared).clear(); // re-offer prepare to the reconnected process
            self.maybe_prepare();
        }
        tracing::info!("{app} connected (process {})", short(instance));
        (id, rx)
    }

    pub fn disconnect(&self, instance: &str, conn: u64) {
        let mut instances = lock(&self.instances);
        if let Some(inst) = instances.get_mut(instance) {
            inst.conns.retain(|c| c.id != conn);
            if !inst.connected() {
                inst.disconnected_at = Some(Instant::now());
                tracing::info!("{} disconnected (process {})", inst.app, short(instance));
            }
        }
    }

    /// Make sure an enqueuing process is known even before its stream opens,
    /// so a process that never connects is still cleaned up.
    pub fn register_instance(&self, app: &str, instance: &str) {
        lock(&self.instances)
            .entry(instance.to_owned())
            .or_insert_with(|| Instance {
                app: app.to_owned(),
                conns: Vec::new(),
                disconnected_at: Some(Instant::now()),
                grace: Duration::ZERO,
            });
    }

    /// Send to the newest stream of a process; false if it has none.
    pub fn send(&self, instance: &str, event: AppEvent) -> bool {
        let mut instances = lock(&self.instances);
        let Some(inst) = instances.get_mut(instance) else {
            return false;
        };
        while let Some(conn) = inst.conns.last() {
            if conn.tx.send(event.clone()).is_ok() {
                return true;
            }
            inst.conns.pop();
        }
        false
    }

    pub fn drop_instance(&self, instance: &str, why: &str) {
        lock(&self.instances).remove(instance);
        let (removed, promoted) = self.with_queue(|q, now| q.drop_instance(instance, now));
        if removed.is_empty() && promoted.is_empty() {
            return;
        }
        METRICS.dead_instances.inc();
        for e in &removed {
            tracing::warn!(
                "dropped {} ({}/{}): {why}",
                short(&e.entry_id),
                e.app,
                short(&e.job_id)
            );
        }
        for active in removed.iter().filter(|e| e.state == State::Active) {
            self.note_freed(Some(active));
        }
        self.after_change(promoted);
    }

    /// How long the process has been gone, beyond its startup allowance.
    fn gone_for(&self, instance: &str) -> Option<Duration> {
        let instances = lock(&self.instances);
        let inst = instances.get(instance)?;
        inst.disconnected_at
            .map(|t| t.elapsed().saturating_sub(inst.grace))
    }

    /// Every tick: a disconnected process's GPU turn moves on after
    /// active_grace; its waiting entries are dropped after adopt_grace (the
    /// app didn't come back); adoption offers nobody answered are dropped.
    pub fn check_instances(&self) {
        let holders: Vec<Entry> = self.read_queue(|q| q.actives().cloned().collect());
        let gone: Vec<String> = holders
            .into_iter()
            .filter(|h| {
                h.instance
                    .as_deref()
                    .and_then(|i| self.gone_for(i))
                    .is_some_and(|g| g >= self.cfg.active_grace)
            })
            .map(|h| h.entry_id)
            .collect();
        if !gone.is_empty() {
            self.drop_entries(&gone, "its app is gone; the GPU moves on");
        }
        let expired: Vec<String> = {
            let instances = lock(&self.instances);
            instances
                .iter()
                .filter(|(_, v)| {
                    v.disconnected_at.is_some_and(|t| {
                        t.elapsed().saturating_sub(v.grace) >= self.cfg.adopt_grace
                    })
                })
                .map(|(k, _)| k.clone())
                .collect()
        };
        for instance in expired {
            self.drop_instance(&instance, "its app did not come back");
        }
        let unanswered: Vec<String> = {
            let mut adoptions = lock(&self.adoptions);
            let now = Instant::now();
            let ids: Vec<String> = adoptions
                .iter()
                .filter(|(_, d)| **d <= now)
                .map(|(k, _)| k.clone())
                .collect();
            for id in &ids {
                adoptions.remove(id);
            }
            ids
        };
        if !unanswered.is_empty() {
            self.drop_entries(&unanswered, "its restarted app did not adopt it");
        }
    }

    /// The restarted app's answer to an adoption offer.
    pub fn adopted(&self, entry_id: &str, keep: bool) {
        if lock(&self.adoptions).remove(entry_id).is_none() {
            return;
        }
        if keep {
            tracing::info!("{} kept by its restarted app", short(entry_id));
            self.publish_queue();
        } else {
            self.drop_entries(
                &[entry_id.to_owned()],
                "its restarted app no longer has the job",
            );
        }
    }

    /// Remove entries (waiting or active) and hand the GPU on if it was freed.
    pub fn drop_entries(&self, ids: &[String], why: &str) {
        let mut promoted = Vec::new();
        for id in ids {
            let (removed, next) = self.with_queue(|q, now| q.drop_entry(id, now));
            if let Some(e) = removed {
                tracing::warn!(
                    "dropped {} ({}/{}): {why}",
                    short(&e.entry_id),
                    e.app,
                    short(&e.job_id)
                );
                if e.state == State::Active {
                    METRICS.dead_instances.inc();
                    self.note_freed(Some(&e));
                }
            }
            promoted.extend(next);
        }
        self.after_change(promoted);
    }

    pub fn connected_apps(&self) -> Vec<String> {
        let mut apps: Vec<String> = lock(&self.instances)
            .values()
            .filter(|v| v.connected())
            .map(|v| v.app.clone())
            .collect();
        apps.sort();
        apps.dedup();
        apps
    }

    // ---------------------------------------------------------------- browser

    pub fn snapshot(&self) -> (u64, Arc<str>) {
        self.last_viewed
            .store(self.started.elapsed().as_secs(), Ordering::Relaxed);
        self.snapshot
            .read()
            .map(|s| s.clone())
            .unwrap_or_else(|e| e.into_inner().clone())
    }

    pub fn viewed_recently(&self) -> bool {
        self.started
            .elapsed()
            .as_secs()
            .saturating_sub(self.last_viewed.load(Ordering::Relaxed))
            < 60
            || self.browser.receiver_count() > 0
    }

    fn public_entries(&self) -> Vec<Value> {
        let progress = lock(&self.progress).by_entry.clone();
        let mut entries: Vec<Value> = self.read_queue(|q| {
            let mut position = 0;
            q.entries()
                .iter()
                .map(|e| {
                    let pos = (e.state == State::Waiting).then(|| {
                        position += 1;
                        position
                    });
                    json!({
                        "entry_id": e.entry_id, "app": e.app, "job_id": e.job_id, "label": e.label,
                        "state": e.state.as_str(), "is_continuous": e.is_continuous, "shutdown_at": e.shutdown_at,
                        "enqueued_at": iso(e.enqueued_at), "is_locked": e.is_locked,
                        "became_active_at": e.became_active_at.map(iso), "expires_at": e.expires_at.map(iso),
                        "position": pos, "group": e.group, "progress": progress.get(&e.entry_id),
                        "uses_llm": e.uses_llm, "vram_key": e.vram_key(), "vram_mb": q.estimate(e),
                    })
                })
                .collect()
        });
        // The Master Agent's turns are Hermes runs: fold the Hermes card into
        // the Agent entry (the active one if any), else show it on its own.
        if let Some(card) = lock(&self.hermes_card).clone() {
            let agent = entries
                .iter()
                .position(|e| e["app"] == "Agent" && e["state"] == "active")
                .or_else(|| entries.iter().position(|e| e["app"] == "Agent"));
            match agent {
                Some(i) => {
                    entries[i]["hermes"] =
                        json!({"runs": card["runs"], "loaded_model": card["loaded_model"]})
                }
                None => entries.insert(0, card),
            }
        }
        entries
    }

    fn rebuild_snapshot(&self) -> Arc<str> {
        let body: Arc<str> = Arc::from(json!({ "entries": self.public_entries() }).to_string());
        let version = self.version.fetch_add(1, Ordering::Relaxed) + 1;
        if let Ok(mut s) = self.snapshot.write() {
            *s = (version, body.clone());
        }
        body
    }

    pub fn publish_queue(&self) {
        let body = self.rebuild_snapshot();
        let _ = self.browser.send(("queue", body));
    }

    pub fn publish_progress(&self) {
        let body: Arc<str> = {
            let mut p = lock(&self.progress);
            p.published_at = Some(Instant::now());
            p.unpublished = false;
            Arc::from(json!({ "progress": p.by_entry }).to_string())
        };
        self.rebuild_snapshot();
        let _ = self.browser.send(("progress", body));
    }
}
