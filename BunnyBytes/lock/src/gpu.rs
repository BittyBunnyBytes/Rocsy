//! The GPUs on this machine and which of them each kind of work runs on.
//!
//! Cards are found in amdgpu sysfs (`/sys/class/drm/cardN/device`) and kept in
//! PCI bus order, which is the order ROCm numbers them in. Each is identified
//! by its ROCm UUID (`GPU-<device/unique_id>`), which doesn't change when a
//! card is added or moved to another slot; ROCR_VISIBLE_DEVICES accepts it.
//!
//! A **role** is one kind of GPU work (ComfyUI renders, the LLM, ...). The
//! queue settings map each role to an ordered list of card ids (Queue page ->
//! GPUs); a role that isn't set, or whose cards are all gone, uses the
//! default: the primary card, or every card (primary first) for the `_multi`
//! roles. Launchers ask `GET /v2/gpus/visible?role=...` for the
//! ROCR_VISIBLE_DEVICES value; the queue itself plans each job's VRAM on the
//! cards of its role.

use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};

use serde_json::{json, Value};

const MIB: u64 = 1024 * 1024;

/// Every role: (name, what runs there).
pub const ROLE_INFO: &[(&str, &str)] = &[
    ("comfyui", "ComfyUI renders (Image, Music, Video, 3D Models)"),
    (
        "comfyui_multi",
        "Video's two-GPU H3 modes (split / parallel / conditioning on GPU 1)",
    ),
    (
        "llama",
        "LLM (llama.cpp): Agentic planning, Master Agent, chat",
    ),
    (
        "llama_multi",
        "LLM models with Multi-GPU on (split across these cards)",
    ),
    ("voice", "Voice (TTS, voice conversion, whisper)"),
    ("trainer", "Trainer (LoRA training, tagging)"),
];

pub const ROLES: &[&str] = &[
    "comfyui",
    "comfyui_multi",
    "llama",
    "llama_multi",
    "voice",
    "trainer",
];

/// The role an app's jobs run on, unless the job names another.
pub fn app_role(app: &str) -> &'static str {
    match app {
        "Vid" | "Krea" | "Mus" | "Model3D" => "comfyui",
        "Voice" => "voice",
        "Trainer" => "trainer",
        // The Master Agent and the Agentic framework: their LLM turns add the
        // LLM's cards (uses_llm); anything else of theirs runs on the primary.
        _ => "default",
    }
}

pub fn is_multi(role: &str) -> bool {
    role.ends_with("_multi")
}

#[derive(Clone, Debug)]
pub struct Card {
    pub dir: PathBuf,
    /// PCI slot, e.g. 0000:0a:00.0 ("" when unknown).
    pub pci: String,
    /// ROCm UUID (GPU-...) when the driver reports one, else PCI-<slot>.
    pub id: String,
    pub name: String,
    /// The card driving the display (boot_vga).
    pub display: bool,
    /// The PCIe link it really runs at (the narrowest hop up to the CPU).
    pub link: Option<Link>,
}

/// A PCIe link: lanes and per-lane rate (GT/s), now and at best.
#[derive(Clone, Debug, PartialEq)]
pub struct Link {
    pub width: u32,
    pub max_width: u32,
    pub rate: f64,
    pub max_rate: f64,
}

/// PCIe generation from the per-lane rate.
pub fn generation(rate: f64) -> u8 {
    match rate {
        r if r >= 60.0 => 6,
        r if r >= 30.0 => 5,
        r if r >= 15.0 => 4,
        r if r >= 7.5 => 3,
        r if r >= 4.5 => 2,
        _ => 1,
    }
}

/// The bottleneck of a chain of links (device first, root port last). A
/// Radeon's own endpoint and the switch inside it report x16 whatever the
/// slot is wired for, so only the whole chain tells.
pub fn bottleneck(links: &[Link]) -> Option<Link> {
    let mut out = links.first()?.clone();
    for l in &links[1..] {
        out.width = out.width.min(l.width);
        out.max_width = out.max_width.min(l.max_width);
        out.rate = out.rate.min(l.rate);
        out.max_rate = out.max_rate.min(l.max_rate);
    }
    Some(out)
}

fn link_of(device: &Path) -> Option<Link> {
    let gts =
        |d: &Path, f: &str| -> Option<f64> { read(d, f)?.split_whitespace().next()?.parse().ok() };
    let mut links = Vec::new();
    let mut dir = std::fs::canonicalize(device).ok();
    while let Some(d) = dir {
        let (Some(width), Some(max_width)) = (
            read(&d, "current_link_width").and_then(|w| w.parse::<u32>().ok()),
            read(&d, "max_link_width").and_then(|w| w.parse::<u32>().ok()),
        ) else {
            break; // the PCI host bridge
        };
        let rate = gts(&d, "current_link_speed").unwrap_or(0.0);
        let max_rate = gts(&d, "max_link_speed").unwrap_or(rate);
        if width > 0 && max_width > 0 && rate > 0.0 {
            links.push(Link {
                width,
                max_width,
                rate,
                max_rate,
            });
        }
        dir = d.parent().map(Path::to_path_buf);
    }
    bottleneck(&links)
}

