//! VRAM: the card's live usage (amdgpu sysfs, readable in the distroless
//! container), how much each kind of job needs, and the `Admission` the queue
//! decides concurrent starts with.
//!
//! Peaks are recorded per job type: service, model and config (`vram_key`).
//! Every second, each running job is charged the reading minus the floor (the
//! lowest reading ever seen: the desktop alone) minus the other running jobs'
//! estimates. Alone, that is its own use; next to others, an upper bound, so a
//! job that used more than it was planned for raises its peak at once, before
//! anything else is let in next to it. VRAM held by programs outside Rocsy
//! (games, the browser, the desktop: procvram.rs) is taken off the reading
//! first, against a floor of Rocsy's own idle use; anything that can't be
//! attributed still counts, so the numbers err high, never low. Admission
//! keeps using the real card-wide reading, games included.
//!
//! The estimate is the all-time peak (an app's own guess can only raise it).
//! The queue page shows each peak, can forget one, or override it.
//!
//! Every GPU is read on its own (gpu.rs): each has its own floor, and a job
//! is charged on each card its role places it on, so its peak is kept per card
//! (`Profile::cards`) as well as overall (the highest card).

use std::{
    collections::{BTreeMap, HashMap},
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::{
    core::{Admission, CardBudget, Entry},
    gpu::{self, Card},
    state::{lock, Settings, Shared},
};

const INTERVAL: Duration = Duration::from_secs(1);
/// Push the live reading to open queue pages this often.
const PUBLISH_EVERY: Duration = Duration::from_secs(2);
/// Recent run peaks kept per profile (shown on the queue page).
const KEEP_RUNS: usize = 5;
const FILE: &str = "vram.json";

/// Which Rocsy host service does each app's GPU work (procvram unit names).
/// LLM turns (uses_llm) also run in Strata when that's the loaded model.
const APP_UNITS: [(&str, &[&str]); 7] = [
    ("VoiceRT", &["rocsy-voice-realtime.service"]),
    ("Vid", &["comfyui-supervisor.service"]),
    ("Krea", &["comfyui-supervisor.service"]),
    ("Mus", &["comfyui-supervisor.service"]),
    ("Model3D", &["comfyui-supervisor.service"]),
    ("Voice", &["rocsy-voice-worker.service"]),
    ("LLM", &["core-strata-serve.service"]),
];

fn comfy_plugin_apps() -> &'static [String] {
    static APPS: std::sync::OnceLock<Vec<String>> = std::sync::OnceLock::new();
    APPS.get_or_init(|| {
        std::env::var("LOCK_COMFY_APPS")
            .unwrap_or_default()
            .split(',')
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect()
    })
}

fn units_of(e: &Entry) -> Vec<&'static str> {
    let mut units: Vec<&'static str> = APP_UNITS
        .iter()
        .filter(|(app, _)| *app == e.app)
        .flat_map(|(_, us)| us.iter().copied())
        .collect();
    // Plugin apps that render in ComfyUI, named in LOCK_COMFY_APPS (comma-separated).
    if units.is_empty() && comfy_plugin_apps().iter().any(|a| a == &e.app) {
        units.push("comfyui-supervisor.service");
    }
    if e.uses_llm != Some(false) {
        units.extend(APP_UNITS.iter().filter(|(a, _)| *a == "LLM").flat_map(|(_, us)| us.iter().copied()));
    }
    units
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Profile {
    /// The highest use ever charged to this job type on any card, MiB.
    #[serde(default)]
    pub peak_mb: u64,
    /// card id -> the highest use charged on that card, MiB.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub cards: BTreeMap<String, u64>,
    /// Each finished run's peak, MiB, oldest first.
    #[serde(default)]
    pub recent: Vec<u64>,
    #[serde(default)]
    pub runs: u64,
    #[serde(default)]
    pub updated_at: Option<String>,
}

impl Profile {
    pub fn learned(&self) -> Option<u64> {
        (self.peak_mb > 0).then_some(self.peak_mb)
    }
}

#[derive(Default, Serialize, Deserialize)]
struct Saved {
    /// One-card files: the floor of the card that was read (now: the primary).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    floor_mb: Option<u64>,
    /// card id -> floor, MiB.
    #[serde(default)]
    floors: BTreeMap<String, u64>,
    /// card id -> lowest reading with outside programs taken off (Rocsy's idle use), MiB.
    #[serde(default)]
    job_floors: BTreeMap<String, u64>,
    #[serde(default)]
    profiles: BTreeMap<String, Profile>,
}

