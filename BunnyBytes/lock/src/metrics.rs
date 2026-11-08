//! Handoff timing, exposed at GET /metrics (Prometheus text format).

use std::{
    fmt::Write,
    sync::atomic::{AtomicU64, Ordering::Relaxed},
    time::Duration,
};

const BUCKETS_MS: [u64; 14] = [
    1, 5, 10, 25, 50, 100, 250, 500, 1_000, 2_500, 5_000, 10_000, 30_000, 120_000,
];

pub struct Histogram {
    name: &'static str,
    help: &'static str,
    buckets: [AtomicU64; BUCKETS_MS.len()],
    count: AtomicU64,
    sum_us: AtomicU64,
}

impl Histogram {
    const fn new(name: &'static str, help: &'static str) -> Self {
        Histogram {
            name,
            help,
            buckets: [const { AtomicU64::new(0) }; BUCKETS_MS.len()],
            count: AtomicU64::new(0),
            sum_us: AtomicU64::new(0),
        }
    }

    pub fn observe(&self, d: Duration) {
        let ms = d.as_millis() as u64;
        for (bound, bucket) in BUCKETS_MS.iter().zip(&self.buckets) {
            if ms <= *bound {
                bucket.fetch_add(1, Relaxed);
            }
        }
        self.count.fetch_add(1, Relaxed);
        self.sum_us.fetch_add(d.as_micros() as u64, Relaxed);
    }

    fn render(&self, out: &mut String) {
        let _ = writeln!(
            out,
            "# HELP {} {}\n# TYPE {} histogram",
            self.name, self.help, self.name
        );
        for (bound, bucket) in BUCKETS_MS.iter().zip(&self.buckets) {
            let _ = writeln!(
                out,
                "{}_bucket{{le=\"{}\"}} {}",
                self.name,
                *bound as f64 / 1000.0,
                bucket.load(Relaxed)
            );
        }
        let count = self.count.load(Relaxed);
        let _ = writeln!(out, "{}_bucket{{le=\"+Inf\"}} {count}", self.name);
        let _ = writeln!(
            out,
            "{}_sum {}",
            self.name,
            self.sum_us.load(Relaxed) as f64 / 1e6
        );
        let _ = writeln!(out, "{}_count {count}", self.name);
    }
}

pub struct Counter {
    name: &'static str,
    help: &'static str,
    value: AtomicU64,
}

impl Counter {
    const fn new(name: &'static str, help: &'static str) -> Self {
        Counter {
            name,
            help,
            value: AtomicU64::new(0),
        }
    }

    pub fn inc(&self) {
        self.value.fetch_add(1, Relaxed);
    }

    fn render(&self, out: &mut String) {
        let _ = writeln!(
            out,
            "# HELP {} {}\n# TYPE {} counter\n{} {}",
            self.name,
            self.help,
            self.name,
            self.name,
            self.value.load(Relaxed)
        );
    }
}

pub struct Metrics {
    /// GPU freed -> next job's app confirmed it started (the dead time between jobs).
    pub handoff_gap: Histogram,
    /// Grant sent -> app ack.
    pub grant_to_ack: Histogram,
    /// Enqueue -> grant.
    pub wait: Histogram,
    /// Grant -> release.
    pub lease: Histogram,
    pub grants: Counter,
    pub expired: Counter,
    pub dead_instances: Counter,
    pub llm_unloads: Counter,
    pub prepares: Counter,
}

pub static METRICS: Metrics = Metrics {
    handoff_gap: Histogram::new(
        "bunnylock_handoff_gap_seconds",
        "GPU freed until the next job's app confirmed it started",
    ),
    grant_to_ack: Histogram::new(
        "bunnylock_grant_to_ack_seconds",
        "Grant sent until the app confirmed it",
    ),
    wait: Histogram::new("bunnylock_wait_seconds", "Time an entry waited for the GPU"),
    lease: Histogram::new("bunnylock_lease_seconds", "Time an entry held the GPU"),
    grants: Counter::new("bunnylock_grants_total", "GPU turns handed out"),
    expired: Counter::new(
        "bunnylock_expired_total",
        "Leases that ran out without a release",
    ),
    dead_instances: Counter::new(
        "bunnylock_dead_instances_total",
        "App processes whose entries were dropped after their stream closed",
    ),
    llm_unloads: Counter::new(
        "bunnylock_llm_guard_unloads_total",
        "LLM unloads by the guard during renders",
    ),
    prepares: Counter::new("bunnylock_prepares_total", "Prepare-early notices sent"),
};

pub fn render() -> String {
    let m = &METRICS;
    let mut out = String::with_capacity(4096);
    for h in [&m.handoff_gap, &m.grant_to_ack, &m.wait, &m.lease] {
        h.render(&mut out);
    }
    for c in [
        &m.grants,
        &m.expired,
        &m.dead_instances,
        &m.llm_unloads,
        &m.prepares,
    ] {
        c.render(&mut out);
    }
    out
}
