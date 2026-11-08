//! Settings from the environment (BunnyBytes/.env via docker-compose.yml).

use std::{collections::BTreeMap, env, path::PathBuf, time::Duration};

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("{0} is required and must not be empty. Set it in BunnyBytes/.env before starting the queue.")]
    Missing(&'static str),
}

#[derive(Clone, Debug)]
pub struct Config {
    pub host: String,
    pub port: u16,
    pub secret: String,
    pub prefix: String,
    pub ttl: Duration,
    /// A disconnected app process's job holding the GPU keeps it this long
    /// (the app may be restarting and adopt it), then the GPU moves on.
    pub active_grace: Duration,
    /// Its waiting entries are kept this long for the restarted app to adopt.
    pub adopt_grace: Duration,
    /// After a queue restart: time for every app to reconnect before anyone is judged dead.
    pub startup_grace: Duration,
    pub data_dir: PathBuf,
    pub static_dir: PathBuf,
    pub template: PathBuf,
    /// Apps whose `GET {base}/api/jobs/{job_id}` reports render progress (polling fallback).
    pub progress_apps: BTreeMap<String, String>,
    pub hermes_url: String,
    pub hermes_key: String,
    pub llama_url: String,
    pub llama_key: String,
    pub supervisor_url: String,
    /// How long after an LLM turn the queue keeps the LLM unloaded for renders.
    pub llm_settle: Duration,
    /// The GPU's sysfs device directory (with mem_info_vram_*); unset = the
    /// drm card with the most VRAM.
    pub vram_device: Option<PathBuf>,
}

fn var(name: &str, default: &str) -> String {
    env::var(name)
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| default.to_owned())
}

fn base(name: &str, default: &str) -> String {
    var(name, default).trim_end_matches('/').to_owned()
}

fn seconds(name: &str, default: f64) -> Duration {
    Duration::from_secs_f64(var(name, "").parse().unwrap_or(default).max(0.0))
}

impl Config {
    pub fn from_env() -> Result<Self, ConfigError> {
        let secret = env::var("QUEUE_INTERNAL_SECRET")
            .ok()
            .filter(|v| !v.is_empty())
            .ok_or(ConfigError::Missing("QUEUE_INTERNAL_SECRET"))?;
        // Base URLs include each app's own mount prefix.
        let progress_apps = BTreeMap::from([
            (
                "Vid".to_owned(),
                base("VID_UPSTREAM", "http://127.0.0.1:8000") + "/Video",
            ),
            (
                "Mus".to_owned(),
                base("MUS_UPSTREAM", "http://127.0.0.1:8200") + "/Music",
            ),
            (
                "Krea".to_owned(),
                base("KREA_UPSTREAM", "http://127.0.0.1:8300") + "/Image",
            ),
            (
                "Trainer".to_owned(),
                base("TRAINER_UPSTREAM", "http://127.0.0.1:8500") + "/Trainer",
            ),
            (
                "Voice".to_owned(),
                base("VOICE_UPSTREAM", "http://127.0.0.1:8950") + "/Voice",
            ),
        ]);
        let data_dir = PathBuf::from(var("LOCK_DATA_DIR", "/app/data"));
        Ok(Config {
            host: var("LOCK_HOST", "127.0.0.1"),
            port: var("LOCK_PORT", "8600").parse().unwrap_or(8600),
            secret,
            prefix: var("QUEUE_MOUNT_PREFIX", "")
                .trim_end_matches('/')
                .to_owned(),
            ttl: seconds("LOCK_TTL_SECONDS", 90.0),
            active_grace: seconds("LOCK_ACTIVE_GRACE_SECONDS", 30.0),
            adopt_grace: seconds("LOCK_ADOPT_GRACE_SECONDS", 600.0),
            startup_grace: seconds("LOCK_STARTUP_GRACE_SECONDS", 15.0),
            static_dir: PathBuf::from(var("LOCK_STATIC_DIR", "/app/static")),
            template: PathBuf::from(var("LOCK_TEMPLATE", "/app/templates/queue.html")),
            data_dir,
            progress_apps,
            hermes_url: base("HERMES_API_URL", "http://127.0.0.1:8642"),
            hermes_key: var("HERMES_API_KEY", ""),
            llama_url: base("LLAMA_URL", "http://127.0.0.1:8400"),
            llama_key: var("LLAMA_API_KEY", ""),
            supervisor_url: base("COMFYUI_SUPERVISOR_URL", "http://127.0.0.1:8083"),
            llm_settle: seconds("LOCK_LLM_SETTLE_SECONDS", 150.0),
            vram_device: env::var("LOCK_VRAM_DEVICE")
                .ok()
                .filter(|v| !v.is_empty())
                .map(PathBuf::from),
        })
    }
}
