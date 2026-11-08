//! The queue rules, with no network or disk access: time is passed in, so the
//! tests are deterministic. Normally one entry is active (holds the GPU); with
//! concurrent jobs on (`Admission`), the entry at the head of the line also
//! starts while others run when its VRAM estimate fits next to theirs. The
//! rest wait in `sort_order` order. Locked waiting entries keep their slot.
//!
//! Every change marks the touched entry ids dirty; the server writes exactly
//! those rows to SQLite after each call (`take_dirty`).

use std::collections::{BTreeSet, HashMap};

use chrono::{DateTime, Duration, Utc};
use serde::Serialize;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum State {
    Waiting,
    Active,
}

impl State {
    pub fn as_str(self) -> &'static str {
        match self {
            State::Waiting => "waiting",
            State::Active => "active",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Entry {
    pub entry_id: String,
    pub app: String,
    pub job_id: String,
    pub token: Option<String>,
    pub label: String,
    pub state: State,
    pub is_continuous: bool,
    pub shutdown_at: Option<String>,
    pub seq: i64,
    pub sort_order: i64,
    pub is_locked: bool,
    pub enqueued_at: DateTime<Utc>,
    pub became_active_at: Option<DateTime<Utc>>,
    pub expires_at: Option<DateTime<Utc>>,
    /// Entries sharing a group show as one card on the queue page.
    pub group: Option<String>,
    /// The app process (lease stream) that owns this entry.
    pub instance: Option<String>,
    /// Some(false): the job never needs the LLM, so the LLM guard may unload it.
    pub uses_llm: Option<bool>,
    /// The owning app confirmed it started the job it was granted.
    pub acked: bool,
    /// The model the job loads, when the app says (VRAM peaks are kept per model).
    pub model: Option<String>,
    /// What else changes its VRAM use (resolution, length, batch...), when the app says.
    pub config: Option<String>,
    /// The app's own VRAM guess in MiB: can only raise the recorded peak.
    pub vram_mb: Option<u64>,
}

impl Entry {
    /// What the VRAM peak of this job is filed under: service, then the model
    /// (or "LLM" for an LLM turn that doesn't name one), then the config.
    pub fn vram_key(&self) -> String {
        let mut key = self.app.clone();
        match self.model.as_deref().filter(|m| !m.is_empty()) {
            Some(m) => {
                key.push_str(" · ");
                key.push_str(m);
            }
            None if self.uses_llm == Some(true) => key.push_str(" · LLM"),
            None => {}
        }
        if let Some(c) = self.config.as_deref().filter(|c| !c.is_empty()) {
            key.push_str(" · ");
            key.push_str(c);
        }
        key
    }
}

/// When jobs may share the GPU: set by the server from the VRAM monitor and
/// the settings. Disabled (the default) means one job at a time.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Admission {
    pub enabled: bool,
    /// Usable VRAM of the card, MiB.
    pub total_mb: u64,
    /// The live reading, MiB (everything: desktop, cached models, running jobs).
    pub used_mb: u64,
    /// What the card uses with no jobs at all (desktop), MiB.
    pub floor_mb: u64,
    /// Always kept free, MiB.
    pub reserve_mb: u64,
    /// vram_key -> recorded peak, MiB.
    pub estimates: HashMap<String, u64>,
    /// vram_key -> MiB set by hand on the queue page (replaces the peak).
    pub overrides: HashMap<String, u64>,
}

pub struct NewEntry {
    pub app: String,
    pub job_id: String,
    pub label: String,
    pub is_continuous: bool,
    pub priority: bool,
    pub group: Option<String>,
    pub instance: Option<String>,
    pub uses_llm: Option<bool>,
    pub model: Option<String>,
    pub config: Option<String>,
    pub vram_mb: Option<u64>,
}

#[derive(Debug, PartialEq, Eq)]
pub enum ReorderError {
    QueueChanged,
    LockedPosition,
}

impl ReorderError {
    pub fn code(&self) -> &'static str {
        match self {
            ReorderError::QueueChanged => "queue_changed",
            ReorderError::LockedPosition => "locked_position",
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum DequeueOutcome {
    Removed,
    NotFound,
    Active,
}

pub struct Enqueued {
    pub entry: Entry,
    /// Waiting position (1-based) when it did not become active at once.
    pub position: Option<usize>,
    /// Whatever became active because of this call (usually the new entry).
    pub promoted: Vec<Entry>,
}

pub struct Queue {
    entries: Vec<Entry>,
    next_seq: i64,
    ttl: Duration,
    dirty: BTreeSet<String>,
    admission: Admission,
}

pub fn new_token() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}

impl Queue {
    /// Restore from rows loaded at startup. A restored active holder is assumed
    /// to have started its job (it was granted before the restart).
    pub fn restore(mut entries: Vec<Entry>, ttl: Duration) -> Self {
        for e in &mut entries {
            e.acked = e.state == State::Active;
        }
        let next_seq = entries.iter().map(|e| e.seq).max().unwrap_or(0) + 1;
        let mut q = Queue {
            entries,
            next_seq,
            ttl,
            dirty: BTreeSet::new(),
            admission: Admission::default(),
        };
        q.sort();
        q
    }

