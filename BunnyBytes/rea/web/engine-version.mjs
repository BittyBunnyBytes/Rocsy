// The engine VERSION of a game folder, read from the engine's own data (pack headers, serialized
// file headers, runtime scripts), so the Files view and every agent brief can say "Godot 3.5.2" or
// "Unity 2022.3.60f1" instead of just the engine name: API names and syntax change between versions.
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";

const cache = new Map(); // gameDir → { key, at, value }

async function readAt(file, bytes, from = 0) {
  const fd = await fsp.open(file, "r").catch(() => null);
  if (!fd) return Buffer.alloc(0);
  try {
    const { size } = await fd.stat();
    const start = from < 0 ? Math.max(0, size + from) : from;
    const n = Math.max(0, Math.min(bytes, size - start));
    const buf = Buffer.alloc(n);
    await fd.read(buf, 0, n, start);
    return buf;
  } finally { await fd.close(); }
}
const exists = (p) => fs.existsSync(p);

async function find(dir, test, maxDepth = 3, limit = 20, base = dir, depth = 0, out = []) {
  if (depth > maxDepth || out.length >= limit) return out;
  for (const e of await fsp.readdir(dir, { withFileTypes: true }).catch(() => [])) {
    if (out.length >= limit) break;
    const full = path.join(dir, e.name);
    const rel = path.relative(base, full).split(path.sep).join("/");
    if (e.isSymbolicLink()) continue;
    if (e.isDirectory()) await find(full, test, maxDepth, limit, base, depth + 1, out);
    else if (test(rel, e.name)) out.push(rel);
  }
  return out;
}

// Godot: the pack header (a .pck, or a pack embedded at the end of the executable) holds the engine version.
async function godotPack(file) {
  const tail = await readAt(file, 12, -12);
  let start = 0;
  if (tail.length === 12 && tail.subarray(8).toString("latin1") === "GDPC") {
    const { size } = await fsp.stat(file);
    start = size - 12 - Number(tail.readBigUInt64LE(0));
  }
  const head = await readAt(file, 20, start);
  if (head.length < 20 || head.subarray(0, 4).toString("latin1") !== "GDPC") return null;
  const [format, major, minor, patch] = [0, 1, 2, 3].map((i) => head.readUInt32LE(4 + i * 4));
  return { version: `${major}.${minor}${patch ? `.${patch}` : ""}`, major, source: "pack header", detail: `pack format ${format}` };
}
async function godot(dir) {
  const top = await fsp.readdir(dir, { withFileTypes: true }).catch(() => []);
  const files = top.filter((e) => e.isFile()).map((e) => e.name);
  for (const f of [...files.filter((f) => /\.pck$/i.test(f)), ...files.filter((f) => /\.(exe|x86_64|x86|arm64)$/i.test(f) || !f.includes("."))]) {
    const v = await godotPack(path.join(dir, f)).catch(() => null);
    if (v) return { engine: "Godot", ...v, file: f };
  }
  return null;
}

// Unity: every serialized file (globalgamemanagers, mainData, levelN) and UnityFS bundle names the editor version.
const UNITY_RE = /\b((?:20\d\d|[3-6])\.\d+\.\d+[abfpx]\d+)\b/;
async function unity(dir) {
  const data = (await fsp.readdir(dir, { withFileTypes: true }).catch(() => [])).find((e) => e.isDirectory() && /_Data$/.test(e.name))?.name;
  if (!data) return null;
  for (const name of ["globalgamemanagers", "mainData", "data.unity3d", "level0", "sharedassets0.assets"]) {
    const rel = `${data}/${name}`;
    const m = (await readAt(path.join(dir, rel), 4096)).toString("latin1").match(UNITY_RE);
    if (m) {
      const backend = exists(path.join(dir, "GameAssembly.dll")) || exists(path.join(dir, "GameAssembly.so")) ? "IL2CPP" : "Mono";
      return { engine: "Unity", version: m[1], major: Number(m[1].split(".")[0]), source: rel, detail: backend };
    }
  }
  return null;
}