struct Track {
    key: String,
    peak: u64,
}

/// A running job as the sampler sees it: the cards it may use and its
/// planned VRAM on each (same order).
pub struct Running {
    pub entry: Entry,
    pub cards: Vec<usize>,
    pub planned: Vec<Option<u64>>,
}

/// A running job's cards with its planned MiB on each.
type Placed = Vec<(usize, Option<u64>)>;

pub struct Vram {
    cards: Vec<Card>,
    /// Card ids (GPU-uuid), totals and the latest readings, by card index.
    ids: Vec<String>,
    totals: Vec<u64>,
    used: Vec<u64>,
    primary: usize,
    path: PathBuf,
    floors: BTreeMap<String, u64>,
    job_floors: BTreeMap<String, u64>,
    /// MiB outside programs hold on each card (last sample); None = can't tell (no host /proc).
    outside: Option<Vec<u64>>,
    /// MiB each readable Rocsy service holds on each card (last sample).
    units: HashMap<String, Vec<u64>>,
    proc_root: PathBuf,
    profiles: BTreeMap<String, Profile>,
    tracks: HashMap<String, Track>,
}

impl Vram {
    pub fn open(data_dir: &Path, device: Option<&Path>) -> Self {
        let path = data_dir.join(FILE);
        let saved: Saved = std::fs::read_to_string(&path)
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or_default();
        let mut cards = gpu::detect(Path::new("/sys/class/drm"));
        if cards.is_empty() {
            // A configured device directory outside /sys/class/drm.
            cards.extend(device.and_then(Card::open));
        }
        let primary = gpu::primary(&cards, device);
        if cards.is_empty() {
            tracing::warn!("VRAM: no readable GPU memory counters; jobs run one at a time");
        }
        for (i, c) in cards.iter().enumerate() {
            tracing::info!(
                "GPU {i}: {} {} MiB, {} at {} ({}){}",
                c.name,
                c.total_mb().unwrap_or(0),
                c.id,
                c.pci,
                c.path(),
                if i == primary { ", primary" } else { "" }
            );
        }
        let mut vram = Vram {
            ids: cards.iter().map(|c| c.id.clone()).collect(),
            totals: cards.iter().map(|c| c.total_mb().unwrap_or(0)).collect(),
            used: cards.iter().map(|c| c.used_mb().unwrap_or(0)).collect(),
            cards,
            primary,
            path,
            floors: saved.floors,
            job_floors: saved.job_floors,
            outside: None,
            units: HashMap::new(),
            proc_root: PathBuf::from(std::env::var("LOCK_PROC").unwrap_or_else(|_| "/proc".into())),
            profiles: saved.profiles,
            tracks: HashMap::new(),
        };
        vram.migrate(saved.floor_mb);
        vram
    }

    /// One-card files: their floor and peaks were measured on today's primary.
    fn migrate(&mut self, floor_mb: Option<u64>) {
        let Some(id) = self.ids.get(self.primary).cloned() else {
            return;
        };
        let mut moved = false;
        if let Some(f) = floor_mb {
            if !self.floors.contains_key(&id) {
                self.floors.insert(id.clone(), f);
                moved = true;
            }
        }
        for p in self.profiles.values_mut() {
            if p.cards.is_empty() && p.peak_mb > 0 {
                p.cards.insert(id.clone(), p.peak_mb);
                moved = true;
            }
        }
        if moved {
            self.save();
        }
    }

    pub fn available(&self) -> bool {
        self.totals.iter().any(|t| *t > 0)
    }

    fn save(&self) {
        let saved = Saved {
            floor_mb: None,
            floors: self.floors.clone(),
            job_floors: self.job_floors.clone(),
            profiles: self.profiles.clone(),
        };
        let tmp = self.path.with_extension("json.tmp");
        let body = serde_json::to_vec_pretty(&saved).unwrap_or_default();
        if let Err(e) = std::fs::write(&tmp, body).and_then(|_| std::fs::rename(&tmp, &self.path)) {
            tracing::warn!("could not save VRAM profiles: {e}");
        }
    }