    pub fn take_dirty(&mut self) -> Vec<String> {
        std::mem::take(&mut self.dirty).into_iter().collect()
    }

    fn touch(&mut self, id: &str) {
        self.dirty.insert(id.to_owned());
    }

    /// Active first, then waiting by (sort_order, seq).
    fn sort(&mut self) {
        self.entries
            .sort_by_key(|e| (e.state != State::Active, e.sort_order, e.seq));
    }

    pub fn entries(&self) -> &[Entry] {
        &self.entries
    }

    pub fn get(&self, entry_id: &str) -> Option<&Entry> {
        self.entries.iter().find(|e| e.entry_id == entry_id)
    }

    /// Every entry holding the GPU (one, or several with concurrent jobs on).
    pub fn actives(&self) -> impl Iterator<Item = &Entry> {
        self.entries.iter().filter(|e| e.state == State::Active)
    }

    pub fn has_active(&self) -> bool {
        self.actives().next().is_some()
    }

    /// The active entry holding `token`.
    pub fn holder(&self, token: &str) -> Option<&Entry> {
        self.actives().find(|e| e.token.as_deref() == Some(token))
    }

    pub fn admission(&self) -> &Admission {
        &self.admission
    }

    pub fn set_admission(&mut self, admission: Admission) {
        self.admission = admission;
    }

    /// The VRAM this entry is expected to need, MiB: the user's override, else
    /// the recorded peak for its service/model/config, raised (never lowered)
    /// by the app's own guess. None until one of them exists.
    pub fn estimate(&self, e: &Entry) -> Option<u64> {
        let key = e.vram_key();
        if let Some(mb) = self.admission.overrides.get(&key) {
            return Some(*mb);
        }
        let peak = self.admission.estimates.get(&key).copied();
        match (peak, e.vram_mb) {
            (Some(p), Some(h)) => Some(p.max(h)),
            (p, h) => p.or(h),
        }
    }

    /// Can `candidate` start now, next to the jobs already running?
    /// Every job's estimate must be known; at most one job per app and one LLM
    /// job (they share llama-swap) run at once; and the estimates plus the
    /// reserve must fit the card, also by the live reading.
    pub fn fits_alongside(&self, candidate: &Entry) -> bool {
        let a = &self.admission;
        if !a.enabled || a.total_mb == 0 || candidate.uses_llm.is_none() {
            return false;
        }
        let Some(need) = self.estimate(candidate) else {
            return false;
        };
        let mut planned = a.floor_mb;
        for e in self.actives() {
            if e.app == candidate.app
                || e.uses_llm.is_none()
                || (e.uses_llm == Some(true) && candidate.uses_llm == Some(true))
            {
                return false;
            }
            let Some(est) = self.estimate(e) else {
                return false;
            };
            planned += est;
        }
        // The live reading covers what estimates miss (models other apps left
        // loaded, a job using more than measured); the plan covers jobs granted
        // a moment ago that haven't loaded yet.
        let occupied = planned.max(a.used_mb);
        occupied + need + a.reserve_mb <= a.total_mb
    }

    pub fn first_waiting(&self) -> Option<&Entry> {
        self.entries.iter().find(|e| e.state == State::Waiting)
    }

    pub fn position(&self, entry_id: &str) -> Option<usize> {
        self.entries
            .iter()
            .filter(|e| e.state == State::Waiting)
            .position(|e| e.entry_id == entry_id)
            .map(|i| i + 1)
    }

    fn remove(&mut self, entry_id: &str) -> Option<Entry> {
        let index = self.entries.iter().position(|e| e.entry_id == entry_id)?;
        self.touch(entry_id);
        Some(self.entries.remove(index))
    }

    /// Drop active holders whose lease ran out (crashed without releasing
    /// and stopped heartbeating).
    fn reap_expired(&mut self, now: DateTime<Utc>) -> Vec<Entry> {
        let expired: Vec<String> = self
            .actives()
            .filter(|e| e.expires_at.is_none_or(|t| t <= now))
            .map(|e| e.entry_id.clone())
            .collect();
        expired.iter().filter_map(|id| self.remove(id)).collect()
    }

    fn activate(&mut self, id: &str, now: DateTime<Utc>) -> Option<Entry> {
        let ttl = self.ttl;
        let entry = self.entries.iter_mut().find(|e| e.entry_id == id)?;
        entry.state = State::Active;
        entry.token = Some(new_token());
        entry.became_active_at = Some(now);
        entry.expires_at = Some(now + ttl);
        entry.acked = false;
        let promoted = entry.clone();
        self.touch(id);
        self.sort();
        Some(promoted)
    }