fn link_json(link: &Option<Link>) -> Value {
    match link {
        None => Value::Null,
        Some(l) => json!({
            "width": l.width, "max_width": l.max_width,
            "gen": generation(l.rate), "max_gen": generation(l.max_rate),
            "rate_gts": l.rate, "max_rate_gts": l.max_rate,
            "label": format!("PCIe {}.0 x{}", generation(l.rate), l.width),
            "limited": l.width < l.max_width || l.rate + 0.01 < l.max_rate,
        }),
    }
}

fn read(dir: &Path, file: &str) -> Option<String> {
    std::fs::read_to_string(dir.join(file))
        .ok()
        .map(|s| s.trim().to_owned())
}

impl Card {
    pub fn open(dir: &Path) -> Option<Card> {
        let mut card = Card {
            dir: dir.to_path_buf(),
            pci: std::fs::canonicalize(dir)
                .ok()
                .and_then(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()))
                .filter(|n| n.contains(':'))
                .unwrap_or_default(),
            id: String::new(),
            name: String::new(),
            display: read(dir, "boot_vga").as_deref() == Some("1"),
            link: None,
        };
        card.total_mb()?;
        card.link = link_of(dir);
        card.id = match read(dir, "unique_id").filter(|u| !u.is_empty()) {
            Some(uuid) => format!("GPU-{uuid}"),
            None if !card.pci.is_empty() => format!("PCI-{}", card.pci),
            None => format!("PATH-{}", dir.display()),
        };
        card.name = name_of(
            read(dir, "device").as_deref().unwrap_or(""),
            card.total_mb().unwrap_or(0),
        );
        Some(card)
    }

    fn read_mb(&self, file: &str) -> Option<u64> {
        read(&self.dir, file)?.parse::<u64>().ok().map(|b| b / MIB)
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

    /// What ROCR_VISIBLE_DEVICES calls this card (`ordinal`: its ROCm index).
    fn rocr(&self, ordinal: usize) -> String {
        if self.id.starts_with("GPU-") {
            self.id.clone()
        } else {
            ordinal.to_string()
        }
    }
}

fn name_of(device: &str, total_mb: u64) -> String {
    let gib = total_mb / 1024;
    match device {
        // Navi 31: the XTX has 24 GiB, the XT 20 GiB, the GRE 16 GiB.
        "0x744c" if gib >= 22 => "RX 7900 XTX".into(),
        "0x744c" if gib >= 18 => "RX 7900 XT".into(),
        "0x744c" => "RX 7900 GRE".into(),
        "0x7448" => "Radeon Pro W7900".into(),
        "0x747e" if gib >= 14 => "RX 7800 XT".into(),
        "0x747e" => "RX 7700 XT".into(),
        "0x7480" => "RX 7600".into(),
        "0x7550" => "RX 9070 XT / 9070".into(),
        "0x7590" => "RX 9060 XT".into(),
        "" => format!("GPU ({gib} GiB)"),
        other => format!("AMD GPU {other} ({gib} GiB)"),
    }
}

/// Every card under `root` (normally /sys/class/drm) with VRAM counters, in
/// PCI order.
pub fn detect(root: &Path) -> Vec<Card> {
    let mut cards: Vec<Card> = std::fs::read_dir(root)
        .map(|it| {
            it.flatten()
                .filter_map(|entry| {
                    let name = entry.file_name().to_string_lossy().into_owned();
                    // card0, card1 ... (not the connectors card1-DP-1, ...)
                    if !name.starts_with("card") || name.contains('-') {
                        return None;
                    }
                    Card::open(&entry.path().join("device"))
                })
                .collect()
        })
        .unwrap_or_default();
    cards.sort_by(|a, b| a.pci.cmp(&b.pci).then_with(|| a.dir.cmp(&b.dir)));
    cards.dedup_by(|a, b| a.id == b.id);
    cards
}

/// The default card: LOCK_VRAM_DEVICE when set, else the one with the most
/// VRAM (the display card on a tie, then the first).
pub fn primary(cards: &[Card], configured: Option<&Path>) -> usize {
    if let Some(dir) = configured {
        let want = std::fs::canonicalize(dir).unwrap_or_else(|_| dir.to_path_buf());
        if let Some(i) = cards
            .iter()
            .position(|c| std::fs::canonicalize(&c.dir).unwrap_or_else(|_| c.dir.clone()) == want)
        {
            return i;
        }
    }
    let mut best = 0;
    for (i, c) in cards.iter().enumerate() {
        let b = &cards[best];
        let (t, bt) = (c.total_mb().unwrap_or(0), b.total_mb().unwrap_or(0));
        if t > bt || (t == bt && c.display && !b.display) {
            best = i;
        }
    }
    best
}