    /// One reading of every card: update the floors, follow the running jobs,
    /// and file the footprint of each job that finished.
    fn sample(&mut self, actives: &[Running]) {
        if self.cards.is_empty() {
            return;
        }
        let used: Vec<u64> = self
            .cards
            .iter()
            .enumerate()
            .map(|(i, c)| c.used_mb().unwrap_or(self.used[i]))
            .collect();
        let pcis: Vec<String> = self.cards.iter().map(|c| c.pci.clone()).collect();
        let scan = crate::procvram::scan(&self.proc_root, &pcis);
        if self.record_scan(&used, scan, actives) {
            self.save();
        }
    }

    fn record_scan(&mut self, used: &[u64], scan: Option<crate::procvram::Scan>, actives: &[Running]) -> bool {
        let (outside, units) = match scan {
            Some(s) => (Some(s.outside), s.units),
            None => (None, HashMap::new()),
        };
        self.units = units;
        self.record_with(used, outside, actives)
    }

    /// MiB on `card` held by Rocsy services whose app has no job running now
    /// (ComfyUI keeping models between renders, real-time voice's GPU runtime
    /// after its model unloaded): not charged to the jobs that do run.
    /// Services not in APP_UNITS always count.
    fn idle_units(&self, card: usize, actives: &[Running]) -> u64 {
        let busy: Vec<&str> = actives.iter().flat_map(|r| units_of(&r.entry)).collect();
        self.units
            .iter()
            .filter(|(unit, _)| APP_UNITS.iter().any(|(_, us)| us.contains(&unit.as_str())))
            .filter(|(unit, _)| !busy.contains(&unit.as_str()))
            .filter_map(|(_, mb)| mb.get(card))
            .sum()
    }

    /// The floor jobs are charged against: Rocsy's own idle use when outside
    /// programs can be told apart, else the card-wide floor.
    fn job_floor(&self, card: usize) -> u64 {
        if self.outside.is_none() {
            return self.floor(card);
        }
        self.ids
            .get(card)
            .and_then(|id| self.job_floors.get(id))
            .copied()
            .unwrap_or(0)
    }

    fn floor(&self, card: usize) -> u64 {
        self.ids
            .get(card)
            .and_then(|id| self.floors.get(id))
            .copied()
            .unwrap_or_else(|| self.used.get(card).copied().unwrap_or(0))
    }

    /// `used`: the reading of each card. True when a floor or a profile
    /// changed (save them).
    #[cfg(test)]
    fn record(&mut self, used: &[u64], actives: &[Running]) -> bool {
        self.record_with(used, None, actives)
    }

