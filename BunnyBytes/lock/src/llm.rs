//! The LLM side of the GPU: the guard that keeps the local model unloaded
//! while a render holds the GPU right after an LLM turn, and the display-only
//! "Remote agent" card.

use std::{
    sync::Arc,
    time::{Duration, Instant},
};

use serde_json::{json, Value};

use crate::{
    metrics::METRICS,
    state::{lock, Shared},
};

const GUARD_INTERVAL: Duration = Duration::from_secs(1);
const HERMES_INTERVAL: Duration = Duration::from_secs(10);
/// Nothing reloaded the model for this long: the guard ends.
const QUIET: Duration = Duration::from_secs(8);

impl Shared {
    fn llama(&self, path: &str) -> reqwest::RequestBuilder {
        let request = self.http.get(format!("{}{path}", self.cfg.llama_url));
        if self.cfg.llama_key.is_empty() {
            request
        } else {
            request.bearer_auth(&self.cfg.llama_key)
        }
    }

    /// An LLM turn released the GPU right after unloading its model. Hermes
    /// keeps calling the model after a run ends (background review, a late
    /// steer), which loads it straight back next to the render: for a while,
    /// unload it whenever the GPU holder is a job that never uses the LLM.
    pub fn start_llm_guard(&self) {
        *lock(&self.llm_guard_until) = Some(Instant::now() + self.cfg.llm_settle);
    }
}

async fn loaded_models(shared: &Shared) -> Option<Vec<String>> {
    let response = shared
        .llama("/running")
        .timeout(Duration::from_secs(4))
        .send()
        .await
        .ok()?;
    let body: Value = response.error_for_status().ok()?.json().await.ok()?;
    Some(
        body.get("running")?
            .as_array()?
            .iter()
            .filter_map(|m| {
                m.get("model")
                    .or_else(|| m.get("name"))
                    .and_then(Value::as_str)
                    .map(str::to_owned)
            })
            .collect(),
    )
}

pub async fn guard_loop(shared: Arc<Shared>) {
    let mut tick = tokio::time::interval(GUARD_INTERVAL);
    // The guard ends once nothing reloaded the model for QUIET (as the Agent's
    // own settle wait did), or at its deadline.
    let mut window: Option<Instant> = None;
    let mut quiet_since = Instant::now();
    loop {
        tick.tick().await;
        let until = *lock(&shared.llm_guard_until);
        let Some(until) = until else {
            window = None;
            continue;
        };
        if window != Some(until) {
            window = Some(until);
            quiet_since = Instant::now();
        }
        if Instant::now() >= until || quiet_since.elapsed() >= QUIET {
            *lock(&shared.llm_guard_until) = None;
            continue;
        }
        // Only while every job on the GPU is one that never uses the LLM: an
        // LLM job running next to a render needs its model loaded.
        let holder = shared.read_queue(|q| {
            let mut actives = q.actives().peekable();
            actives.peek()?;
            let mut first = None;
            for e in actives {
                if e.uses_llm != Some(false) {
                    return None;
                }
                first.get_or_insert_with(|| (e.app.clone(), e.job_id.clone()));
            }
            first
        });
        // Nobody to protect yet (the next job hasn't started): keep watching.
        let Some((app, job)) = holder else {
            quiet_since = Instant::now();
            continue;
        };
        let Some(models) = loaded_models(&shared).await.filter(|m| !m.is_empty()) else {
            continue;
        };
        tracing::info!(
            "LLM guard: unloading {} while {app}/{} renders",
            models.join(", "),
            job.get(..8).unwrap_or(&job)
        );
        let _ = shared
            .llama("/unload")
            // Until it has really stopped: Strata finishes its running request first.
            .timeout(Duration::from_secs(480))
            .send()
            .await;
        METRICS.llm_unloads.inc();
        quiet_since = Instant::now();
    }
}

/// Show remote API work only while Hermes is running and Llama has a model
/// loaded. A display card, never a queue entry.
async fn hermes_card(shared: &Shared) -> Option<Value> {
    let cfg = &shared.cfg;
    if cfg.hermes_key.is_empty() || cfg.llama_key.is_empty() {
        return None;
    }
    let hermes = shared
        .http
        .get(format!("{}/health/detailed", cfg.hermes_url))
        .bearer_auth(&cfg.hermes_key)
        .timeout(Duration::from_secs(3))
        .send();
    let (hermes, loaded) = tokio::join!(hermes, loaded_models(shared));
    let health: Value = hermes.ok()?.error_for_status().ok()?.json().await.ok()?;
    let runs = health
        .pointer("/readiness/checks/background_queues/active_api_runs")?
        .as_i64()?;
    let model = loaded?.into_iter().next()?;
    if runs < 1 {
        return None;
    }
    Some(json!({
        "entry_id": "hermes-remote-active", "app": "Hermes", "job_id": "remote-agent",
        "label": if runs > 1 { format!("Remote agent · {runs} runs") } else { "Remote agent".to_owned() },
        "state": "active", "is_continuous": true, "is_locked": false,
        "is_informational": true, "loaded_model": model, "shutdown_at": null,
        "position": null, "runs": runs,
    }))
}

pub async fn hermes_loop(shared: Arc<Shared>) {
    let mut tick = tokio::time::interval(HERMES_INTERVAL);
    loop {
        tick.tick().await;
        // Nobody is looking at the queue: don't poll Hermes and Llama.
        let card = if shared.viewed_recently() {
            hermes_card(&shared).await
        } else {
            None
        };
        let changed = {
            let mut current = lock(&shared.hermes_card);
            let changed = *current != card;
            *current = card;
            changed
        };
        if changed {
            shared.publish_queue();
        }
    }
}
