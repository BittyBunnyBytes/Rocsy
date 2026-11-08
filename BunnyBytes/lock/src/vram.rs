//! VRAM: the card's live usage (amdgpu sysfs, readable in the distroless
//! container), how much each kind of job needs, and the `Admission` the queue
//! decides concurrent starts with.
//!
//! Peaks are recorded per job type: service, model and config (`vram_key`).
//! Every second, each running job is charged the reading minus the floor (the
//! lowest reading ever seen: the desktop alone) minus the other running jobs'
//! estimates. Alone, that is its own use; next to others, an upper bound, so a
//! job that used more than it was planned for raises its peak at once, before
//! anything else is let in next to it. Whatever other processes still had
//! loaded counts too: the numbers err high, never low.
//!
//! The estimate is the all-time peak (an app's own guess can only raise it).
//! The queue page shows each peak, can forget one, or override it.

use std::{
    collections::{BTreeMap, HashMap},
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::{
    core::{Admission, Entry},
    state::{lock, Settings, Shared},
};

const INTERVAL: Duration = Duration::from_secs(1);
/// Push the live reading to open queue pages this often.
const PUBLISH_EVERY: Duration = Duration::from_secs(2);
/// Recent run peaks kept per profile (shown on the queue page).
const KEEP_RUNS: usize = 5;
const MIB: u64 = 1024 * 1024;
const FILE: &str = "vram.json";

pub struct Card {
    dir: PathBuf,
}

impl Card {
    /// The configured device directory, else the drm card with the most VRAM.
    pub fn detect(configured: Option<&Path>) -> Option<Card> {
        if let Some(dir) = configured {
            let card = Card {
                dir: dir.to_path_buf(),
            };
            return card.total_mb().map(|_| card);
        }
        let mut best: Option<(u64, Card)> = None;
        for entry in std::fs::read_dir("/sys/class/drm").ok()?.flatten() {
            let name = entry.file_name().to_string_lossy().into_owned();
            // card0, card1 ... (not the connectors card1-DP-1, ...)
            if !name.starts_with("card") || name.contains('-') {
                continue;
            }
            let card = Card {
                dir: entry.path().join("device"),
            };
            if let Some(total) = card.total_mb() {
                if best.as_ref().is_none_or(|(t, _)| total > *t) {
                    best = Some((total, card));
                }
            }
        }
        best.map(|(_, card)| card)
    }

    fn read_mb(&self, file: &str) -> Option<u64> {
        let text = std::fs::read_to_string(self.dir.join(file)).ok()?;
        text.trim().parse::<u64>().ok().map(|b| b / MIB)
    }

    pub fn total_mb(&self) -> Option<u64> {
        self.read_mb("mem_info_vram_total").filter(|t| *t > 0)
    }

    pub fn used_mb(&self) -> Option<u64> {
        self.read_mb("mem_info_vram_used")
    }

    pub fn path(&self) -> String {
        self.dir.display().to_string()
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Profile {
    /// The highest use ever charged to this job type, MiB.
    #[serde(default)]
    pub peak_mb: u64,
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
    #[serde(default)]
    floor_mb: Option<u64>,
    #[serde(default)]
    profiles: BTreeMap<String, Profile>,
}

struct Track {
    key: String,
    peak: u64,
}

pub struct Vram {
    card: Option<Card>,
    path: PathBuf,
    total_mb: u64,
    used_mb: u64,
    floor_mb: Option<u64>,
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
        let card = Card::detect(device);
        match &card {
            Some(c) => tracing::info!("VRAM: {} MiB on {}", c.total_mb().unwrap_or(0), c.path()),
            None => tracing::warn!("VRAM: no readable GPU memory counters; jobs run one at a time"),
        }
        let mut vram = Vram {
            total_mb: card.as_ref().and_then(Card::total_mb).unwrap_or(0),
            used_mb: 0,
            card,
            path,
            floor_mb: saved.floor_mb,
            profiles: saved.profiles,
            tracks: HashMap::new(),
        };
        if let Some(used) = vram.card.as_ref().and_then(Card::used_mb) {
            vram.used_mb = used;
        }
        vram
    }

    pub fn available(&self) -> bool {
        self.card.is_some() && self.total_mb > 0
    }

    fn save(&self) {
        let saved = Saved {
            floor_mb: self.floor_mb,
            profiles: self.profiles.clone(),
        };
        let tmp = self.path.with_extension("json.tmp");
        let body = serde_json::to_vec_pretty(&saved).unwrap_or_default();
        if let Err(e) = std::fs::write(&tmp, body).and_then(|_| std::fs::rename(&tmp, &self.path)) {
            tracing::warn!("could not save VRAM profiles: {e}");
        }
    }

    /// One reading: update the floor, follow the running jobs, and file the
    /// footprint of each job that finished after holding the GPU alone.
    fn sample(&mut self, actives: &[(Entry, Option<u64>)]) {
        let Some(used) = self.card.as_ref().and_then(Card::used_mb) else {
            return;
        };
        if self.record(used, actives) {
            self.save();
        }
    }

    /// `actives`: the running jobs with their current estimates. True when
    /// the floor or a profile changed (save them).
    fn record(&mut self, used: u64, actives: &[(Entry, Option<u64>)]) -> bool {
        self.used_mb = used;
        let mut dirty = false;
        // Below 64 MiB is a reset or a bad read, not a real floor.
        if used >= 64 && self.floor_mb.is_none_or(|f| used < f) {
            self.floor_mb = Some(used);
            dirty = true;
        }
        let floor = self.floor_mb.unwrap_or(0);
        let now = crate::store::iso(chrono::Utc::now());
        for (e, _) in actives {
            let others: u64 = actives
                .iter()
                .filter(|(o, _)| o.entry_id != e.entry_id)
                .filter_map(|(_, est)| *est)
                .sum();
            let charged = used.saturating_sub(floor).saturating_sub(others);
            let track = self.tracks.entry(e.entry_id.clone()).or_insert(Track {
                key: e.vram_key(),
                peak: 0,
            });
            track.peak = track.peak.max(charged);
            // Raise the peak while the job still runs: the next admission
            // decision already plans with it.
            let known = self.profiles.get(&track.key).map_or(0, |p| p.peak_mb);
            if charged > known {
                let profile = self.profiles.entry(track.key.clone()).or_default();
                if profile.peak_mb > 0 {
                    tracing::warn!(
                        "VRAM: {} reached {charged} MiB (peak was {} MiB)",
                        track.key,
                        profile.peak_mb
                    );
                }
                profile.peak_mb = charged;
                profile.updated_at = Some(now.clone());
                dirty = true;
            }
        }
        let finished: Vec<String> = self
            .tracks
            .keys()
            .filter(|id| !actives.iter().any(|(e, _)| &e.entry_id == *id))
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
        dirty
    }

    pub fn forget(&mut self, key: &str) -> bool {
        let removed = self.profiles.remove(key).is_some();
        if removed {
            self.save();
        }
        removed
    }

    fn effective_total(&self, settings: &Settings) -> u64 {
        if settings.vram_total_mb > 0 {
            settings.vram_total_mb
        } else {
            self.total_mb
        }
    }

    pub fn admission(&self, settings: &Settings) -> Admission {
        let estimates: HashMap<String, u64> = self
            .profiles
            .iter()
            .filter_map(|(k, p)| p.learned().map(|mb| (k.clone(), mb)))
            .collect();
        let overrides = settings
            .vram_overrides
            .iter()
            .map(|(k, v)| (k.clone(), *v))
            .collect();
        Admission {
            overrides,
            enabled: settings.concurrent && self.available(),
            total_mb: self.effective_total(settings),
            used_mb: self.used_mb,
            floor_mb: self.floor_mb.unwrap_or(self.used_mb),
            reserve_mb: settings.vram_reserve_mb,
            estimates,
        }
    }
}

impl Shared {
    fn settings_now(&self) -> Settings {
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

    /// The VRAM panel of the queue page.
    pub fn vram_status(&self) -> Value {
        let settings = self.settings_now();
        let (running, admission) = self.read_queue(|q| {
            let running: Vec<Value> = q
                .actives()
                .map(|e| {
                    json!({
                        "entry_id": e.entry_id, "app": e.app, "label": e.label,
                        "key": e.vram_key(), "estimate_mb": q.estimate(e),
                    })
                })
                .collect();
            (running, q.admission().clone())
        });
        let v = lock(&self.vram);
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
                    "recent": p.map(|p| p.recent.clone()).unwrap_or_default(),
                    "runs": p.map(|p| p.runs).unwrap_or(0),
                    "override_mb": settings.vram_overrides.get(k),
                    "updated_at": p.and_then(|p| p.updated_at.clone()),
                })
            })
            .collect();
        let planned: u64 = admission.floor_mb
            + running
                .iter()
                .filter_map(|r| r["estimate_mb"].as_u64())
                .sum::<u64>();
        json!({
            "available": v.available(),
            "device": v.card.as_ref().map(Card::path),
            "detected_total_mb": v.total_mb,
            "total_mb": v.effective_total(&settings),
            "used_mb": v.used_mb,
            "floor_mb": v.floor_mb,
            "reserve_mb": settings.vram_reserve_mb,
            "concurrent": settings.concurrent,
            "active": admission.enabled,
            "free_for_jobs_mb": v.effective_total(&settings)
                .saturating_sub(planned.max(v.used_mb) + settings.vram_reserve_mb),
            "running": running,
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
        let actives: Vec<(Entry, Option<u64>)> =
            shared.read_queue(|q| q.actives().map(|e| (e.clone(), q.estimate(e))).collect());
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
                },
                chrono::Utc::now(),
            )
            .entry;
        Entry {
            entry_id: id.into(),
            ..e
        }
    }

    fn empty() -> Vram {
        Vram {
            card: None,
            path: PathBuf::from("/nonexistent/vram.json"),
            total_mb: 24_000,
            used_mb: 0,
            floor_mb: None,
            profiles: BTreeMap::new(),
            tracks: HashMap::new(),
        }
    }

    #[test]
    fn peaks_are_recorded_alone_and_charged_when_shared() {
        let mut v = empty();
        let mus = entry("m", "Mus");
        let img = entry("i", "Krea");
        v.record(1_000, &[]);
        for used in [3_000, 7_000, 6_500] {
            v.record(used, &[(mus.clone(), None)]);
        }
        assert_eq!(
            v.profiles["Mus"].peak_mb, 6_000,
            "peak 7000 - floor 1000, raised while running"
        );
        assert!(v.record(1_200, &[]), "the finished run is filed");
        assert_eq!(v.profiles["Mus"].recent, [6_000]);
        // Together: Image is planned at 4000 but the card shows 13000.
        v.profiles.entry("Krea".into()).or_default().peak_mb = 4_000;
        for used in [9_000, 13_000] {
            v.record(
                used,
                &[(mus.clone(), Some(6_000)), (img.clone(), Some(4_000))],
            );
        }
        assert_eq!(
            v.profiles["Krea"].peak_mb, 6_000,
            "13000 - 1000 floor - Music's 6000"
        );
        assert_eq!(
            v.profiles["Mus"].peak_mb, 8_000,
            "13000 - 1000 - Image's 4000: an upper bound"
        );
        v.record(1_000, &[]);
        assert_eq!(v.profiles["Krea"].runs, 1);
    }

    #[test]
    fn nothing_measured_files_no_profile() {
        let mut v = empty();
        v.record(1_000, &[]);
        v.record(1_000, &[(entry("t", "TestA"), None)]);
        v.record(1_000, &[]);
        assert!(v.profiles.is_empty());
    }

    #[test]
    fn peaks_never_drop() {
        let mut v = empty();
        let mus = entry("m", "Mus");
        v.record(1_000, &[]);
        v.record(9_000, &[(mus.clone(), None)]);
        v.record(1_000, &[]);
        v.record(3_000, &[(entry("m2", "Mus"), None)]);
        v.record(1_000, &[]);
        assert_eq!(v.profiles["Mus"].learned(), Some(8_000));
        assert_eq!(v.profiles["Mus"].recent, [8_000, 2_000]);
    }

    #[test]
    fn unmeasured_profile_has_no_estimate() {
        assert_eq!(Profile::default().learned(), None);
    }
}