    /// `outside`: MiB non-Rocsy programs hold on each card (None: unknown).
    fn record_with(&mut self, used: &[u64], outside: Option<Vec<u64>>, actives: &[Running]) -> bool {
        self.used = used.to_vec();
        self.outside = outside;
        let mut dirty = false;
        for (card, reading) in used.iter().enumerate() {
            let Some(id) = self.ids.get(card) else {
                continue;
            };
            // Below 64 MiB on the display card is a reset or a bad read, not a
            // real floor; an idle second card can sit lower.
            let min = if card == self.primary { 64 } else { 8 };
            if *reading >= min && self.floors.get(id).is_none_or(|f| reading < f) {
                self.floors.insert(id.clone(), *reading);
                dirty = true;
            }
            if *reading >= min {
                if let Some(rocsy) = self.rocsy_reading(card, *reading) {
                    // Rocsy's baseline: what's left with every known service taken off too
                    // (drivers, containers). The first one starts at the card-wide floor.
                    let base = rocsy.saturating_sub(self.units.values().filter_map(|mb| mb.get(card)).sum());
                    let start = self.floors.get(id).copied().unwrap_or(base);
                    let current = self.job_floors.get(id).copied().unwrap_or(start);
                    if base < current || !self.job_floors.contains_key(id) {
                        self.job_floors.insert(id.clone(), base.min(current));
                        dirty = true;
                    }
                }
            }
        }
        let now = crate::store::iso(chrono::Utc::now());
        for r in actives {
            let key = r.entry.vram_key();
            for &card in &r.cards {
                let (Some(id), Some(reading)) = (self.ids.get(card).cloned(), used.get(card))
                else {
                    continue;
                };
                // The other jobs on this card, at their plan for it.
                let others: u64 = actives
                    .iter()
                    .filter(|o| o.entry.entry_id != r.entry.entry_id)
                    .filter_map(|o| {
                        o.cards
                            .iter()
                            .position(|c| *c == card)
                            .and_then(|k| o.planned.get(k).copied().flatten())
                    })
                    .sum();
                let rocsy = self.rocsy_reading(card, *reading).unwrap_or(*reading);
                let charged = rocsy
                    .saturating_sub(self.job_floor(card))
                    .saturating_sub(self.idle_units(card, actives))
                    .saturating_sub(others);
                let track = self
                    .tracks
                    .entry(r.entry.entry_id.clone())
                    .or_insert(Track {
                        key: key.clone(),
                        peak: 0,
                    });
                track.peak = track.peak.max(charged);
                // Raise the peaks while the job still runs: the next admission
                // decision already plans with them.
                let profile = self.profiles.entry(key.clone()).or_default();
                if profile.cards.get(&id).is_none_or(|known| charged > *known) {
                    profile.cards.insert(id.clone(), charged);
                    dirty = true;
                }
                if charged > profile.peak_mb {
                    if profile.peak_mb > 0 {
                        tracing::warn!(
                            "VRAM: {key} reached {charged} MiB on GPU {card} (peak was {} MiB)",
                            profile.peak_mb
                        );
                    }
                    profile.peak_mb = charged;
                    profile.updated_at = Some(now.clone());
                    dirty = true;
                }
            }
        }
        let finished: Vec<String> = self
            .tracks
            .keys()
            .filter(|id| !actives.iter().any(|r| &r.entry.entry_id == *id))
            .cloned()
            .collect();
        for id in finished {
            let Some(t) = self.tracks.remove(&id) else {
                continue;
            };
            if t.peak == 0 {
                continue;
            }
            let profile = self.profiles.entry(t.key.clone()).or_default();
            profile.recent.push(t.peak);
            if profile.recent.len() > KEEP_RUNS {
                let extra = profile.recent.len() - KEEP_RUNS;
                profile.recent.drain(..extra);
            }
            profile.runs += 1;
            profile.updated_at = Some(now.clone());
            tracing::info!("VRAM: {} run peaked at {} MiB", t.key, t.peak);
            dirty = true;
        }
        // A job that never used anything (0 everywhere) files no profile.
        self.profiles.retain(|k, p| {
            p.peak_mb > 0 || p.runs > 0 || self.tracks.values().any(|t| &t.key == k)
        });
        dirty
    }

    /// The card's reading minus what outside programs hold (None: unknown).
    fn rocsy_reading(&self, card: usize, reading: u64) -> Option<u64> {
        self.outside
            .as_ref()
            .and_then(|o| o.get(card))
            .map(|o| reading.saturating_sub(*o))
    }

    pub fn forget(&mut self, key: &str) -> bool {
        let removed = self.profiles.remove(key).is_some();
        if removed {
            self.save();
        }
        removed
    }

    /// The primary card: the one chosen on the queue page while it is
    /// installed, else the detected one.
    pub fn primary_for(&self, settings: &Settings) -> usize {
        settings
            .primary_gpu
            .as_ref()
            .and_then(|id| self.ids.iter().position(|i| i == id))
            .unwrap_or(self.primary)
    }

    fn total(&self, card: usize, settings: &Settings) -> u64 {
        if card == self.primary_for(settings) && settings.vram_total_mb > 0 {
            settings.vram_total_mb
        } else {
            self.totals.get(card).copied().unwrap_or(0)
        }
    }

    pub fn admission(&self, settings: &Settings) -> Admission {
        let estimates: HashMap<String, u64> = self
            .profiles
            .iter()
            .filter_map(|(k, p)| p.learned().map(|mb| (k.clone(), mb)))
            .collect();
        let card_estimates: HashMap<String, HashMap<usize, u64>> = self
            .profiles
            .iter()
            .filter(|(_, p)| p.learned().is_some())
            .map(|(k, p)| {
                let per: HashMap<usize, u64> = p
                    .cards
                    .iter()
                    .filter_map(|(id, mb)| self.ids.iter().position(|i| i == id).map(|c| (c, *mb)))
                    .collect();
                (k.clone(), per)
            })
            .collect();
        let overrides = settings
            .vram_overrides
            .iter()
            .map(|(k, v)| (k.clone(), *v))
            .collect();
        let cards = (0..self.ids.len())
            .map(|c| CardBudget {
                total_mb: self.total(c, settings),
                used_mb: self.used.get(c).copied().unwrap_or(0),
                floor_mb: self.floor(c),
            })
            .collect();
        Admission {
            enabled: settings.concurrent && self.available(),
            reserve_mb: settings.vram_reserve_mb,
            cards,
            primary: self.primary_for(settings),
            roles: gpu::resolve(
                &self.ids,
                self.primary_for(settings),
                &settings.gpu_roles,
                settings.prefer_multi_gpu,
            )
            .into_iter()
            .collect(),
            estimates,
            card_estimates,
            overrides,
        }
    }