    /// Start the head of the line: always when the GPU is free, and (concurrent
    /// jobs on) as long as it fits next to what runs. Strictly in order: a job
    /// that doesn't fit waits, and nothing behind it overtakes.
    pub fn fill(&mut self, now: DateTime<Utc>) -> Vec<Entry> {
        let mut promoted = Vec::new();
        while let Some(head) = self.first_waiting() {
            if self.has_active() && !self.fits_alongside(head) {
                break;
            }
            let id = head.entry_id.clone();
            match self.activate(&id, now) {
                Some(e) => promoted.push(e),
                None => break,
            }
        }
        promoted
    }

    /// Periodic self-heal: expire dead holders and hand the GPU on.
    /// Returns (expired holders, newly active entries).
    pub fn reap(&mut self, now: DateTime<Utc>) -> (Vec<Entry>, Vec<Entry>) {
        let expired = self.reap_expired(now);
        let promoted = self.fill(now);
        (expired, promoted)
    }

    /// Insert, promote and (if asked) move to the front in one step, so two
    /// simultaneous enqueues into an empty queue can never both win the slot.
    pub fn enqueue(&mut self, new: NewEntry, now: DateTime<Utc>) -> Enqueued {
        self.reap_expired(now);
        let sort_order = self.entries.iter().map(|e| e.sort_order).max().unwrap_or(0) + 1;
        let entry_id = new_token();
        let seq = self.next_seq;
        self.next_seq += 1;
        self.entries.push(Entry {
            entry_id: entry_id.clone(),
            app: new.app,
            job_id: new.job_id,
            token: None,
            label: new.label,
            state: State::Waiting,
            is_continuous: new.is_continuous,
            shutdown_at: None,
            seq,
            sort_order,
            is_locked: false,
            enqueued_at: now,
            became_active_at: None,
            expires_at: None,
            group: new.group.filter(|g| !g.is_empty()),
            instance: new.instance,
            uses_llm: new.uses_llm,
            acked: false,
            model: new.model.filter(|m| !m.is_empty()),
            config: new.config.filter(|c| !c.is_empty()),
            vram_mb: new.vram_mb.filter(|v| *v > 0),
        });
        self.touch(&entry_id);
        self.sort();
        let mut promoted = self.fill(now);
        let self_promoted = promoted.iter().any(|p| p.entry_id == entry_id);
        if new.priority && !self_promoted {
            self.prioritize(&entry_id);
            // At the front now: it may fit next to what runs.
            promoted.extend(self.fill(now));
        }
        let position = self.position(&entry_id);
        let entry = self
            .get(&entry_id)
            .cloned()
            .unwrap_or_else(|| unreachable_entry(&entry_id));
        Enqueued {
            entry,
            position,
            promoted,
        }
    }

    /// Extend the active holder's lease. A holder that moved to a new process
    /// (`instance`) is re-bound to it, so the restarted app keeps its turn.
    pub fn heartbeat(
        &mut self,
        token: &str,
        instance: Option<&str>,
        now: DateTime<Utc>,
    ) -> Option<Entry> {
        self.reap_expired(now);
        let ttl = self.ttl;
        let entry = self
            .entries
            .iter_mut()
            .find(|e| e.state == State::Active && e.token.as_deref() == Some(token))?;
        entry.expires_at = Some(now + ttl);
        if let Some(instance) = instance {
            entry.instance = Some(instance.to_owned());
        }
        let entry = entry.clone();
        self.touch(&entry.entry_id);
        Some(entry)
    }

    pub fn ack(&mut self, token: &str) -> Option<Entry> {
        let entry = self
            .entries
            .iter_mut()
            .find(|e| e.state == State::Active && e.token.as_deref() == Some(token))?;
        entry.acked = true;
        Some(entry.clone())
    }

    /// Release the active holder matching `token`. Idempotent: with nothing
    /// active it counts as released (and promotes whoever waits). Returns
    /// (released, newly active entries).
    pub fn release(&mut self, token: &str, now: DateTime<Utc>) -> (bool, Vec<Entry>) {
        self.reap_expired(now);
        if !self.has_active() {
            return (true, self.fill(now));
        }
        let Some(id) = self.holder(token).map(|e| e.entry_id.clone()) else {
            return (false, Vec::new());
        };
        self.remove(&id);
        (true, self.fill(now))
    }

    /// Remove a waiting entry. An active one is left to its app's release.
    pub fn dequeue(&mut self, entry_id: &str) -> DequeueOutcome {
        match self.get(entry_id).map(|e| e.state) {
            None => DequeueOutcome::NotFound,
            Some(State::Active) => DequeueOutcome::Active,
            Some(State::Waiting) => {
                self.remove(entry_id);
                DequeueOutcome::Removed
            }
        }
    }

    /// Remove an entry nobody will ever release (its app is gone or no longer
    /// knows the job), active or not. Returns (removed, newly active entry).
    pub fn drop_entry(
        &mut self,
        entry_id: &str,
        now: DateTime<Utc>,
    ) -> (Option<Entry>, Vec<Entry>) {
        let removed = self.remove(entry_id);
        let promoted = self.fill(now);
        (removed, promoted)
    }

