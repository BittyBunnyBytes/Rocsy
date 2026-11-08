//! SQLite persistence: the same `queue` table the Python lock used (plus the
//! `instance`, `uses_llm`, `model`, `config` and `vram_mb` columns), so either version can read the file.
//! Writes are write-through for exactly the rows a call changed.

use std::path::Path;

use chrono::{DateTime, SecondsFormat, Utc};
use rusqlite::{params, Connection, OptionalExtension, Row};

use crate::core::{Entry, State};

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("sqlite: {0}")]
    Sqlite(#[from] rusqlite::Error),
}

pub struct Store {
    conn: Connection,
}

/// Python's `datetime.isoformat()` for an aware UTC time.
pub fn iso(t: DateTime<Utc>) -> String {
    t.to_rfc3339_opts(SecondsFormat::Micros, false)
}

fn parse(value: Option<String>) -> Option<DateTime<Utc>> {
    let value = value?;
    DateTime::parse_from_rfc3339(&value)
        .map(|t| t.with_timezone(&Utc))
        .or_else(|_| {
            // Naive timestamps (no offset) are UTC, as in the Python lock.
            chrono::NaiveDateTime::parse_from_str(&value, "%Y-%m-%dT%H:%M:%S%.f")
                .map(|n| n.and_utc())
        })
        .ok()
}

fn row_to_entry(row: &Row<'_>) -> rusqlite::Result<Entry> {
    let state: String = row.get("state")?;
    Ok(Entry {
        entry_id: row.get("entry_id")?,
        app: row.get("app")?,
        job_id: row.get("job_id")?,
        token: row.get("token")?,
        label: row.get("label")?,
        state: if state == "active" {
            State::Active
        } else {
            State::Waiting
        },
        is_continuous: row.get::<_, i64>("is_continuous")? != 0,
        shutdown_at: row.get("shutdown_at")?,
        seq: row.get("seq")?,
        sort_order: row.get::<_, Option<i64>>("sort_order")?.unwrap_or(0),
        is_locked: row.get::<_, i64>("is_locked")? != 0,
        enqueued_at: parse(row.get("enqueued_at")?).unwrap_or_else(Utc::now),
        became_active_at: parse(row.get("became_active_at")?),
        expires_at: parse(row.get("expires_at")?),
        group: row.get("grp")?,
        instance: row.get("instance")?,
        uses_llm: row.get::<_, Option<i64>>("uses_llm")?.map(|v| v != 0),
        acked: false,
        model: row.get("model")?,
        config: row.get("config")?,
        vram_mb: row
            .get::<_, Option<i64>>("vram_mb")?
            .map(|v| v.max(0) as u64),
    })
}