    /// The GPU layout (cards + what each role runs on).
    pub fn layout(&self, settings: &Settings) -> Value {
        let mut layout = gpu::describe(
            &self.cards,
            self.primary_for(settings),
            &self.used,
            &settings.gpu_roles,
            settings.prefer_multi_gpu,
        );
        layout["auto_primary"] = json!(self.primary);
        layout["primary_chosen"] = json!(settings.primary_gpu);
        layout["primary_missing"] = json!(settings
            .primary_gpu
            .as_ref()
            .is_some_and(|id| !self.ids.contains(id)));
        layout
    }

    /// ROCR_VISIBLE_DEVICES for a role ("" = no cards known: leave it unset).
    pub fn visible(&self, role: &str, settings: &Settings) -> String {
        let roles = gpu::resolve(
            &self.ids,
            self.primary_for(settings),
            &settings.gpu_roles,
            settings.prefer_multi_gpu,
        );
        let picked = roles
            .get(role)
            .cloned()
            .unwrap_or_else(|| vec![self.primary_for(settings)]);
        gpu::visible(&self.cards, &picked)
    }
}

impl Shared {
    pub fn settings_now(&self) -> Settings {
        self.settings
            .read()
            .map(|s| s.clone())
            .unwrap_or_else(|e| e.into_inner().clone())
    }

    /// Hand the queue the current budget and start whatever now fits.
    pub fn apply_admission(&self) {
        let settings = self.settings_now();
        let admission = lock(&self.vram).admission(&settings);
        let promoted = self.with_queue(|q, now| {
            if q.admission() != &admission {
                q.set_admission(admission);
            }
            q.fill(now)
        });
        if !promoted.is_empty() {
            self.after_change(promoted);
        }
    }

