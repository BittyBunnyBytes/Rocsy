//! VRAM held by programs outside Rocsy (games, the browser, the desktop), per
//! card, from the kernel's per-process DRM accounting (`/proc/<pid>/fdinfo`).
//!
//! The card-wide reading counts everything. A job charged "reading - floor"
//! was also charged a game that started while it ran (Deadlock's 9 GB showed
//! up as real-time voice needing 17 GB). Subtracting what these outside
//! programs hold leaves Rocsy's own use.
//!
//! A process is Rocsy's when its cgroup is a container (docker / containerd /
//! podman) or one of Rocsy's systemd units (rocsy-*, comfyui*, core-*,
//! bunny-*), or when its fdinfo can't be read (another user: every container
//! runs as its own user). Unreadable processes are counted as Rocsy, so the
//! correction only ever removes VRAM that clearly belongs to someone else.
//!
//! Per client: drm-total-vram minus drm-shared-vram (buffers shared with other
//! clients, e.g. a window's buffer the compositor also maps, are counted by
//! their owner). drm-resident-vram is not used: amdgpu reports it above the
//! card's size for some clients. Each drm-client-id is counted once.
//!
//! Rocsy's own host services are also summed per unit (ComfyUI, the voice
//! worker, real-time voice, Strata), so a job isn't charged what another
//! app's idle service keeps loaded (vram.rs `idle_units`).
//!
//! Needs the host's process list (`pid: host` in compose). In a container
//! that only sees itself, `scan` returns None and nothing changes.

use std::{
    collections::{HashMap, HashSet},
    fs,
    path::Path,
};

/// Rocsy's own systemd user units (cgroup leaf names start with these).
const ROCSY_UNITS: [&str; 5] = ["rocsy-", "comfyui", "core-", "bunny-", "docker-"];
const CONTAINER_MARKERS: [&str; 4] = ["docker", "containerd", "libpod", "/lxc/"];

fn is_rocsy_cgroup(cgroup: &str) -> bool {
    let path = cgroup.lines().find_map(|l| l.strip_prefix("0::")).unwrap_or(cgroup);
    if CONTAINER_MARKERS.iter().any(|m| path.contains(m)) {
        return true;
    }
    let leaf = path.rsplit('/').next().unwrap_or("");
    ROCSY_UNITS.iter().any(|u| leaf.starts_with(u)) && leaf.ends_with(".service")
}

/// "123456 KiB" -> MiB-ish KiB count.
fn kib(value: &str) -> u64 {
    let mut parts = value.split_whitespace();
    let n: u64 = parts.next().and_then(|v| v.parse().ok()).unwrap_or(0);
    match parts.next().unwrap_or("KiB") {
        "B" => n / 1024,
        "MiB" => n * 1024,
        "GiB" => n * 1024 * 1024,
        _ => n,
    }
}

/// One amdgpu client's (pci address, client id, private KiB) from an fdinfo file.
fn client(fdinfo: &str) -> Option<(String, String, u64)> {
    let mut driver = false;
    let (mut pdev, mut id, mut total, mut shared) = (None, None, 0, 0);
    for line in fdinfo.lines() {
        let Some((key, value)) = line.split_once(':') else {
            continue;
        };
        let value = value.trim();
        match key.trim() {
            "drm-driver" => driver = value == "amdgpu",
            "drm-pdev" => pdev = Some(value.to_owned()),
            "drm-client-id" => id = Some(value.to_owned()),
            "drm-total-vram" => total = kib(value),
            "drm-shared-vram" => shared = kib(value),
            _ => {}
        }
    }
    if !driver {
        return None;
    }
    Some((pdev?, id?, total.saturating_sub(shared)))
}

/// One scan: MiB per card (card order) held by outside programs, and by each
/// readable Rocsy systemd unit (leaf cgroup name, e.g. "comfyui-supervisor.service").
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Scan {
    pub outside: Vec<u64>,
    pub units: HashMap<String, Vec<u64>>,
}

/// `pcis`: each card's PCI address, in card order. None when the host's
/// processes aren't visible.
pub fn scan(proc_root: &Path, pcis: &[String]) -> Option<Scan> {
    let me = std::process::id().to_string();
    // Without pid: host, pid 1 is this very process: nothing to see.
    if fs::read_to_string(proc_root.join("1/cmdline")).is_ok_and(|c| c.contains("bunny-lock"))
        && proc_root == Path::new("/proc")
    {
        return None;
    }
    let mut seen: HashSet<(String, String)> = HashSet::new();
    let mut by_pci: HashMap<String, u64> = HashMap::new();
    let mut unit_pci: HashMap<String, HashMap<String, u64>> = HashMap::new();
    let entries = fs::read_dir(proc_root).ok()?;
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(pid) = name.to_str().filter(|n| n.bytes().all(|b| b.is_ascii_digit())) else {
            continue;
        };
        if pid == me {
            continue;
        }
        let dir = entry.path();
        let Ok(fds) = fs::read_dir(dir.join("fd")) else {
            continue; // another user's process: counted as Rocsy
        };
        // Only processes that have a GPU node open are worth reading further.
        let gpu_fds: Vec<String> = fds
            .flatten()
            .filter(|fd| {
                fs::read_link(fd.path())
                    .is_ok_and(|t| t.to_string_lossy().starts_with("/dev/dri/"))
            })
            .map(|fd| fd.file_name().to_string_lossy().into_owned())
            .collect();
        if gpu_fds.is_empty() {
            continue;
        }
        let cgroup = fs::read_to_string(dir.join("cgroup")).unwrap_or_default();
        // A container's processes are left to the card-wide count (most can't be read anyway).
        let unit = if is_rocsy_cgroup(&cgroup) {
            match unit_of(&cgroup) {
                Some(unit) => Some(unit),
                None => continue,
            }
        } else {
            None
        };
        for fd in gpu_fds {
            let Ok(info) = fs::read_to_string(dir.join("fdinfo").join(&fd)) else {
                continue;
            };
            if let Some((pdev, id, private)) = client(&info) {
                if seen.insert((pdev.clone(), id)) {
                    match &unit {
                        Some(u) => *unit_pci.entry(u.clone()).or_default().entry(pdev).or_default() += private,
                        None => *by_pci.entry(pdev).or_default() += private,
                    }
                }
            }
        }
    }
    let per_card = |m: &HashMap<String, u64>| -> Vec<u64> {
        pcis.iter().map(|pci| m.get(pci).copied().unwrap_or(0) / 1024).collect()
    };
    Some(Scan {
        outside: per_card(&by_pci),
        units: unit_pci.iter().map(|(u, m)| (u.clone(), per_card(m))).collect(),
    })
}