    /// Remove every entry of a dead app process. Returns (removed, newly active).
    pub fn drop_instance(
        &mut self,
        instance: &str,
        now: DateTime<Utc>,
    ) -> (Vec<Entry>, Vec<Entry>) {
        let ids: Vec<String> = self
            .entries
            .iter()
            .filter(|e| e.instance.as_deref() == Some(instance))
            .map(|e| e.entry_id.clone())
            .collect();
        let removed = ids.iter().filter_map(|id| self.remove(id)).collect();
        let promoted = self.fill(now);
        (removed, promoted)
    }

    /// Hand every entry of a dead process to its replacement (the same app
    /// restarted). Returns the moved entries.
    pub fn rebind_instance(&mut self, old: &str, new: &str) -> Vec<Entry> {
        let mut moved = Vec::new();
        for e in self
            .entries
            .iter_mut()
            .filter(|e| e.instance.as_deref() == Some(old))
        {
            e.instance = Some(new.to_owned());
            moved.push(e.clone());
        }
        for e in &moved {
            self.dirty.insert(e.entry_id.clone());
        }
        moved
    }

    pub fn set_shutdown_at(&mut self, entry_id: &str, shutdown_at: Option<String>) -> bool {
        let Some(entry) = self.entries.iter_mut().find(|e| e.entry_id == entry_id) else {
            return false;
        };
        entry.shutdown_at = shutdown_at;
        self.touch(entry_id);
        true
    }

    pub fn set_locked(&mut self, entry_id: &str, is_locked: bool) -> bool {
        let Some(entry) = self.entries.iter_mut().find(|e| e.entry_id == entry_id) else {
            return false;
        };
        entry.is_locked = is_locked;
        self.touch(entry_id);
        true
    }

    /// Change the waiting order. `entry_ids` must be exactly the waiting
    /// entries; locked ones must stay where they are.
    pub fn reorder(&mut self, entry_ids: &[String]) -> Result<(), ReorderError> {
        let current: Vec<(String, bool)> = self
            .entries
            .iter()
            .filter(|e| e.state == State::Waiting)
            .map(|e| (e.entry_id.clone(), e.is_locked))
            .collect();
        let mut wanted: Vec<&String> = entry_ids.iter().collect();
        let mut have: Vec<&String> = current.iter().map(|(id, _)| id).collect();
        wanted.sort();
        wanted.dedup();
        have.sort();
        if entry_ids.len() != current.len() || wanted != have {
            return Err(ReorderError::QueueChanged);
        }
        for (index, (id, locked)) in current.iter().enumerate() {
            if *locked && &entry_ids[index] != id {
                return Err(ReorderError::LockedPosition);
            }
        }
        // Compact ranks: later enqueues always land after the reordered list.
        for (rank, id) in entry_ids.iter().enumerate() {
            if let Some(entry) = self.entries.iter_mut().find(|e| &e.entry_id == id) {
                entry.sort_order = rank as i64 + 1;
            }
            self.touch(id);
        }
        self.sort();
        Ok(())
    }

    /// Move a waiting entry to the front; locked entries keep their slots.
    pub fn prioritize(&mut self, entry_id: &str) -> bool {
        let waiting: Vec<(String, bool)> = self
            .entries
            .iter()
            .filter(|e| e.state == State::Waiting)
            .map(|e| (e.entry_id.clone(), e.is_locked))
            .collect();
        let Some((_, locked)) = waiting.iter().find(|(id, _)| id == entry_id) else {
            return false;
        };
        let mut movable: Vec<String> = waiting
            .iter()
            .filter(|(_, l)| !l)
            .map(|(id, _)| id.clone())
            .collect();
        if !locked {
            movable.retain(|id| id != entry_id);
            movable.insert(0, entry_id.to_owned());
        }
        let mut movable = movable.into_iter();
        let order: Vec<String> = waiting
            .iter()
            .map(|(id, l)| {
                if *l {
                    id.clone()
                } else {
                    movable.next().unwrap_or_default()
                }
            })
            .collect();
        self.reorder(&order).is_ok()
    }

