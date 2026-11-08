//! Render progress of the active entries: pushed by apps (POST /v2/progress),
//! with polling of the app's job API as the fallback for apps that don't push.

use std::{
    collections::HashSet,
    sync::Arc,
    time::{Duration, Instant},
};

use chrono::{DateTime, Utc};
use serde_json::Value;

use crate::{
    core::Entry,
    state::{lock, Progress, Shared},
};

const POLL_INTERVAL: Duration = Duration::from_secs(2);
const POLL_TIMEOUT: Duration = Duration::from_secs(3);
/// An app that pushed within this window is not polled.
const PUSH_FRESH: Duration = Duration::from_secs(5);
/// Browsers get at most this many progress events per second.
const MIN_PUBLISH_GAP: Duration = Duration::from_millis(250);

fn seconds_since(value: Option<&str>, fallback: Option<DateTime<Utc>>) -> Option<f64> {
    let started = value
        .and_then(|v| {
            DateTime::parse_from_rfc3339(v)
                .ok()
                .map(|t| t.with_timezone(&Utc))
        })
        .or_else(|| {
            value.and_then(|v| {
                chrono::NaiveDateTime::parse_from_str(v, "%Y-%m-%dT%H:%M:%S%.f")
                    .ok()
                    .map(|n| n.and_utc())
            })
        })
        .or(fallback)?;
    Some(((Utc::now() - started).num_milliseconds() as f64 / 1000.0).max(0.0))
}

fn number(v: &Value) -> Option<f64> {
    if v.is_boolean() {
        return None;
    }
    v.as_f64()
}

/// Normalise an app's job JSON to percent + time left. Apps that give an
/// estimate (Video's render average, the Trainer's epoch clock) use it;
/// otherwise time left is extrapolated from the progress so far.
pub fn normalise(
    app: &str,
    job: &Value,
    became_active_at: Option<DateTime<Utc>>,
) -> Option<Progress> {
    let mut stage = job.get("stage").and_then(Value::as_str).map(str::to_owned);
    let raw = job.get("progress")?;
    let mut remaining: Option<f64> = None;
    let percent = if app == "Trainer" && raw.is_object() {
        if let Some(label) = raw
            .get("phase_label")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        {
            stage = Some(label.to_owned());
        }
        remaining = job
            .get("timing")
            .and_then(|t| t.get("remaining_seconds"))
            .and_then(number)
            .or_else(|| raw.get("eta_seconds").and_then(number));
        raw.get("overall").and_then(number).unwrap_or(0.0) * 100.0
    } else {
        number(raw)?
    };
    let percent = percent.clamp(0.0, 100.0);
    let elapsed = seconds_since(
        job.get("started_at").and_then(Value::as_str),
        became_active_at,
    );
    let estimate = job
        .get("estimate")
        .and_then(|e| e.get("seconds"))
        .and_then(number)
        .filter(|s| *s > 0.0);
    if remaining.is_none() {
        if let (Some(estimate), Some(elapsed)) = (estimate, elapsed) {
            if elapsed < estimate {
                remaining = Some(estimate - elapsed);
            }
        }
    }
    if remaining.is_none() {
        if let Some(elapsed) = elapsed {
            if (3.0..100.0).contains(&percent) {
                remaining = Some(elapsed * (100.0 - percent) / percent);
            }
        }
    }
    Some(Progress {
        percent: (percent * 10.0).round() / 10.0,
        remaining_seconds: remaining.map(|r| r.round() as i64),
        stage: stage.map(|s| s.chars().take(60).collect()),
    })
}