    /// The VRAM panel of the queue page. The top-level numbers are the
    /// primary card's (one-card pages); `cards` has every card.
    pub fn vram_status(&self) -> Value {
        let settings = self.settings_now();
        let (running, admission) = self.read_queue(|q| {
            let a = q.admission();
            let running: Vec<(Value, Placed)> = q
                .actives()
                .map(|e| {
                    let on: Placed = a
                        .cards_of(e)
                        .into_iter()
                        .map(|c| (c, q.estimate_on(e, c)))
                        .collect();
                    (
                        json!({
                            "entry_id": e.entry_id, "app": e.app, "label": e.label,
                            "key": e.vram_key(), "estimate_mb": q.estimate(e),
                            "cards": on.iter().map(|(c, _)| *c).collect::<Vec<_>>(),
                        }),
                        on,
                    )
                })
                .collect();
            (running, a.clone())
        });
        let v = lock(&self.vram);
        let primary_index = v.primary_for(&settings);
        let reserve = settings.vram_reserve_mb;
        let cards: Vec<Value> = (0..v.ids.len())
            .map(|c| {
                let jobs: Vec<Value> = running
                    .iter()
                    .filter_map(|(job, on)| {
                        on.iter().find(|(card, _)| *card == c).map(|(_, mb)| {
                            json!({
                                "entry_id": job["entry_id"], "app": job["app"], "label": job["label"],
                                "key": job["key"], "estimate_mb": mb,
                            })
                        })
                    })
                    .collect();
                let floor = v.floor(c);
                let planned = floor + jobs.iter().filter_map(|j| j["estimate_mb"].as_u64()).sum::<u64>();
                let used = v.used.get(c).copied().unwrap_or(0);
                let total = v.total(c, &settings);
                let card = v.cards.get(c);
                json!({
                    "index": c, "id": v.ids[c], "name": card.map(|k| k.name.clone()),
                    "pci": card.map(|k| k.pci.clone()), "device": card.map(Card::path),
                    "primary": c == primary_index, "display": card.is_some_and(|k| k.display),
                    "detected_total_mb": v.totals.get(c), "total_mb": total, "used_mb": used,
                    "floor_mb": v.floors.get(&v.ids[c]), "running": jobs,
                    "free_for_jobs_mb": total.saturating_sub(planned.max(used) + reserve),
                    "outside_mb": v.outside.as_ref().and_then(|o| o.get(c)).copied(),
                    "rocsy_floor_mb": v.job_floors.get(&v.ids[c]),
                })
            })
            .collect();
        let mut keys: Vec<String> = v.profiles.keys().cloned().collect();
        keys.extend(settings.vram_overrides.keys().cloned());
        keys.sort();
        keys.dedup();
        let profiles: Vec<Value> = keys
            .iter()
            .map(|k| {
                let p = v.profiles.get(k);
                json!({
                    "key": k,
                    "peak_mb": p.and_then(Profile::learned),
                    "cards": p.map(|p| p.cards.iter().filter_map(|(id, mb)| {
                        v.ids.iter().position(|i| i == id).map(|c| json!({"index": c, "id": id, "peak_mb": mb}))
                    }).collect::<Vec<_>>()).unwrap_or_default(),
                    "recent": p.map(|p| p.recent.clone()).unwrap_or_default(),
                    "runs": p.map(|p| p.runs).unwrap_or(0),
                    "override_mb": settings.vram_overrides.get(k),
                    "updated_at": p.and_then(|p| p.updated_at.clone()),
                })
            })
            .collect();
        let primary = cards.get(primary_index).cloned().unwrap_or(Value::Null);
        json!({
            "available": v.available(),
            "device": primary["device"],
            "detected_total_mb": primary["detected_total_mb"].as_u64().unwrap_or(0),
            "total_mb": primary["total_mb"].as_u64().unwrap_or(0),
            "used_mb": primary["used_mb"].as_u64().unwrap_or(0),
            "floor_mb": primary["floor_mb"],
            "outside_mb": primary["outside_mb"],
            "reserve_mb": reserve,
            "concurrent": settings.concurrent,
            "active": admission.enabled,
            "free_for_jobs_mb": primary["free_for_jobs_mb"].as_u64().unwrap_or(0),
            "running": running.into_iter().map(|(j, _)| j).collect::<Vec<_>>(),
            "cards": cards,
            "profiles": profiles,
        })
    }
}