// Unreal: the shipping executable carries its build branch string, e.g. "++UE4+Release-4.27" (often UTF-16).
async function unreal(dir) {
  const exe = (await find(dir, (r) => /\/Binaries\/(Win64|WinGDK|Linux)\/[^/]+-Shipping(\.exe)?$/i.test("/" + r), 3, 5))
    .sort((a, b) => a.length - b.length)[0];
  if (!exe) return null;
  const file = path.join(dir, exe);
  const ascii = /\+\+UE([45])\+Release-(\d+\.\d+)/;
  const utf16 = /\+\0\+\0U\0E\0([45])\0\+\0R\0e\0l\0e\0a\0s\0e\0-\0((?:\d\0)+\.\0(?:\d\0)+)/;
  const fd = await fsp.open(file, "r").catch(() => null);
  if (!fd) return null;
  try {
    const { size } = await fd.stat();
    const chunk = 8 << 20, overlap = 128, cap = Math.min(size, 512 << 20);
    const buf = Buffer.alloc(chunk + overlap);
    for (let pos = 0; pos < cap; pos += chunk) {
      const { bytesRead } = await fd.read(buf, 0, chunk + overlap, pos);
      const text = buf.subarray(0, bytesRead).toString("latin1");
      const m = text.match(ascii) || text.match(utf16);
      if (m) {
        const version = m[2].replace(/\0/g, "");
        return { engine: "Unreal Engine", version, major: Number(m[1]), source: exe, detail: `UE${m[1]}` };
      }
    }
  } finally { await fd.close(); }
  return { engine: "Unreal Engine", version: "", major: 0, source: exe, detail: "build string not found" };
}

// GameMaker: data.win's GEN8 chunk holds the bytecode version (what UndertaleModTool cares about) and the IDE major.
async function gamemaker(dir) {
  for (const name of ["data.win", "game.unx", "game.ios"]) {
    const head = await readAt(path.join(dir, name), 80);
    if (head.length < 64 || head.subarray(0, 4).toString("latin1") !== "FORM" || head.subarray(8, 12).toString("latin1") !== "GEN8") continue;
    const bytecode = head[17], ide = [60, 64, 68, 72].map((o) => head.readUInt32LE(o));
    const major = ide[0] >= 2 ? 2 : 1;
    return { engine: "GameMaker", version: `${major === 2 ? "Studio 2+" : "Studio 1.x"}`, major, source: name,
      detail: `bytecode ${bytecode}${ide[0] && ide.slice(1).some(Boolean) ? `, IDE ${ide.join(".")}` : ""}` };
  }
  return null;
}

// RPG Maker MV / MZ: the core script's header comment.
async function rpgmaker(dir) {
  for (const [js, core, maker] of [["www/js", "rpg_core.js", "MV"], ["js", "rpg_core.js", "MV"], ["js", "rmmz_core.js", "MZ"]]) {
    const m = (await readAt(path.join(dir, js, core), 512)).toString("utf8").match(/(?:rpg_core|rmmz_core)\.js v(\d+\.\d+\.\d+)/);
    if (exists(path.join(dir, js, core)))
      return { engine: "RPG Maker", version: [maker, m?.[1]].filter(Boolean).join(" "), major: maker === "MZ" ? 2 : 1, source: `${js}/${core}`, detail: "" };
  }
  return null;
}