impl Store {
    pub fn open(path: &Path) -> Result<Self, StoreError> {
        let conn = Connection::open(path)?;
        // WAL + NORMAL: a commit does not fsync the whole file.
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "synchronous", "NORMAL")?;
        conn.pragma_update(None, "busy_timeout", 5000)?;
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS queue (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id TEXT NOT NULL UNIQUE,
                app TEXT NOT NULL,
                job_id TEXT NOT NULL,
                token TEXT,
                label TEXT NOT NULL,
                state TEXT NOT NULL,
                is_continuous INTEGER NOT NULL DEFAULT 0,
                shutdown_at TEXT,
                enqueued_at TEXT NOT NULL,
                became_active_at TEXT,
                expires_at TEXT
            );",
        )?;
        let columns: Vec<String> = conn
            .prepare("PRAGMA table_info(queue)")?
            .query_map([], |r| r.get::<_, String>("name"))?
            .collect::<Result<_, _>>()?;
        for (name, ddl) in [
            (
                "sort_order",
                "ALTER TABLE queue ADD COLUMN sort_order INTEGER",
            ),
            (
                "is_locked",
                "ALTER TABLE queue ADD COLUMN is_locked INTEGER NOT NULL DEFAULT 0",
            ),
            ("grp", "ALTER TABLE queue ADD COLUMN grp TEXT"),
            ("instance", "ALTER TABLE queue ADD COLUMN instance TEXT"),
            ("uses_llm", "ALTER TABLE queue ADD COLUMN uses_llm INTEGER"),
            ("model", "ALTER TABLE queue ADD COLUMN model TEXT"),
            ("config", "ALTER TABLE queue ADD COLUMN config TEXT"),
            ("vram_mb", "ALTER TABLE queue ADD COLUMN vram_mb INTEGER"),
        ] {
            if !columns.iter().any(|c| c == name) {
                conn.execute(ddl, [])?;
            }
        }
        conn.execute(
            "UPDATE queue SET sort_order = seq WHERE sort_order IS NULL",
            [],
        )?;
        Ok(Store { conn })
    }

    pub fn load_all(&self) -> Result<Vec<Entry>, StoreError> {
        let mut stmt = self.conn.prepare_cached("SELECT * FROM queue")?;
        let rows = stmt
            .query_map([], row_to_entry)?
            .collect::<Result<_, _>>()?;
        Ok(rows)
    }

    /// Write the given rows (present = upsert, absent = delete) in one transaction.
    pub fn sync(
        &mut self,
        changed: &[String],
        lookup: impl Fn(&str) -> Option<Entry>,
    ) -> Result<(), StoreError> {
        if changed.is_empty() {
            return Ok(());
        }
        let tx = self.conn.transaction()?;
        for id in changed {
            match lookup(id) {
                None => {
                    tx.prepare_cached("DELETE FROM queue WHERE entry_id = ?1")?
                        .execute(params![id])?;
                }
                Some(e) => {
                    tx.prepare_cached(
                        "INSERT INTO queue (seq, entry_id, app, job_id, token, label, state, is_continuous, shutdown_at,
                                            enqueued_at, became_active_at, expires_at, sort_order, is_locked, grp, instance, uses_llm,
                                            model, config, vram_mb)
                         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20)
                         ON CONFLICT(entry_id) DO UPDATE SET
                            token = excluded.token, label = excluded.label, state = excluded.state,
                            shutdown_at = excluded.shutdown_at, became_active_at = excluded.became_active_at,
                            expires_at = excluded.expires_at, sort_order = excluded.sort_order,
                            is_locked = excluded.is_locked, grp = excluded.grp, instance = excluded.instance,
                            uses_llm = excluded.uses_llm, model = excluded.model,
                            config = excluded.config, vram_mb = excluded.vram_mb",
                    )?
                    .execute(params![
                        e.seq,
                        e.entry_id,
                        e.app,
                        e.job_id,
                        e.token,
                        e.label,
                        e.state.as_str(),
                        e.is_continuous as i64,
                        e.shutdown_at,
                        iso(e.enqueued_at),
                        e.became_active_at.map(iso),
                        e.expires_at.map(iso),
                        e.sort_order,
                        e.is_locked as i64,
                        e.group,
                        e.instance,
                        e.uses_llm.map(|v| v as i64),
                        e.model,
                        e.config,
                        e.vram_mb.map(|v| v as i64),
                    ])?;
                }
            }
        }
        tx.commit()?;
        Ok(())
    }

    #[allow(dead_code)]
    pub fn count(&self) -> Result<i64, StoreError> {
        Ok(self
            .conn
            .query_row("SELECT COUNT(*) FROM queue", [], |r| r.get(0))
            .optional()?
            .unwrap_or(0))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::{NewEntry, Queue};
    use chrono::Duration;

    #[test]
    fn round_trip_and_python_timestamps() {
        let dir =
            std::env::temp_dir().join(format!("bunnylock-test-{}", uuid::Uuid::new_v4().simple()));
        std::fs::create_dir_all(&dir).ok();
        let path = dir.join("q.sqlite3");
        let mut store = Store::open(&path).unwrap_or_else(|e| panic!("{e}"));
        let mut q = Queue::restore(Vec::new(), Duration::seconds(90));
        let now = Utc::now();
        for job in ["a", "b"] {
            q.enqueue(
                NewEntry {
                    app: "Krea".into(),
                    job_id: job.into(),
                    label: job.into(),
                    is_continuous: false,
                    priority: false,
                    group: Some("G".into()),
                    instance: Some("i1".into()),
                    uses_llm: Some(false),
                    model: Some("flux".into()),
                    config: Some("1024x1024".into()),
                    vram_mb: Some(9_000),
                },
                now,
            );
        }
        let dirty = q.take_dirty();
        store
            .sync(&dirty, |id| q.get(id).cloned())
            .unwrap_or_else(|e| panic!("{e}"));
        let loaded = Queue::restore(store.load_all().unwrap_or_default(), Duration::seconds(90));
        assert_eq!(loaded.entries().len(), 2);
        assert_eq!(loaded.entries()[0].group.as_deref(), Some("G"));
        assert_eq!(loaded.entries()[1].uses_llm, Some(false));
        assert_eq!(loaded.entries()[1].model.as_deref(), Some("flux"));
        assert_eq!(loaded.entries()[1].vram_mb, Some(9_000));
        assert_eq!(
            parse(Some("2026-10-31T15:13:14.851234+00:00".into())).map(|t| t.timestamp()),
            Some(1793459594)
        );
        assert!(parse(Some("2026-10-31T15:13:14.851234".into())).is_some());
        std::fs::remove_dir_all(&dir).ok();
    }
}