pub async fn monitor_loop(shared: Arc<Shared>) {
    let mut tick = tokio::time::interval(INTERVAL);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut published = Instant::now();
    loop {
        tick.tick().await;
        let actives: Vec<Running> = shared.read_queue(|q| {
            let a = q.admission();
            q.actives()
                .map(|e| {
                    let cards = a.cards_of(e);
                    let planned = cards.iter().map(|c| q.estimate_on(e, *c)).collect();
                    Running {
                        entry: e.clone(),
                        cards,
                        planned,
                    }
                })
                .collect()
        });
        lock(&shared.vram).sample(&actives);
        shared.apply_admission();
        if shared.browser.receiver_count() > 0 && published.elapsed() >= PUBLISH_EVERY {
            published = Instant::now();
            let body: Arc<str> = Arc::from(shared.vram_status().to_string());
            let _ = shared.browser.send(("vram", body));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(id: &str, app: &str) -> Entry {
        let mut q = crate::core::Queue::restore(Vec::new(), chrono::Duration::seconds(90));
        let e = q
            .enqueue(
                crate::core::NewEntry {
                    app: app.into(),
                    job_id: id.into(),
                    label: id.into(),
                    is_continuous: false,
                    priority: false,
                    group: None,
                    instance: None,
                    uses_llm: Some(false),
                    model: None,
                    config: None,
                    vram_mb: None,
                    gpu_role: None,
                },
                chrono::Utc::now(),
            )
            .entry;
        Entry {
            entry_id: id.into(),
            ..e
        }
    }

    fn with_cards(n: usize) -> Vram {
        Vram {
            cards: Vec::new(),
            ids: (0..n).map(|i| format!("GPU-{i}")).collect(),
            totals: vec![24_000; n],
            used: vec![0; n],
            primary: 0,
            path: PathBuf::from("/nonexistent/vram.json"),
            floors: BTreeMap::new(),
            job_floors: BTreeMap::new(),
            outside: None,
            units: HashMap::new(),
            proc_root: PathBuf::from("/nonexistent"),
            profiles: BTreeMap::new(),
            tracks: HashMap::new(),
        }
    }

    #[test]
    fn games_are_not_charged_to_jobs() {
        let mut v = empty();
        let rt = entry("rt", "VoiceRT");
        // Idle: 1.5 GiB on the card, 1.2 GiB of it the desktop and a browser.
        v.record_with(&[1_500], Some(vec![1_200]), &[]);
        assert_eq!(v.job_floors["GPU-0"], 300);
        // Real-time voice starts (+1 GiB), then a game loads 9 GiB while it runs.
        v.record_with(&[2_500], Some(vec![1_200]), &[on0(&rt, None)]);
        v.record_with(&[11_500], Some(vec![10_200]), &[on0(&rt, None)]);
        assert_eq!(v.profiles["VoiceRT"].peak_mb, 1_000, "the game's 9 GiB isn't the job's");
        // Without process info it errs high, as before: reading - card floor.
        v.record_with(&[11_500], None, &[on0(&rt, None)]);
        assert_eq!(v.profiles["VoiceRT"].peak_mb, 10_000);
    }

    #[test]
    fn idle_services_of_other_apps_are_not_charged() {
        use crate::procvram::Scan;
        let mut v = empty();
        v.floors.insert("GPU-0".into(), 100);
        let scan = |outside: u64, comfy: u64, rt: u64| Some(Scan {
            outside: vec![outside],
            units: [("comfyui-supervisor.service".to_owned(), vec![comfy]),
                    ("rocsy-voice-realtime.service".to_owned(), vec![rt])].into_iter().collect(),
        });
        // Idle: desktop 1000, ComfyUI keeps 2400 of models, nothing else; 200 is drivers.
        v.record_scan(&[3_600], scan(1_000, 2_400, 0), &[]);
        assert_eq!(v.job_floors["GPU-0"], 100, "starts at the card floor, then the lowest baseline");
        // Real-time voice: +1300 in its own service. ComfyUI's idle 2400 isn't its.
        let rt = entry("rt", "VoiceRT");
        v.record_scan(&[4_900], scan(1_000, 2_400, 1_300), &[on0(&rt, None)]);
        assert_eq!(v.profiles["VoiceRT"].peak_mb, 1_400, "1300 + 200 unattributed (errs high)");
        // A Video render: ComfyUI is its service, so all of ComfyUI counts; idle real-time voice doesn't.
        let vid = entry("vid", "Vid");
        v.record_scan(&[12_100], scan(1_000, 9_600, 1_300), &[on0(&vid, None)]);
        assert_eq!(v.profiles["Vid"].peak_mb, 9_700, "9600 ComfyUI + 200 unattributed - 100 floor");
    }

    fn empty() -> Vram {
        with_cards(1)
    }

    /// One-card shorthand: the job runs on card 0 with this plan.
    fn on0(e: &Entry, planned: Option<u64>) -> Running {
        Running {
            entry: e.clone(),
            cards: vec![0],
            planned: vec![planned],
        }
    }

    #[test]
    fn peaks_are_recorded_alone_and_charged_when_shared() {
        let mut v = empty();
        let mus = entry("m", "Mus");
        let img = entry("i", "Krea");
        v.record(&[1_000], &[]);
        for used in [3_000, 7_000, 6_500] {
            v.record(&[used], &[on0(&mus, None)]);
        }
        assert_eq!(
            v.profiles["Mus"].peak_mb, 6_000,
            "peak 7000 - floor 1000, raised while running"
        );
        assert!(v.record(&[1_200], &[]), "the finished run is filed");
        assert_eq!(v.profiles["Mus"].recent, [6_000]);
        // Together: Image is planned at 4000 but the card shows 13000.
        v.profiles.entry("Krea".into()).or_default().peak_mb = 4_000;
        for used in [9_000, 13_000] {
            v.record(&[used], &[on0(&mus, Some(6_000)), on0(&img, Some(4_000))]);
        }
        assert_eq!(
            v.profiles["Krea"].peak_mb, 6_000,
            "13000 - 1000 floor - Music's 6000"
        );
        assert_eq!(
            v.profiles["Mus"].peak_mb, 8_000,
            "13000 - 1000 - Image's 4000: an upper bound"
        );
        v.record(&[1_000], &[]);
        assert_eq!(v.profiles["Krea"].runs, 1);
    }

    #[test]
    fn nothing_measured_files_no_profile() {
        let mut v = empty();
        v.record(&[1_000], &[]);
        v.record(&[1_000], &[on0(&entry("t", "TestA"), None)]);
        v.record(&[1_000], &[]);
        assert!(v.profiles.is_empty());
    }

    #[test]
    fn peaks_never_drop() {
        let mut v = empty();
        let mus = entry("m", "Mus");
        v.record(&[1_000], &[]);
        v.record(&[9_000], &[on0(&mus, None)]);
        v.record(&[1_000], &[]);
        v.record(&[3_000], &[on0(&entry("m2", "Mus"), None)]);
        v.record(&[1_000], &[]);
        assert_eq!(v.profiles["Mus"].learned(), Some(8_000));
        assert_eq!(v.profiles["Mus"].cards["GPU-0"], 8_000);
        assert_eq!(v.profiles["Mus"].recent, [8_000, 2_000]);
    }

    #[test]
    fn each_card_has_its_own_floor_and_charges() {
        let mut v = with_cards(2);
        v.record(&[1_000, 300], &[]);
        let render = entry("r", "Krea");
        let mut chat = entry("c", "Agent");
        chat.uses_llm = Some(true);
        let both = [
            Running {
                entry: render.clone(),
                cards: vec![0],
                planned: vec![None],
            },
            Running {
                entry: chat.clone(),
                cards: vec![0, 1],
                planned: vec![None, None],
            },
        ];
        v.record(&[11_000, 9_300], &both);
        // Card 1 only has the LLM: 9300 - 300.
        assert_eq!(v.profiles["Agent · LLM"].cards["GPU-1"], 9_000);
        assert_eq!(
            v.profiles["Agent · LLM"].peak_mb, 10_000,
            "card 0 upper bound: 11000 - 1000"
        );
        assert_eq!(v.profiles["Krea"].cards["GPU-0"], 10_000);
        assert!(
            !v.profiles["Krea"].cards.contains_key("GPU-1"),
            "never charged where it doesn't run"
        );
        let a = v.admission(&Settings {
            concurrent: true,
            ..Settings::default()
        });
        assert_eq!(a.cards.len(), 2);
        assert_eq!(a.cards[1].floor_mb, 300);
        assert_eq!(a.card_estimates["Agent · LLM"][&1], 9_000);
    }

    #[test]
    fn the_chosen_primary_wins_while_it_is_installed() {
        let mut v = with_cards(2);
        let mut s = Settings::default();
        assert_eq!(v.primary_for(&s), 0);
        s.primary_gpu = Some("GPU-1".into());
        assert_eq!(v.primary_for(&s), 1);
        let a = v.admission(&s);
        assert_eq!(a.primary, 1);
        assert_eq!(a.roles["llama"], [1]);
        assert_eq!(a.roles["comfyui_multi"], [1, 0], "primary first");
        // The card total override follows the chosen primary.
        s.vram_total_mb = 16_000;
        assert_eq!(v.admission(&s).cards[1].total_mb, 16_000);
        assert_eq!(v.admission(&s).cards[0].total_mb, 24_000);
        s.primary_gpu = Some("GPU-gone".into());
        assert_eq!(
            v.primary_for(&s),
            0,
            "a removed card falls back to automatic"
        );
        v.primary = 1;
        s.primary_gpu = None;
        assert_eq!(v.primary_for(&s), 1);
    }

    #[test]
    fn one_card_files_move_onto_the_primary() {
        let mut v = with_cards(2);
        v.primary = 1;
        v.profiles.insert(
            "Mus".into(),
            Profile {
                peak_mb: 7_000,
                ..Profile::default()
            },
        );
        v.migrate(Some(900));
        assert_eq!(v.floors["GPU-1"], 900);
        assert_eq!(v.profiles["Mus"].cards["GPU-1"], 7_000);
        let saved: Saved =
            serde_json::from_str(r#"{"floor_mb": 800, "profiles": {"X": {"peak_mb": 5}}}"#)
                .unwrap_or_else(|e| panic!("{e}"));
        assert_eq!(saved.floor_mb, Some(800));
        assert!(saved.profiles["X"].cards.is_empty());
    }

    #[test]
    fn unmeasured_profile_has_no_estimate() {
        assert_eq!(Profile::default().learned(), None);
    }
}