// Ren'Py: renpy/vc_version.py (newer) or the version tuple in renpy/__init__.py.
async function renpy(dir) {
  if (!exists(path.join(dir, "renpy"))) return null;
  const vc = (await readAt(path.join(dir, "renpy/vc_version.py"), 2048)).toString("utf8");
  let version = vc.match(/^version\s*=\s*u?['"](\d+\.\d+\.\d+)/m)?.[1];
  if (!version) {
    const init = (await readAt(path.join(dir, "renpy/__init__.py"), 16384)).toString("utf8");
    const t = init.match(/version_tuple\s*=\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/) || init.match(/VersionTuple\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/);
    if (t) version = `${t[1]}.${t[2]}.${t[3]}`;
  }
  return { engine: "Ren'Py", version: version || "", major: Number(version?.split(".")[0]) || 0, source: "renpy/",
    detail: version ? (Number(version.split(".")[0]) >= 8 ? "Python 3" : "Python 2") : "" };
}

// Source / GoldSrc: steam.inf in the game folder carries the patch version.
async function valve(dir) {
  const inf = (await find(dir, (r, n) => n === "steam.inf", 2, 1))[0];
  if (!inf) return null;
  const text = (await readAt(path.join(dir, inf), 2048)).toString("utf8");
  const v = text.match(/^PatchVersion\s*=\s*([\d.]+)/m)?.[1];
  return v ? { engine: "", version: `patch ${v}`, major: 0, source: inf, detail: "" } : null;
}

const PROBES = [godot, unity, unreal, gamemaker, rpgmaker, renpy, valve];

// { engine, version, major, source, detail, label } or null. Cached per folder until its top level changes.
export async function engineVersion(dir) {
  const key = String((await fsp.stat(dir).catch(() => null))?.mtimeMs || 0);
  const hit = cache.get(dir);
  if (hit && hit.key === key && Date.now() - hit.at < 30 * 60 * 1000) return hit.value;
  let value = null;
  for (const probe of PROBES) {
    try { value = await probe(dir); } catch { value = null; }
    if (value) break;
  }
  if (value) {
    value.label = [value.engine, value.version].filter(Boolean).join(" ") + (value.detail ? ` (${value.detail})` : "");
    // For appending to an engine name that already says it (Unity's label carries Mono/IL2CPP).
    value.short = [value.version, value.engine === "Unity" ? "" : value.detail].filter(Boolean).join(" · ");
  }
  cache.set(dir, { key, at: Date.now(), value });
  return value;
}

// Which inventory engine hint a version belongs to.
export function versionMatches(hintName, v) {
  if (!v) return false;
  if (!v.engine) return /Source|GoldSrc/.test(hintName);
  const a = hintName.toLowerCase(), b = v.engine.toLowerCase();
  return a.startsWith(b.split(" ")[0]) || b.startsWith(a.split(" ")[0]);
}

// A short version note for agent briefs on engines without a dedicated rename list (Godot has its own in mods.mjs).
export function versionBrief(v) {
  if (!v?.version && !v?.detail) return "";
  const name = v.label;
  const notes = {
    "Unity": `Target this Unity version's APIs${v.detail === "IL2CPP" ? " (IL2CPP: Il2CppInterop types, BepInEx 6)" : " (Mono: .NET Framework/netstandard as shipped in <Game>_Data/Managed, BepInEx 5)"}; ` +
      "APIs added in later Unity versions do not exist here; check names against the game's Managed/ or dumped assemblies.",
    "Unreal Engine": v.major === 5 ? "UE5: FVector/FRotator are doubles; UE4SS Lua names follow the UE5 reflection dump."
      : "UE4: FVector/FRotator are floats; UE5-only classes and functions do not exist.",
    "GameMaker": "Match the GML of this runtime/bytecode version (UndertaleModTool compiles for it); GMS2.3+ functions/structs need bytecode 17.",
    "RPG Maker": v.major === 2 ? "MZ: PIXI v5, rmmz_*.js classes; MV-only (rpg_*) names differ." : "MV: PIXI v4, rpg_*.js classes, plugins in www/js/plugins.js; MZ-only (rmmz_*) APIs do not exist.",
    "Ren'Py": v.major >= 8 ? "Ren'Py 8 runs Python 3." : "Ren'Py 7 and older run Python 2 (print statement, no f-strings).",
  }[v.engine] || "";
  return `ENGINE VERSION: this game runs ${name}. ${notes}`.trim();
}