    #[cfg(test)]
    pub fn waiting_count(&self) -> usize {
        self.entries
            .iter()
            .filter(|e| e.state == State::Waiting)
            .count()
    }
}

#[cold]
fn unreachable_entry(entry_id: &str) -> Entry {
    // An entry inserted under the same &mut borrow cannot vanish; keep the
    // server alive with an inert value instead of panicking if it ever did.
    tracing::error!(entry_id, "freshly inserted queue entry missing");
    Entry {
        entry_id: entry_id.to_owned(),
        app: String::new(),
        job_id: String::new(),
        token: None,
        label: String::new(),
        state: State::Waiting,
        is_continuous: false,
        shutdown_at: None,
        seq: 0,
        sort_order: 0,
        is_locked: false,
        enqueued_at: Utc::now(),
        became_active_at: None,
        expires_at: None,
        group: None,
        instance: None,
        uses_llm: None,
        acked: false,
        model: None,
        config: None,
        vram_mb: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    fn t0() -> DateTime<Utc> {
        DateTime::from_timestamp(1_800_000_000, 0).unwrap_or_default()
    }

    fn new(app: &str, job: &str) -> NewEntry {
        NewEntry {
            app: app.into(),
            job_id: job.into(),
            label: job.into(),
            is_continuous: false,
            priority: false,
            group: None,
            instance: Some(format!("{app}-1")),
            uses_llm: Some(false),
            model: None,
            config: None,
            vram_mb: None,
        }
    }

    fn llm(app: &str, job: &str) -> NewEntry {
        NewEntry {
            uses_llm: Some(true),
            ..new(app, job)
        }
    }

    /// 24 GiB card, 1 GiB desktop, 1 GiB reserve.
    fn admission(estimates: &[(&str, u64)]) -> Admission {
        Admission {
            enabled: true,
            total_mb: 24_576,
            used_mb: 1_024,
            floor_mb: 1_024,
            reserve_mb: 1_024,
            estimates: estimates
                .iter()
                .map(|(k, v)| ((*k).to_owned(), *v))
                .collect(),
            overrides: HashMap::new(),
        }
    }

    fn active_jobs(q: &Queue) -> Vec<String> {
        q.actives().map(|e| e.job_id.clone()).collect()
    }

    fn queue() -> Queue {
        Queue::restore(Vec::new(), Duration::seconds(90))
    }

    #[test]
    fn first_enqueue_is_active_at_once() {
        let mut q = queue();
        let a = q.enqueue(new("Krea", "a"), t0());
        assert_eq!(a.entry.state, State::Active);
        assert_eq!(
            a.promoted.first().map(|p| p.entry_id.clone()),
            Some(a.entry.entry_id.clone())
        );
        let b = q.enqueue(new("Mus", "b"), t0());
        assert_eq!(b.entry.state, State::Waiting);
        assert_eq!(b.position, Some(1));
        assert!(b.promoted.is_empty());
    }

    #[test]
    fn release_promotes_next_and_wrong_token_is_refused() {
        let mut q = queue();
        let a = q.enqueue(new("Krea", "a"), t0()).entry;
        let b = q.enqueue(new("Mus", "b"), t0()).entry;
        assert_eq!(q.release("nope", t0()), (false, Vec::new()));
        let (released, promoted) = q.release(a.token.as_deref().unwrap_or_default(), t0());
        assert!(released);
        assert_eq!(
            promoted.first().map(|p| p.entry_id.clone()),
            Some(b.entry_id)
        );
        // Idempotent: the old token no longer matches the new holder.
        assert!(!q.release(a.token.as_deref().unwrap_or_default(), t0()).0);
    }

    #[test]
    fn expired_lease_is_reaped_and_heartbeat_extends() {
        let mut q = queue();
        let a = q.enqueue(new("Krea", "a"), t0()).entry;
        q.enqueue(new("Mus", "b"), t0());
        let token = a.token.clone().unwrap_or_default();
        assert!(q
            .heartbeat(&token, None, t0() + Duration::seconds(80))
            .is_some());
        let (expired, promoted) = q.reap(t0() + Duration::seconds(100));
        assert!(expired.is_empty(), "heartbeat at 80 s keeps it until 170 s");
        assert!(promoted.is_empty());
        let (expired, promoted) = q.reap(t0() + Duration::seconds(171));
        assert_eq!(expired.first().map(|e| e.job_id.clone()), Some("a".into()));
        assert_eq!(promoted.first().map(|e| e.job_id.clone()), Some("b".into()));
    }

    #[test]
    fn heartbeat_rebinds_instance() {
        let mut q = queue();
        let a = q.enqueue(new("Krea", "a"), t0()).entry;
        let e = q.heartbeat(a.token.as_deref().unwrap_or_default(), Some("Krea-2"), t0());
        assert_eq!(e.and_then(|e| e.instance), Some("Krea-2".into()));
    }

    #[test]
    fn priority_skips_unlocked_but_not_locked() {
        let mut q = queue();
        q.enqueue(new("Krea", "active"), t0());
        let w1 = q.enqueue(new("Krea", "w1"), t0()).entry;
        q.enqueue(new("Krea", "w2"), t0());
        assert!(q.set_locked(&w1.entry_id, true));
        let mut agent = new("Agent", "turn");
        agent.priority = true;
        let p = q.enqueue(agent, t0());
        assert_eq!(p.position, Some(2), "w1 is locked in slot 1");
        let order: Vec<String> = q
            .entries()
            .iter()
            .filter(|e| e.state == State::Waiting)
            .map(|e| e.job_id.clone())
            .collect();
        assert_eq!(order, ["w1", "turn", "w2"]);
    }

    #[test]
    fn reorder_rejects_stale_and_locked_moves() {
        let mut q = queue();
        q.enqueue(new("Krea", "active"), t0());
        let a = q.enqueue(new("Krea", "a"), t0()).entry.entry_id;
        let b = q.enqueue(new("Krea", "b"), t0()).entry.entry_id;
        assert_eq!(
            q.reorder(std::slice::from_ref(&a)),
            Err(ReorderError::QueueChanged)
        );
        assert_eq!(
            q.reorder(&[a.clone(), a.clone()]),
            Err(ReorderError::QueueChanged)
        );
        q.set_locked(&a, true);
        assert_eq!(
            q.reorder(&[b.clone(), a.clone()]),
            Err(ReorderError::LockedPosition)
        );
        q.set_locked(&a, false);
        assert_eq!(q.reorder(&[b.clone(), a.clone()]), Ok(()));
        assert_eq!(q.first_waiting().map(|e| e.entry_id.clone()), Some(b));
    }

    #[test]
    fn dequeue_leaves_active_alone() {
        let mut q = queue();
        let a = q.enqueue(new("Krea", "a"), t0()).entry.entry_id;
        let b = q.enqueue(new("Krea", "b"), t0()).entry.entry_id;
        assert_eq!(q.dequeue(&a), DequeueOutcome::Active);
        assert_eq!(q.dequeue(&b), DequeueOutcome::Removed);
        assert_eq!(q.dequeue(&b), DequeueOutcome::NotFound);
    }

    #[test]
    fn drop_instance_frees_slot_and_promotes() {
        let mut q = queue();
        q.enqueue(new("Krea", "a"), t0());
        q.enqueue(new("Krea", "b"), t0());
        let c = q.enqueue(new("Mus", "c"), t0()).entry;
        let (removed, promoted) = q.drop_instance("Krea-1", t0());
        assert_eq!(removed.len(), 2);
        assert_eq!(
            promoted.first().map(|p| p.entry_id.clone()),
            Some(c.entry_id)
        );
    }

    #[test]
    fn rebind_moves_entries_to_the_new_process() {
        let mut q = queue();
        q.enqueue(new("Krea", "a"), t0());
        q.enqueue(new("Krea", "b"), t0());
        q.enqueue(new("Mus", "c"), t0());
        let moved = q.rebind_instance("Krea-1", "Krea-2");
        assert_eq!(moved.len(), 2);
        assert!(q
            .entries()
            .iter()
            .filter(|e| e.app == "Krea")
            .all(|e| e.instance.as_deref() == Some("Krea-2")));
        assert_eq!(active_jobs(&q), ["a"], "the holder keeps the GPU");
    }

    #[test]
    fn restore_keeps_order_and_marks_active_started() {
        let mut q = queue();
        q.enqueue(new("Krea", "a"), t0());
        q.enqueue(new("Krea", "b"), t0());
        let rows = q.entries().to_vec();
        let r = Queue::restore(rows.into_iter().rev().collect(), Duration::seconds(90));
        assert_eq!(r.entries()[0].job_id, "a");
        assert!(r.entries()[0].acked);
        assert_eq!(r.entries()[1].job_id, "b");
    }

    #[test]
    fn dirty_tracks_changed_rows() {
        let mut q = queue();
        let a = q.enqueue(new("Krea", "a"), t0()).entry;
        assert_eq!(q.take_dirty(), vec![a.entry_id.clone()]);
        assert!(q.take_dirty().is_empty());
        q.release(a.token.as_deref().unwrap_or_default(), t0());
        assert_eq!(q.take_dirty(), vec![a.entry_id]);
    }

    #[test]
    fn concurrent_jobs_start_together_when_they_fit() {
        let mut q = queue();
        // 1 GiB desktop + Music 6 + Image 9 + a 9B agent 5 + 1 GiB reserve = 22 GiB of 24.
        q.set_admission(admission(&[
            ("Mus", 6_144),
            ("Krea", 9_216),
            ("Agent · LLM", 5_120),
        ]));
        q.enqueue(new("Mus", "song"), t0());
        let image = q.enqueue(new("Krea", "image"), t0());
        assert_eq!(image.entry.state, State::Active, "6 + 9 + 1 + 1 GiB fits");
        let agent = q.enqueue(llm("Agent", "turn"), t0());
        assert_eq!(agent.entry.state, State::Active, "+5 GiB = 22 GiB fits");
        assert_eq!(active_jobs(&q), ["song", "image", "turn"]);
        let video = q.enqueue(new("Vid", "clip"), t0());
        assert_eq!(
            video.entry.state,
            State::Waiting,
            "no estimate: only runs alone"
        );
    }

    #[test]
    fn llm_image_music_and_whisper_all_run_together_when_they_fit() {
        let mut q = queue();
        // 1 GiB desktop + LLM 7 + Image 8 + Music 5 + whisper 1.5 + 1 GiB reserve = 23.5 of 24 GiB.
        q.set_admission(admission(&[
            ("Agent · qwen3-9b", 7_168),
            ("Krea · Flux · 1024x1024", 8_192),
            ("Mus · yue2 · up to 120s", 5_120),
            ("Voice · transcribe", 1_536),
        ]));
        let mut agent = llm("Agent", "turn");
        agent.model = Some("qwen3-9b".into());
        let mut image = new("Krea", "image");
        image.model = Some("Flux".into());
        image.config = Some("1024x1024".into());
        let mut music = new("Mus", "song");
        music.model = Some("yue2".into());
        music.config = Some("up to 120s".into());
        let mut whisper = new("Voice", "whisper");
        whisper.model = Some("transcribe".into());
        for job in [agent, image, music, whisper] {
            assert_eq!(q.enqueue(job, t0()).entry.state, State::Active);
        }
        assert_eq!(active_jobs(&q), ["turn", "image", "song", "whisper"]);
        // A bigger image doesn't fit next to them: it waits for room.
        let mut big = new("Krea", "big");
        big.model = Some("Flux".into());
        big.config = Some("1024x1024".into());
        assert_eq!(q.enqueue(big, t0()).entry.state, State::Waiting);
    }

    #[test]
    fn concurrent_respects_budget_live_reading_and_order() {
        let mut q = queue();
        q.set_admission(admission(&[
            ("Mus", 6_144),
            ("Krea", 17_408),
            ("Trainer", 1_024),
        ]));
        q.enqueue(new("Mus", "song"), t0());
        assert_eq!(
            q.enqueue(new("Krea", "big"), t0()).entry.state,
            State::Waiting
        );
        // Head-of-line: the small job behind the big one waits too.
        assert_eq!(
            q.enqueue(new("Trainer", "small"), t0()).entry.state,
            State::Waiting
        );
        // Something outside the queue fills the card: the live reading counts.
        let mut a = admission(&[("Mus", 6_144), ("Krea", 4_096)]);
        a.used_mb = 20_000;
        q.set_admission(a);
        assert!(q.fill(t0()).is_empty());
        let mut a = admission(&[("Mus", 6_144), ("Krea", 4_096), ("Trainer", 1_024)]);
        a.used_mb = 3_000;
        q.set_admission(a);
        let started: Vec<String> = q.fill(t0()).into_iter().map(|e| e.job_id).collect();
        assert_eq!(started, ["big", "small"]);
    }

    #[test]
    fn one_llm_job_and_one_job_per_app() {
        let mut q = queue();
        q.set_admission(admission(&[
            ("Agent · LLM", 2_048),
            ("Framework · LLM", 2_048),
            ("Krea", 2_048),
        ]));
        q.enqueue(llm("Agent", "turn"), t0());
        assert_eq!(
            q.enqueue(llm("Framework", "hermes"), t0()).entry.state,
            State::Waiting
        );
        let mut q = queue();
        q.set_admission(admission(&[("Krea", 2_048)]));
        q.enqueue(new("Krea", "a"), t0());
        assert_eq!(
            q.enqueue(new("Krea", "b"), t0()).entry.state,
            State::Waiting
        );
    }

    #[test]
    fn disabled_admission_is_one_at_a_time_and_release_promotes_several() {
        let mut q = queue();
        let a = q.enqueue(new("Mus", "a"), t0()).entry;
        q.enqueue(new("Krea", "b"), t0());
        q.enqueue(new("Trainer", "c"), t0());
        assert_eq!(active_jobs(&q), ["a"]);
        q.set_admission(admission(&[
            ("Mus", 2_048),
            ("Krea", 2_048),
            ("Trainer", 2_048),
        ]));
        let (released, promoted) = q.release(a.token.as_deref().unwrap_or_default(), t0());
        assert!(released);
        assert_eq!(promoted.len(), 2, "b and c both start");
        assert!(!q.release("not-a-holder", t0()).0);
    }

    #[test]
    fn model_and_hint_feed_the_estimate() {
        let mut q = queue();
        q.set_admission(admission(&[]));
        let mut agent = llm("Agent", "turn");
        agent.model = Some("qwen3-9b".into());
        agent.vram_mb = Some(7_000);
        agent.config = Some("ctx 32k".into());
        let e = q.enqueue(agent, t0()).entry;
        assert_eq!(e.vram_key(), "Agent · qwen3-9b · ctx 32k");
        assert_eq!(q.estimate(&e), Some(7_000), "only the app's guess so far");
        q.set_admission(admission(&[("Agent · qwen3-9b · ctx 32k", 8_000)]));
        assert_eq!(q.estimate(&e), Some(8_000), "the recorded peak is higher");
        q.set_admission(admission(&[("Agent · qwen3-9b · ctx 32k", 6_000)]));
        assert_eq!(
            q.estimate(&e),
            Some(7_000),
            "a guess can raise the peak, never lower it"
        );
        let mut a = admission(&[("Agent · qwen3-9b · ctx 32k", 6_000)]);
        a.overrides
            .insert("Agent · qwen3-9b · ctx 32k".into(), 5_000);
        q.set_admission(a);
        assert_eq!(q.estimate(&e), Some(5_000), "a hand-set value wins");
    }

    #[derive(Debug, Clone)]
    enum Op {
        Enqueue { app: u8, priority: bool },
        ReleaseActive,
        Dequeue(usize),
        Lock(usize, bool),
        Prioritize(usize),
        Reverse,
        Advance(i64),
        DropInstance(u8),
    }

    fn op() -> impl Strategy<Value = Op> {
        prop_oneof![
            (0u8..3, any::<bool>()).prop_map(|(app, priority)| Op::Enqueue { app, priority }),
            Just(Op::ReleaseActive),
            (0usize..8).prop_map(Op::Dequeue),
            (0usize..8, any::<bool>()).prop_map(|(i, l)| Op::Lock(i, l)),
            (0usize..8).prop_map(Op::Prioritize),
            Just(Op::Reverse),
            (0i64..120).prop_map(Op::Advance),
            (0u8..3).prop_map(Op::DropInstance),
        ]
    }

    proptest! {
        /// Whatever happens: at most one holder, nothing waits while the GPU is
        /// free, locked waiting entries never move, and every entry is either
        /// still queued or was explicitly removed.
        #[test]
        fn invariants_hold(ops in proptest::collection::vec(op(), 1..60), concurrent in any::<bool>()) {
            let mut q = queue();
            if concurrent {
                q.set_admission(admission(&[("Krea", 9_000), ("Mus", 6_000), ("Agent · LLM", 7_000)]));
            }
            let mut now = t0();
            for op in ops {
                let waiting_before: Vec<(String, bool)> = q.entries().iter()
                    .filter(|e| e.state == State::Waiting).map(|e| (e.entry_id.clone(), e.is_locked)).collect();
                let pick = |i: usize| waiting_before.get(i % waiting_before.len().max(1)).map(|(id, _)| id.clone());
                let mut reordering = false;
                match op {
                    Op::Enqueue { app, priority } => {
                        let name = ["Krea", "Mus", "Agent"][app as usize];
                        let mut n = if name == "Agent" { llm(name, "j") } else { new(name, "j") };
                        n.priority = priority;
                        q.enqueue(n, now);
                        reordering = priority;
                    }
                    Op::ReleaseActive => {
                        let token = q.actives().next().and_then(|a| a.token.clone());
                        if let Some(t) = token { q.release(&t, now); }
                    }
                    Op::Dequeue(i) => if let Some(id) = pick(i) { q.dequeue(&id); },
                    Op::Lock(i, l) => if let Some(id) = pick(i) { q.set_locked(&id, l); },
                    Op::Prioritize(i) => if let Some(id) = pick(i) { q.prioritize(&id); reordering = true; },
                    Op::Reverse => {
                        let mut ids: Vec<String> = waiting_before.iter().map(|(id, _)| id.clone()).collect();
                        ids.reverse();
                        let _ = q.reorder(&ids);
                        reordering = true;
                    }
                    Op::Advance(s) => { now += Duration::seconds(s); q.reap(now); }
                    Op::DropInstance(app) => { q.drop_instance(&format!("{}-1", ["Krea", "Mus", "Agent"][app as usize]), now); }
                }
                let active = q.entries().iter().filter(|e| e.state == State::Active).count();
                if concurrent {
                    let apps: BTreeSet<&str> = q.actives().map(|e| e.app.as_str()).collect();
                    prop_assert_eq!(apps.len(), active, "one job per app");
                    prop_assert!(q.actives().filter(|e| e.uses_llm == Some(true)).count() <= 1);
                    if active > 1 {
                        let a = q.admission();
                        let planned: u64 = q.actives().filter_map(|e| q.estimate(e)).sum();
                        prop_assert!(a.floor_mb + planned + a.reserve_mb <= a.total_mb, "over budget");
                    }
                } else {
                    prop_assert!(active <= 1);
                }
                if active == 0 { prop_assert_eq!(q.waiting_count(), 0, "waiting while the GPU is free"); }
                prop_assert_eq!(q.entries().first().map(|e| e.state == State::Active).unwrap_or(false), active >= 1);
                if reordering {
                    let after: Vec<String> = q.entries().iter().filter(|e| e.state == State::Waiting).map(|e| e.entry_id.clone()).collect();
                    for (index, (id, locked)) in waiting_before.iter().enumerate() {
                        let still_waiting = q.get(id).is_some_and(|e| e.state == State::Waiting);
                        if *locked && still_waiting && after.len() > waiting_before.len() - 1 {
                            // A locked entry keeps its slot (an enqueue only appends after
                            // it); entries ahead of it that started running leave the line.
                            let started = waiting_before[..index].iter()
                                .filter(|(other, _)| q.get(other).is_some_and(|e| e.state == State::Active))
                                .count();
                            prop_assert_eq!(after.get(index - started), Some(id));
                        }
                    }
                }
            }
        }
    }
}