/// role -> card indices, from the saved choices (role -> card ids). Cards
/// that aren't there any more are skipped; an empty result is the default.
///
/// `prefer_multi` (Queue settings -> "Use multi-GPU wherever it can"), with two
/// or more cards: the LLM role gets the multi-GPU LLM cards, so every model
/// is split across them. ComfyUI's renders have no multi-GPU mode of their own;
/// Video asks for `comfyui_multi` itself (see Video's comfy profile choice).
pub fn resolve(
    ids: &[String],
    primary: usize,
    chosen: &BTreeMap<String, Vec<String>>,
    prefer_multi: bool,
) -> BTreeMap<String, Vec<usize>> {
    let mut roles = resolve_chosen(ids, primary, chosen);
    if prefer_multi && ids.len() >= 2 {
        if let Some(multi) = roles.get("llama_multi").cloned() {
            if multi.len() >= 2 {
                roles.insert("llama".into(), multi);
            }
        }
    }
    roles
}

fn resolve_chosen(
    ids: &[String],
    primary: usize,
    chosen: &BTreeMap<String, Vec<String>>,
) -> BTreeMap<String, Vec<usize>> {
    ROLES
        .iter()
        .map(|role| {
            let mut cards: Vec<usize> = chosen
                .get(*role)
                .map(|want| {
                    want.iter()
                        .filter_map(|id| ids.iter().position(|have| have == id))
                        .collect()
                })
                .unwrap_or_default();
            let mut seen = Vec::new();
            cards.retain(|c| {
                let new = !seen.contains(c);
                seen.push(*c);
                new
            });
            if !is_multi(role) {
                cards.truncate(1);
            }
            if cards.is_empty() && !ids.is_empty() {
                cards.push(primary);
                if is_multi(role) {
                    cards.extend((0..ids.len()).filter(|i| *i != primary));
                }
            }
            ((*role).to_owned(), cards)
        })
        .collect()
}

/// ROCR_VISIBLE_DEVICES for a list of cards (order kept: the first one is
/// device 0 inside the process). Only cards that exist: ROCm stops at the
/// first unknown id and would hide the rest.
pub fn visible(cards: &[Card], indices: &[usize]) -> String {
    indices
        .iter()
        .filter_map(|i| cards.get(*i).map(|c| c.rocr(*i)))
        .collect::<Vec<_>>()
        .join(",")
}