/// The Rocsy systemd unit a cgroup belongs to (None for containers).
fn unit_of(cgroup: &str) -> Option<String> {
    let path = cgroup.lines().find_map(|l| l.strip_prefix("0::")).unwrap_or(cgroup);
    if CONTAINER_MARKERS.iter().any(|m| path.contains(m)) {
        return None;
    }
    path.rsplit('/').next().filter(|l| l.ends_with(".service")).map(str::to_owned)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fdinfo(pdev: &str, id: u32, total_kib: u64, shared_kib: u64) -> String {
        format!(
            "pos:\t0\ndrm-driver:\tamdgpu\ndrm-pdev:\t{pdev}\ndrm-client-id:\t{id}\n\
             drm-total-vram:\t{total_kib} KiB\ndrm-shared-vram:\t{shared_kib} KiB\n\
             drm-resident-vram:\t99999999 KiB\n"
        )
    }

    /// A fake /proc: pid -> (cgroup, [(fd, fdinfo)]). Fds link to /dev/dri.
    fn fake(root: &Path, procs: &[(&str, &str, Vec<(&str, String)>)]) {
        for (pid, cgroup, fds) in procs {
            let dir = root.join(pid);
            fs::create_dir_all(dir.join("fd")).unwrap_or_default();
            fs::create_dir_all(dir.join("fdinfo")).unwrap_or_default();
            fs::write(dir.join("cgroup"), format!("0::{cgroup}\n")).unwrap_or_default();
            for (fd, info) in fds {
                std::os::unix::fs::symlink("/dev/dri/renderD128", dir.join("fd").join(fd))
                    .unwrap_or_default();
                fs::write(dir.join("fdinfo").join(fd), info).unwrap_or_default();
            }
        }
    }

    #[test]
    fn counts_only_outside_programs_once_per_client() {
        let root = std::env::temp_dir().join(format!("procvram-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let card = "0000:0a:00.0";
        fake(
            &root,
            &[
                // A game: 9 GiB, three fds of one client (counted once) + a 1 GiB second client.
                ("100", "/user.slice/user@1000.service/app.slice/app-flatpak-com.valvesoftware.Steam-1.scope",
                 vec![("5", fdinfo(card, 7, 9 * 1024 * 1024, 0)), ("6", fdinfo(card, 7, 9 * 1024 * 1024, 0)),
                      ("7", fdinfo(card, 8, 1024 * 1024, 0))]),
                // The compositor: 160 MiB total, 100 MiB of it shared windows -> 60 MiB.
                ("200", "/user.slice/session-3.scope", vec![("3", fdinfo(card, 9, 160 * 1024, 100 * 1024))]),
                // Rocsy: ComfyUI's unit, the real-time voice unit, a container: not counted.
                ("300", "/user.slice/user@1000.service/app.slice/comfyui-supervisor.service",
                 vec![("4", fdinfo(card, 10, 8 * 1024 * 1024, 0))]),
                ("301", "/user.slice/user@1000.service/app.slice/rocsy-voice-realtime.service",
                 vec![("4", fdinfo(card, 11, 1024 * 1024, 0))]),
                ("302", "/system.slice/docker-abc123.scope", vec![("4", fdinfo(card, 12, 4 * 1024 * 1024, 0))]),
                // Another GPU.
                ("400", "/user.slice/session-3.scope", vec![("3", fdinfo("0000:0b:00.0", 13, 512 * 1024, 0))]),
            ],
        );
        let got = scan(&root, &[card.to_owned(), "0000:0b:00.0".to_owned(), "0000:0c:00.0".to_owned()])
            .unwrap_or_default();
        assert_eq!(got.outside, vec![10 * 1024 + 60, 512, 0]);
        assert_eq!(got.units.get("comfyui-supervisor.service"), Some(&vec![8 * 1024, 0, 0]));
        assert_eq!(got.units.get("rocsy-voice-realtime.service"), Some(&vec![1024, 0, 0]));
        assert_eq!(got.units.len(), 2, "the container isn't a unit");
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn rocsy_units_and_containers() {
        assert!(is_rocsy_cgroup("0::/user.slice/user@1000.service/app.slice/core-strata-serve.service"));
        assert!(is_rocsy_cgroup("0::/system.slice/docker-1f2e.scope"));
        assert!(is_rocsy_cgroup("0::/user.slice/user@1000.service/app.slice/bunny-hostctl.service"));
        assert!(!is_rocsy_cgroup("0::/user.slice/user@1000.service/app.slice/app-flatpak-x.scope"));
        assert!(!is_rocsy_cgroup("0::/user.slice/user-1000.slice/session-3.scope"));
        assert!(!is_rocsy_cgroup("0::/user.slice/user@1000.service/app.slice/rocsy-notes.scope"));
    }
}