/// Store a new value; publish now unless browsers got one very recently
/// (the poll loop flushes a held-back value).
pub fn update(shared: &Arc<Shared>, entry_id: &str, value: Option<Progress>, pushed: bool) {
    enum Next {
        Nothing,
        Publish,
        FlushIn(Duration),
    }
    let next = {
        let mut p = lock(&shared.progress);
        if pushed {
            p.pushed_at.insert(entry_id.to_owned(), Instant::now());
        }
        let changed = match value {
            Some(v) => {
                p.by_entry.get(entry_id) != Some(&v) && {
                    p.by_entry.insert(entry_id.to_owned(), v);
                    true
                }
            }
            None => p.by_entry.remove(entry_id).is_some(),
        };
        let since = p.published_at.map(|t| t.elapsed());
        if !changed {
            Next::Nothing
        } else if since.is_none_or(|e| e >= MIN_PUBLISH_GAP) {
            Next::Publish
        } else {
            p.unpublished = true;
            if p.flush_scheduled {
                Next::Nothing
            } else {
                p.flush_scheduled = true;
                Next::FlushIn(MIN_PUBLISH_GAP - since.unwrap_or_default())
            }
        }
    };
    match next {
        Next::Nothing => {}
        Next::Publish => shared.publish_progress(),
        // Held back by the rate limit: send it as soon as the gap is over.
        Next::FlushIn(wait) => {
            let shared = shared.clone();
            tokio::spawn(async move {
                tokio::time::sleep(wait).await;
                let due = {
                    let mut p = lock(&shared.progress);
                    p.flush_scheduled = false;
                    p.unpublished
                };
                if due {
                    shared.publish_progress();
                }
            });
        }
    }
}

pub async fn poll_loop(shared: Arc<Shared>) {
    let mut tick = tokio::time::interval(POLL_INTERVAL);
    loop {
        tick.tick().await;
        let actives: Vec<Entry> = shared.read_queue(|q| q.actives().cloned().collect());
        let current: HashSet<&str> = actives.iter().map(|e| e.entry_id.as_str()).collect();
        let (stale, poll, flush) = {
            let mut p = lock(&shared.progress);
            let before = p.by_entry.len();
            p.by_entry.retain(|k, _| current.contains(k.as_str()));
            p.unknown.retain(|k| current.contains(k.as_str()));
            p.pushed_at.retain(|k, _| current.contains(k.as_str()));
            let poll: Vec<Entry> = actives
                .iter()
                .filter(|e| {
                    !p.unknown.contains(&e.entry_id)
                        && p.pushed_at
                            .get(&e.entry_id)
                            .is_none_or(|t| t.elapsed() > PUSH_FRESH)
                })
                .cloned()
                .collect();
            (p.by_entry.len() != before, poll, p.unpublished)
        };
        if stale || flush {
            shared.publish_progress();
        }
        for active in poll {
            let Some(base) = shared.cfg.progress_apps.get(&active.app) else {
                continue;
            };
            let url = format!("{base}/api/jobs/{}", active.job_id);
            match shared.http.get(&url).timeout(POLL_TIMEOUT).send().await {
                Ok(r) if r.status() == reqwest::StatusCode::NOT_FOUND => {
                    lock(&shared.progress)
                        .unknown
                        .insert(active.entry_id.clone());
                }
                Ok(r) if r.status().is_success() => {
                    if let Ok(job) = r.json::<Value>().await {
                        update(
                            &shared,
                            &active.entry_id,
                            normalise(&active.app, &job, active.became_active_at),
                            false,
                        );
                    }
                }
                _ => {} // busy or restarting: keep the last value
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn plain_percent_and_estimate() {
        let started = (Utc::now() - chrono::Duration::seconds(10)).to_rfc3339();
        let p = normalise(
            "Vid",
            &json!({"progress": 50, "started_at": started, "estimate": {"seconds": 30}}),
            None,
        );
        let p = p.unwrap_or_else(|| panic!("no progress"));
        assert_eq!(p.percent, 50.0);
        assert!(matches!(p.remaining_seconds, Some(19..=21)));
    }

    #[test]
    fn extrapolates_and_trainer_shape() {
        let started = (Utc::now() - chrono::Duration::seconds(10)).to_rfc3339();
        let p = normalise(
            "Krea",
            &json!({"progress": 25.0, "started_at": started}),
            None,
        )
        .unwrap_or_else(|| panic!());
        assert!(matches!(p.remaining_seconds, Some(29..=31)));
        let t = normalise("Trainer", &json!({"progress": {"overall": 0.5, "phase_label": "Epoch 2"}, "timing": {"remaining_seconds": 100}}), None)
            .unwrap_or_else(|| panic!());
        assert_eq!(
            (t.percent, t.remaining_seconds, t.stage.as_deref()),
            (50.0, Some(100), Some("Epoch 2"))
        );
        assert!(normalise("Krea", &json!({"progress": true}), None).is_none());
        assert!(normalise("Krea", &json!({}), None).is_none());
    }
}