/// The GPU layout for the queue page and the launchers.
pub fn describe(
    cards: &[Card],
    primary: usize,
    used: &[u64],
    chosen: &BTreeMap<String, Vec<String>>,
    prefer_multi: bool,
) -> Value {
    let ids: Vec<String> = cards.iter().map(|c| c.id.clone()).collect();
    let roles = resolve(&ids, primary, chosen, prefer_multi);
    json!({
        "primary": primary,
        "count": cards.len(),
        "multi_available": cards.len() >= 2,
        "prefer_multi_gpu": prefer_multi,
        "multi_active": prefer_multi && cards.len() >= 2,
        "cards": cards.iter().enumerate().map(|(i, c)| json!({
            "index": i, "id": c.id, "pci": c.pci, "name": c.name, "display": c.display,
            "primary": i == primary, "total_mb": c.total_mb(), "used_mb": used.get(i),
            "link": link_json(&c.link),
        })).collect::<Vec<_>>(),
        "roles": ROLE_INFO.iter().map(|(role, about)| {
            let picked = roles.get(*role).cloned().unwrap_or_default();
            json!({
                "role": role, "about": about, "multi": is_multi(role),
                "cards": picked, "visible": visible(cards, &picked),
                "custom": chosen.get(*role).is_some_and(|v| !v.is_empty()),
                "chosen": chosen.get(*role),
            })
        }).collect::<Vec<_>>(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fake(root: &Path, card: &str, pci: &str, uuid: Option<&str>, gib: u64, display: bool) {
        let dev = root.join("devices").join(pci);
        std::fs::create_dir_all(&dev).unwrap_or_else(|e| panic!("{e}"));
        std::fs::write(
            dev.join("mem_info_vram_total"),
            (gib * 1024 * MIB).to_string(),
        )
        .unwrap_or_else(|e| panic!("{e}"));
        std::fs::write(dev.join("mem_info_vram_used"), (512 * MIB).to_string())
            .unwrap_or_else(|e| panic!("{e}"));
        std::fs::write(dev.join("device"), "0x744c\n").unwrap_or_else(|e| panic!("{e}"));
        std::fs::write(dev.join("boot_vga"), if display { "1" } else { "0" })
            .unwrap_or_else(|e| panic!("{e}"));
        if let Some(u) = uuid {
            std::fs::write(dev.join("unique_id"), format!("{u}\n"))
                .unwrap_or_else(|e| panic!("{e}"));
        }
        let c = root.join("drm").join(card);
        std::fs::create_dir_all(&c).unwrap_or_else(|e| panic!("{e}"));
        std::os::unix::fs::symlink(&dev, c.join("device")).unwrap_or_else(|e| panic!("{e}"));
        // A connector entry must be skipped.
        std::fs::create_dir_all(root.join("drm").join(format!("{card}-DP-1")))
            .unwrap_or_else(|e| panic!("{e}"));
    }

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("bunnylock-gpu-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap_or_else(|e| panic!("{e}"));
        d
    }

    #[test]
    fn two_cards_in_pci_order_by_uuid() {
        let root = tmp("two");
        // The new XT sits in a lower slot than the XTX: it is ROCm device 0.
        fake(
            &root,
            "card2",
            "0000:0d:00.0",
            Some("8b6a04dc2c73e091"),
            24,
            true,
        );
        fake(
            &root,
            "card1",
            "0000:03:00.0",
            Some("1111222233334444"),
            20,
            false,
        );
        let cards = detect(&root.join("drm"));
        assert_eq!(cards.len(), 2);
        assert_eq!(cards[0].pci, "0000:03:00.0");
        assert_eq!(cards[0].name, "RX 7900 XT");
        assert_eq!(cards[1].name, "RX 7900 XTX");
        assert_eq!(cards[1].id, "GPU-8b6a04dc2c73e091");
        let p = primary(&cards, None);
        assert_eq!(p, 1, "the XTX (most VRAM) stays primary");
        let ids: Vec<String> = cards.iter().map(|c| c.id.clone()).collect();
        let roles = resolve(&ids, p, &BTreeMap::new(), false);
        assert_eq!(roles["comfyui"], [1]);
        assert_eq!(roles["comfyui_multi"], [1, 0], "primary first");
        assert_eq!(
            visible(&cards, &roles["comfyui_multi"]),
            "GPU-8b6a04dc2c73e091,GPU-1111222233334444"
        );
        // LLM on the XT; a single-card role keeps only its first pick.
        let chosen = BTreeMap::from([
            ("llama".to_owned(), vec![ids[0].clone(), ids[1].clone()]),
            ("voice".to_owned(), vec!["GPU-gone".to_owned()]),
        ]);
        let roles = resolve(&ids, p, &chosen, false);
        assert_eq!(roles["llama"], [0]);
        assert_eq!(
            roles["voice"],
            [1],
            "a missing card falls back to the default"
        );
        let roles = resolve(&ids, p, &chosen, true);
        assert_eq!(
            roles["llama"],
            [1, 0],
            "prefer multi-GPU: the LLM splits across both"
        );
        assert_eq!(roles["comfyui"], [1]);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn no_uuid_uses_ordinals_and_configured_primary_wins() {
        let root = tmp("ord");
        fake(&root, "card0", "0000:0a:00.0", None, 24, true);
        fake(&root, "card1", "0000:0b:00.0", None, 24, false);
        let cards = detect(&root.join("drm"));
        assert_eq!(cards[1].id, "PCI-0000:0b:00.0");
        assert_eq!(primary(&cards, None), 0, "tie: the display card");
        assert_eq!(primary(&cards, Some(&root.join("drm/card1/device"))), 1);
        assert_eq!(visible(&cards, &[1, 0, 7]), "1,0");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn link_is_the_narrowest_hop() {
        let l = |w, mw, r, mr| Link {
            width: w,
            max_width: mw,
            rate: r,
            max_rate: mr,
        };
        // card 16/16, its switch 16/16, the chipset/root ports 8/16.
        let b = bottleneck(&[
            l(16, 16, 16.0, 16.0),
            l(16, 16, 16.0, 16.0),
            l(8, 16, 16.0, 16.0),
        ])
        .unwrap_or_else(|| panic!("no link"));
        assert_eq!((b.width, b.max_width, generation(b.rate)), (8, 16, 4));
        assert_eq!(generation(2.5), 1);
        assert_eq!(generation(32.0), 5);
        assert_eq!(link_json(&Some(b))["label"], "PCIe 4.0 x8");
        assert!(bottleneck(&[]).is_none());
    }

    #[test]
    fn no_cards_resolves_to_nothing() {
        let roles = resolve(&[], 0, &BTreeMap::new(), true);
        let one = resolve(&["GPU-a".to_owned()], 0, &BTreeMap::new(), true);
        assert_eq!(one["llama"], [0], "one card: nothing to split");
        assert!(roles.values().all(Vec::is_empty));
        assert_eq!(visible(&[], &[0]), "");
    }
}
