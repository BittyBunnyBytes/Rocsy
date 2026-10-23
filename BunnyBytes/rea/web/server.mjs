// Rocsy REA: the /REA web page + the MCP bridge Hermes uses, in one container.
// - Runs supergateway (REA's stdio MCP served as Streamable HTTP on :8975) and restarts it if it dies.
// - Serves /REA/ on :8976 (behind the gateway's login): targets come from uploads or the Master Agent's
//   workspace/attachments, and each open target keeps its own `rea mcp` process so Ghidra analyses a
//   file once and every later question (functions, pseudocode, strings, xrefs) answers at once.
import http from "node:http";
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { handleMods, detectGame } from "./mods.mjs";
import { handleRules } from "./rules.mjs";
import { handleKb } from "./kb.mjs";
import { handleLibrary } from "./library.mjs";
import { handleTestRuns } from "./testrun.mjs";
import { handleProjectCheck } from "./buildcheck.mjs";
import { handleOutbox, reaModel } from "./outbox.mjs";
import { handleGoals } from "./goals.mjs";
import { handleTools } from "./tools.mjs";
import { record as recordDecomp, saveItem, readItem, recoveredLanguageFiles } from "./decomp.mjs";
import { detectEngineHints } from "./engine-detection.mjs";
import { engineVersion, versionMatches } from "./engine-version.mjs";
import { detectGameLanguages } from "./language-detection.mjs";

const PORT = Number(process.env.REA_WEB_PORT || 8976);
const MCP_PORT = Number(process.env.REA_PORT || 8975);
const PREFIX = "/REA";
const STATIC = path.join(path.dirname(new URL(import.meta.url).pathname), "static");
// folders = host directories mounted read-only at /folders/<name> (added on this page, see hostctl/reafolders.py).
const ROOTS = { workspace: "/opt/data/rocsy-workspace", attachments: "/opt/data/rocsy-attachments", folders: "/folders" };
const UPLOADS = path.join(ROOTS.workspace, "rea");
const DATA = process.env.REA_WEB_DATA || "/home/node/rea-web";
const TARGETS_FILE = path.join(DATA, "targets.json");
const MAX_UPLOAD = 4 * 1024 ** 3;
const MAX_SESSIONS = 2;
const IDLE_MS = 30 * 60 * 1000;
const ANALYSIS_MS = 60 * 60 * 1000;
const VERSION = (() => {
  try { return JSON.parse(fs.readFileSync("/usr/local/lib/node_modules/rea-agents/package.json", "utf8")).version; }
  catch { return ""; }
})();

// ---------------------------------------------------------------- MCP bridge for Hermes --
let gateway = null;
let gatewayDelay = 2000;
let stopping = false;
function startGateway() {
  gateway = spawn("supergateway", ["--stdio", "rea mcp", "--outputTransport", "streamableHttp", "--stateful",
    "--sessionTimeout", "1800000", "--port", String(MCP_PORT), "--logLevel", "info"], { stdio: "inherit" });
  const started = Date.now();
  gateway.on("exit", (code) => {
    gateway = null;
    if (stopping) return;
    gatewayDelay = Date.now() - started > 60000 ? 2000 : Math.min(gatewayDelay * 2, 30000);
    console.error(`supergateway exited (${code}); restarting in ${gatewayDelay / 1000}s`);
    setTimeout(startGateway, gatewayDelay);
  });
}

// ---------------------------------------------------------------------- MCP stdio client --
class ReaProcess {
  constructor() {
    this.child = spawn("rea", ["mcp"], { stdio: ["pipe", "pipe", "pipe"] });
    this.pending = new Map();
    this.next = 1;
    this.buffer = "";
    this.stderr = "";
    this.closed = false;
    this.child.stdout.setEncoding("utf8");
    this.child.stdout.on("data", (chunk) => {
      this.buffer += chunk;
      let index;
      while ((index = this.buffer.indexOf("\n")) >= 0) {
        const line = this.buffer.slice(0, index).trim();
        this.buffer = this.buffer.slice(index + 1);
        if (line) this.receive(line);
      }
    });
    this.child.stderr.on("data", (chunk) => { this.stderr = (this.stderr + chunk).slice(-4000); });
    this.child.on("exit", (code) => {
      this.closed = true;
      for (const { reject, timer } of this.pending.values()) {
        clearTimeout(timer);
        reject(new Error(`REA stopped (exit ${code})${this.stderr ? `: ${this.stderr.trim().split("\n").pop()}` : ""}`));
      }
      this.pending.clear();
    });
  }

  receive(line) {
    let message;
    try { message = JSON.parse(line); } catch { return; }
    if (message.id !== undefined && message.method) {
      // Server-to-client requests: answer pings, decline everything else.
      const reply = message.method === "ping" ? { result: {} } : { error: { code: -32601, message: "Not supported" } };
      this.send({ jsonrpc: "2.0", id: message.id, ...reply });
      return;
    }
    const waiter = this.pending.get(message.id);
    if (!waiter) return;
    this.pending.delete(message.id);
    clearTimeout(waiter.timer);
    if (message.error) waiter.reject(new Error(message.error.message || "MCP error"));
    else waiter.resolve(message.result);
  }

  send(message) {
    if (!this.closed) this.child.stdin.write(JSON.stringify(message) + "\n");
  }

  request(method, params, timeoutMs = 120000) {
    if (this.closed) return Promise.reject(new Error("REA session is closed"));
    const id = this.next++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`${method} timed out`)); }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.send({ jsonrpc: "2.0", id, method, params });
    });
  }

  async init() {
    await this.request("initialize", { protocolVersion: "2025-06-18", capabilities: {},
      clientInfo: { name: "rocsy-rea-web", version: "1" } }, 60000);
    this.send({ jsonrpc: "2.0", method: "notifications/initialized" });
  }

  async call(name, args, timeoutMs) {
    const result = await this.request("tools/call", { name, arguments: args }, timeoutMs);
    const text = (result.content || []).filter((c) => c.type === "text").map((c) => c.text).join("\n");
    if (result.isError) {
      let detail = text;
      try { const parsed = JSON.parse(text); detail = parsed.error?.message || parsed.message || parsed.error || text; } catch {}
      throw new Error(String(detail).slice(0, 600) || `${name} failed`);
    }
    if (result.structuredContent) return result.structuredContent;
    try { return JSON.parse(text); } catch { return { text }; }
  }

  close() {
    if (!this.closed) this.child.kill("SIGTERM");
    setTimeout(() => { if (!this.closed) this.child.kill("SIGKILL"); }, 5000).unref();
  }
}

// ------------------------------------------------------------------------------ targets --
let targets = {};
try { targets = JSON.parse(fs.readFileSync(TARGETS_FILE, "utf8")); } catch {}
const sessions = new Map(); // target id -> {proc, state, overview, opened, error, started, used}
const uploads = new Map();  // upload id -> {name, size, received, file}

async function saveTargets() {
  await fsp.mkdir(DATA, { recursive: true });
  const tmp = TARGETS_FILE + ".tmp";
  await fsp.writeFile(tmp, JSON.stringify(targets, null, 1));
  await fsp.rename(tmp, TARGETS_FILE);
}

function newId() { return randomBytes(6).toString("hex"); }

function safeName(name) {
  const base = path.basename(String(name || "file")).replace(/[^\w.+@-]+/g, "_").replace(/^\.+/, "");
  return (base || "file").slice(0, 120);
}

// Resolve a path the browser picked inside one of the agent folders; never outside them.
async function inside(rootKey, rel) {
  const root = ROOTS[rootKey];
  if (!root) throw httpError(400, "Unknown folder");
  const realRoot = await fsp.realpath(root);
  const resolved = await fsp.realpath(path.join(realRoot, String(rel || ""))).catch(() => null);
  if (!resolved || (resolved !== realRoot && !resolved.startsWith(realRoot + path.sep))) throw httpError(404, "Not found");
  return resolved;
}

// Read-only bytes of a game file for the agent: game folders are mounted only in REA, so Hermes
// can't open them (one session needed a bytecode file's header and had no way in).
// A directory lists its entries. Contained to the target; at most 1 MiB per call.
async function readTargetBytes(target, params) {
  const root = target.type === "folder" ? target.path : path.dirname(target.path);
  if (!fs.existsSync(root)) throw httpError(410, "The folder is no longer mounted");
  const realRoot = await fsp.realpath(root);
  const rel = target.type === "folder" ? String(params.get("rel") || "") : path.basename(target.path);
  const file = await fsp.realpath(path.join(realRoot, rel)).catch(() => null);
  if (!file || (file !== realRoot && !file.startsWith(realRoot + path.sep)))
    throw httpError(404, `No such file in ${target.name || "the target"}: ${rel}. rel is the path inside the folder, as GET ${PREFIX}/api/targets/${target.id}/inventory lists it (files[].rel).`);
  const stat = await fsp.stat(file);
  if (stat.isDirectory()) {
    const entries = [];
    for (const e of (await fsp.readdir(file, { withFileTypes: true })).slice(0, 2000)) {
      const s = e.isFile() ? await fsp.stat(path.join(file, e.name)).catch(() => null) : null;
      entries.push({ name: e.name, type: e.isDirectory() ? "dir" : e.isFile() ? "file" : "other", ...(s ? { size: s.size } : {}) });
    }
    return { rel: path.relative(realRoot, file), dir: true, entries };
  }
  const offset = Math.max(0, Math.min(stat.size, Number(params.get("offset")) || 0));
  const length = Math.max(1, Math.min(1 << 20, Number(params.get("length")) || 4096, stat.size - offset || 1));
  const format = ["hex", "base64", "text"].includes(params.get("format")) ? params.get("format") : "hex";
  const handle = await fsp.open(file, "r");
  let buf;
  try { buf = Buffer.alloc(length); buf = buf.subarray(0, (await handle.read(buf, 0, length, offset)).bytesRead); }
  finally { await handle.close(); }
  let data;
  if (format === "base64") data = buf.toString("base64");
  else if (format === "text") data = buf.toString("utf8");
  else {
    const lines = [];
    for (let i = 0; i < buf.length; i += 16) {
      const row = buf.subarray(i, i + 16);
      lines.push(`${(offset + i).toString(16).padStart(8, "0")}: ${[...row].map((b) => b.toString(16).padStart(2, "0")).join(" ").padEnd(47)}  |${[...row].map((b) => b >= 32 && b < 127 ? String.fromCharCode(b) : ".").join("")}|`);
    }
    data = lines.join("\n");
  }
  return { rel: path.relative(realRoot, file), size: stat.size, offset, length: buf.length, eof: offset + buf.length >= stat.size, format, data };
}

function publicTarget(target) {
  const stat = (() => { try { return fs.statSync(target.path); } catch { return null; } })();
  if (target.type === "folder") return { ...target, exists: Boolean(stat), mtime: stat?.mtimeMs || 0, state: "folder" };
  const session = sessions.get(target.id);
  return { ...target, exists: Boolean(stat), mtime: stat?.mtimeMs || 0, state: session?.state || "closed", error: session?.error || "",
    started: session?.started || 0, opened: session?.opened || null, overview: session?.overview || null };
}

// The game folder a file belongs to (opened from it, or lying inside it), for decompile progress.
function gameOf(target) {
  if (target.type === "folder") return null;
  const parent = target.parent && targets[target.parent];
  const game = parent?.type === "folder" ? parent
    : Object.values(targets).find((t) => t.type === "folder" && target.path.startsWith(t.path + path.sep));
  return game ? { game, rel: path.relative(game.path, target.path).split(path.sep).join("/") } : null;
}

function noteDecomp(target, update) {
  const g = gameOf(target);
  if (g) recordDecomp(g.game.path, { rel: g.rel, tool: "ghidra", by: "page", ...update })
    .catch((error) => console.error(`decompile progress: ${error.message}`));
}

function touch(session) {
  session.used = Date.now();
  clearTimeout(session.idle);
  session.idle = setTimeout(() => closeSession(session.id), IDLE_MS);
  session.idle.unref();
}

function closeSession(id) {
  const session = sessions.get(id);
  if (!session) return;
  clearTimeout(session.idle);
  session.proc.close();
  sessions.delete(id);
}

function openSession(target) {
  let session = sessions.get(target.id);
  if (session && session.state !== "error" && !session.proc.closed) { touch(session); return session; }
  if (session) closeSession(target.id);
  // Free a slot: the least recently used session that isn't mid-analysis goes first.
  const open = [...sessions.values()].sort((a, b) => a.used - b.used);
  while (open.length >= MAX_SESSIONS) {
    const victim = open.find((s) => s.state !== "analyzing");
    if (!victim) throw httpError(409, `Already analysing ${MAX_SESSIONS} files; wait or close one`);
    closeSession(victim.id);
    open.splice(open.indexOf(victim), 1);
  }
  session = { id: target.id, proc: new ReaProcess(), state: "analyzing", error: "", started: Date.now(), used: Date.now() };
  sessions.set(target.id, session);
  touch(session);
  (async () => {
    try {
      await session.proc.init();
      session.opened = (await session.proc.call("open_binary", { path: target.path }, 120000)).result || null;
      const overview = await session.proc.call("binary_overview", {}, ANALYSIS_MS);
      session.overview = overview.normalized_result || overview;
      session.state = "ready";
      noteDecomp(target, { sha256: session.opened?.sha256, format: session.opened?.format, action: "analysed" });
      if (session.opened) {
        Object.assign(target, { format: session.opened.format, kind: session.opened.kind,
          arch: session.opened.architecture, sha256: session.opened.sha256 });
        await saveTargets();
      }
    } catch (error) {
      session.state = "error";
      session.error = error.message;
    }
  })();
  return session;
}

// ----------------------------------------------------------------------- folder inventory --
// What a game/program folder is made of: every file sorted by what it is (from its first bytes,
// not just its name), plus engine hints. Symlinks are not followed; the walk is capped.
const MAX_WALK = 60000;
const inventories = new Map(); // target id -> {at, data}

async function sniff(file, name, size) {
  const lower = name.toLowerCase();
  const ext = lower.includes(".") ? lower.slice(lower.lastIndexOf(".") + 1) : "";
  let head = Buffer.alloc(0);
  let tail = Buffer.alloc(0);
  if (size >= 4) {
    const handle = await fsp.open(file, "r").catch(() => null);
    if (handle) {
      try {
        const { buffer, bytesRead } = await handle.read(Buffer.alloc(4096), 0, 4096, 0);
        head = buffer.subarray(0, bytesRead);
        if (/\.(exe|x86_64|x86|arm64)$/i.test(name)) {
          const end = await handle.read(Buffer.alloc(4), 0, 4, size - 4);
          tail = end.buffer.subarray(0, end.bytesRead);
        }
      }
      catch {} finally { await handle.close(); }
    }
  }
  const magic = head.subarray(0, 4).toString("hex");
  if (magic === "7f454c46") {
    const type = head.length > 18 ? head.readUInt16LE(16) : 0;
    return { kind: type === 3 && /\.so(\.|$)/.test(lower) ? "library" : "executable", format: "ELF",
      ...(tail.toString() === "GDPC" ? { embeddedPack: "Godot" } : {}) };
  }
  if (head.subarray(0, 2).toString() === "MZ" && head.length >= 64) {
    const pe = head.readUInt32LE(0x3c);
    if (pe + 24 < head.length && head.subarray(pe, pe + 4).toString("hex") === "50450000") {
      const coff = pe + 4, opt = coff + 20;
      const dll = (head.readUInt16LE(coff + 18) & 0x2000) !== 0;
      const magicOpt = opt + 2 <= head.length ? head.readUInt16LE(opt) : 0;
      const clr = opt + (magicOpt === 0x20b ? 112 : 96) + 14 * 8;
      const dotnet = clr + 8 <= head.length && head.readUInt32LE(clr) !== 0;
      if (dotnet) return { kind: "dotnet", format: dll ? ".NET DLL" : ".NET EXE" };
      return { kind: dll ? "library" : "executable", format: dll ? "PE DLL" : "PE EXE",
        ...(tail.toString() === "GDPC" ? { embeddedPack: "Godot" } : {}) };
    }
    return { kind: "other", format: "DOS/MZ" };
  }
  if (["feedface", "feedfacf", "cefaedfe", "cffaedfe"].includes(magic) || (magic === "cafebabe" && ext !== "class"))
    return { kind: ext === "dylib" ? "library" : "executable", format: "Mach-O" };
  if (magic === "0061736d") return { kind: "script", format: "WebAssembly" };
  if (magic === "47445043") return { kind: "package", format: "Godot .pck" };
  if (magic.startsWith("504b0304")) {
    const kinds = { apk: "Android APK", xapk: "Android XAPK", aab: "Android bundle", jar: "Java JAR", ipa: "iOS app", love: "LÖVE game", nupkg: "NuGet" };
    return { kind: kinds[ext] ? "package" : "archive", format: kinds[ext] || "ZIP" };
  }
  if (ext === "asar") return { kind: "package", format: "Electron ASAR" };
  if (lower === "bytecode.byc") return { kind: "package", format: "AGK bytecode" };
  if (["pak", "utoc", "ucas"].includes(ext)) return { kind: "package", format: "Unreal " + ext };
  if (["assets", "unity3d", "bundle", "resource", "ress"].includes(ext) || lower === "globalgamemanagers") return { kind: "package", format: "Unity data" };
  if (["js", "mjs", "cjs", "ts", "lua", "py", "rb", "sh", "bat", "cmd", "ps1", "rpy", "gd"].includes(ext)) return { kind: "script", format: ext.toUpperCase() };
  if (["dll", "so", "dylib"].includes(ext)) return { kind: "library", format: ext.toUpperCase() };
  return { kind: "other", format: ext ? ext.toUpperCase() : "" };
}

// Whether REA can reverse engineer a file, and how. re: ghidra | agent | text | none.
const MEDIA = new Set("png jpg jpeg tga dds ktx ktx2 bmp gif webp psd exr hdr tif tiff ico wav ogg mp3 flac opus bank fsb wem bnk xwb mp4 webm bik bk2 usm mkv avi ttf otf fnt woff woff2".split(" "));
const TEXT = new Set("txt md json xml ini cfg conf config yaml yml csv tsv log html htm css properties toml vdf acf manifest".split(" "));
function reInfo(f) {
  const ext = f.rel.includes(".") ? f.rel.slice(f.rel.lastIndexOf(".") + 1).toLowerCase() : "";
  if (f.size === 0) return { re: "none", reason: "Empty file" };
  if (["ELF", "PE EXE", "PE DLL", "Mach-O"].includes(f.format)) return { re: "ghidra",
    reason: f.embeddedPack === "Godot" ? "Godot executable with an embedded pack: recover the project under Mods; native code can be opened with Ghidra" : "Native code: open it with Ghidra" };
  if (f.kind === "dotnet") return { re: "agent", reason: ".NET assembly: decompile it under Mods (Unity) or ask the agent" };
  if (f.kind === "package") return { re: "agent", reason: `${f.format}: unpacked and read by the agent's REA tools` };
  if (f.kind === "script") return { re: "agent", reason: `${f.format}: source the agent can read and trace` };
  if (f.kind === "archive") return { re: "agent", reason: "Archive: the agent can list and extract it" };
  if (f.kind === "library") return { re: "none", reason: "Named like a library but has no valid ELF/PE/Mach-O header (packed or encrypted?)" };
  if (MEDIA.has(ext)) return { re: "none", reason: "Media or font: images, audio and video hold no code" };
  if (TEXT.has(ext)) return { re: "text", reason: "Plain text or config: readable as is, nothing to decompile" };
  return { re: "none", reason: "Unknown or encrypted data: no known format to analyse" };
}

const RE_ORDER = { ghidra: 0, agent: 1, text: 2, none: 3 };
const KIND_ORDER = { executable: 0, dotnet: 1, library: 2, package: 3, script: 4, archive: 5, other: 6 };
// Lower = more worth reverse engineering:
// 1. the game's own code (its main program, Assembly-CSharp*, IL2CPP's GameAssembly + metadata),
// 2. then by how it can be analysed (Ghidra → agent → text → can't),
// 3. stock engine/runtime/third-party libraries (UnityEngine.*, System.*, steam_api…) after the rest.
const ENGINE_LIB = /(^|\/)(UnityEngine[^/]*|Unity\.[^/]*|UnityPlayer\.[^/]+|System[^/]*|Mono\.[^/]*|mscorlib\.dll|netstandard\.dll|Microsoft\.[^/]*|Newtonsoft[^/]*|0Harmony\.dll|steam_api[^/]*|libsteam_api[^/]*|Galaxy[^/]*|EOSSDK[^/]*|D3D[^/]*|d3dcompiler[^/]*|vcruntime[^/]*|msvcp[^/]*|libc\+\+[^/]*|baselib[^/]*|lib(GL|EGL|vulkan|SDL|openal|fmod|bink)[^/]*|fmod[^/]*|Bink[^/]*|crashpad[^/]*|UnityCrashHandler[^/]*)$/i;
function rankOf(f, mains) {
  const base = path.basename(f.rel).replace(/\.(exe|x86_64|x86|bin|app)$/i, "").toLowerCase();
  const own = (f.kind === "executable" && mains.has(base)) || /(^|\/)(Assembly-CSharp[^/]*\.dll|GameAssembly\.(dll|so|dylib)|global-metadata\.dat)$/i.test(f.rel);
  const engine = ENGINE_LIB.test(f.rel);
  return (own ? 0 : 1000) + (RE_ORDER[f.re] ?? 3) * 100 + (engine ? 50 : 0) + (KIND_ORDER[f.kind] ?? 6) * 2;
}

async function mainNames(dir, name) {
  const top = await fsp.readdir(dir, { withFileTypes: true }).catch(() => []);
  return new Set([name.toLowerCase(), ...top.filter((e) => e.isDirectory() && /_Data$/.test(e.name)).map((e) => e.name.slice(0, -5).toLowerCase())]);
}

async function inventory(target) {
  const files = [];
  const skipped = [];
  const skip = (rel, type, reason) => { if (skipped.length < 2000) skipped.push({ rel, type, reason }); };
  let truncated = false;
  const stack = [""];
  while (stack.length) {
    const rel = stack.pop();
    let entries;
    try { entries = await fsp.readdir(path.join(target.path, rel), { withFileTypes: true }); }
    catch (error) { skip(rel || ".", "folder", error.code === "EACCES" ? "Folder can't be read (permission denied)" : `Folder can't be read (${error.code || "error"})`); continue; }
    for (const entry of entries) {
      const childRel = rel ? `${rel}/${entry.name}` : entry.name;
      if (entry.isSymbolicLink()) { skip(childRel, "link", "Symbolic link: not followed (it may point outside the folder)"); continue; }
      if (entry.isDirectory()) {
        if (childRel.split("/").length < 24) stack.push(childRel);
        else skip(childRel, "folder", "Nested too deep (over 24 levels): not scanned");
        continue;
      }
      if (!entry.isFile()) { skip(childRel, "special", "Not a regular file (socket, pipe or device)"); continue; }
      if (files.length >= MAX_WALK) { truncated = true; break; }
      const full = path.join(target.path, childRel);
      const stat = await fsp.stat(full).catch(() => null);
      if (!stat) { skip(childRel, "file", "File can't be read"); continue; }
      const f = { rel: childRel, size: stat.size, ...(await sniff(full, entry.name, stat.size)) };
      files.push({ ...f, ...reInfo(f) });
    }
    if (truncated) break;
  }
  if (truncated) for (const rel of stack.slice(0, 50)) skip(rel, "folder", `Not scanned: the folder has more than ${MAX_WALK.toLocaleString()} files`);
  const mains = await mainNames(target.path, target.name);
  for (const f of files) f.rank = rankOf(f, mains);
  const engines = detectEngineHints(files);
  const embeddedGodot = files.filter((f) => f.embeddedPack === "Godot").map((f) => f.rel);
  if (embeddedGodot.length && !engines.some((e) => e.name === "Godot"))
    engines.push({ name: "Godot (embedded pack)", evidence: embeddedGodot.slice(0, 3) });
  const version = await engineVersion(target.path).catch(() => null);
  const versioned = engines.find((e) => versionMatches(e.name, version));
  if (versioned) Object.assign(versioned, { version: version.short, versionSource: version.source });
  const kinds = {};
  for (const f of files) { const k = (kinds[f.kind] ||= { count: 0, bytes: 0 }); k.count++; k.bytes += f.size; }
  const re = {};
  for (const f of files) re[f.re] = (re[f.re] || 0) + 1;
  // Folders (top two levels) that hold nothing REA can analyse: only media, data or text.
  const byDir = new Map();
  for (const f of files) {
    const parts = f.rel.split("/");
    for (let depth = 1; depth <= Math.min(2, parts.length - 1); depth++) {
      const dir = parts.slice(0, depth).join("/");
      const d = byDir.get(dir) || byDir.set(dir, { files: 0, useful: 0, bytes: 0 }).get(dir);
      d.files++; d.bytes += f.size;
      if (f.re === "ghidra" || f.re === "agent") d.useful++;
    }
  }
  for (const [dir, d] of byDir) {
    const parent = dir.includes("/") ? byDir.get(dir.slice(0, dir.lastIndexOf("/"))) : null;
    if (d.useful || (parent && !parent.useful)) continue; // report the outermost such folder only
    skip(dir, "folder", `Nothing to reverse engineer inside: ${d.files.toLocaleString()} media/data/text files (${(d.bytes / 1048576).toFixed(1)} MB)`);
  }
  // Ranked: most worth reverse engineering first, then biggest.
  files.sort((a, b) => a.rank - b.rank || b.size - a.size);
  return { files, engines, engineVersion: version?.label || "", kinds, re, skipped, total: files.length, bytes: files.reduce((n, f) => n + f.size, 0), truncated };
}

const MODEL_TASKS = {
  plan: "Planning a mod (reads the game's code, writes the plan)",
  build: "Building a mod (writes and fixes the code)",
  refine: "Refining a mod after you play",
  map: "Mapping a game's architecture",
  ask: "Questions about a file or folder",
  decomp: "Continuing a game's decompilation",
  textures: "Finding textures for a game's 3D models",
};

// Questions the page may ask an open target, with the arguments each takes.
const TOOLS = {
  list_procedures: [], list_strings: [], search_strings: ["pattern", "mode"], search_procedures: ["pattern", "mode"],
  procedure_pseudo_code: ["procedure"], procedure_assembly: ["procedure"], procedure_info: ["procedure"],
  procedure_callers: ["procedure"], procedure_callees: ["procedure"], xrefs: ["address"],
  resolve_containing_procedure: ["address"],
};

// ------------------------------------------------------------------------------- HTTP --
function httpError(status, message) { return Object.assign(new Error(message), { status }); }

function send(res, status, body, headers = {}) {
  const data = typeof body === "string" || Buffer.isBuffer(body) ? body : JSON.stringify(body);
  res.writeHead(status, { "Content-Type": typeof body === "object" && !Buffer.isBuffer(body) ? "application/json" : "text/plain",
    "Cache-Control": "no-store", ...headers });
  res.end(data);
}

async function readJson(req, limit = 1024 * 1024) {
  if (!String(req.headers["content-type"] || "").startsWith("application/json")) throw httpError(415, "JSON body required");
  let size = 0;
  const chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) throw httpError(413, "Body too large");
    chunks.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString() || "{}"); } catch { throw httpError(400, "Bad JSON"); }
}

const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml", ".png": "image/png" };

async function serveStatic(res, file) {
  const full = path.join(STATIC, file);
  if (!full.startsWith(STATIC + path.sep)) throw httpError(404, "Not found");
  const body = await fsp.readFile(full).catch(() => { throw httpError(404, "Not found"); });
  res.writeHead(200, { "Content-Type": TYPES[path.extname(full)] || "application/octet-stream",
    "Cache-Control": file === "index.html" ? "no-cache" : "public, max-age=300" });
  res.end(body);
}

// GET /REA/api/ (also /docs, /help, /openapi.json): every endpoint, so an agent that guesses a
// route gets pointed at the real one. Agents earlier tried test-run, install, openapi.json and docs.
const ENDPOINTS = [
  ["GET", "/health", "service status"],
  ["GET|PUT", "/settings", "the one Master Agent model + thinking level REA uses for all tasks"],
  ["GET", "/rules", "modding rules per engine: hot files to read/patch, libraries to leave alone"],
  ["GET", "/rules/for?folder=ID", "this game's engine rules + which of its files match"], ["PUT|DELETE", "/rules/ENGINE", "edit {focus, skip, notes} / reset to defaults"],
  ["GET", "/targets", "open binaries and game folders"], ["POST", "/targets", "add a target {path} or {folder, rel}"],
  ["GET", "/targets/{target}/inventory", "files of a game folder, ranked (files[].rel = path inside the folder)"],
  ["GET", "/targets/{target}/read?rel=&offset=0&length=4096&format=hex|base64|text", "read-only bytes of a game file (max 1 MiB per call); a folder rel lists it. Game folders are not mounted in your terminal: use this"],
  ["POST", "/targets/{target}/open|close|call", "Ghidra session: open, close, call {tool, args}"], ["DELETE", "/targets/{target}", "remove a target"],
  ["GET", "/files", "agent workspace files you can add"], ["POST", "/uploads, /uploads/{id}, /uploads/{id}/done", "chunked upload"],
  ["GET", "/mods", "mod projects"], ["GET", "/mods/detect?folder={target}", "what engine a game folder uses"], ["POST", "/mods", "new mod {folder, name, goal, recipe}"],
  ["GET", "/mods/{mod}", "project, steps, readiness, last test run"],
  ["POST", "/mods/{mod}/decompile|build|loader", "start that job (one job at a time; poll GET /mods/{mod})"],
  ["GET", "/mods/{mod}/search?q=", "search the mod's decompiled code"],
  ["GET|PUT", "/mods/{mod}/architecture", "the mod's own ARCHITECTURE.md (its design; PUT {text}). The game's architecture is /kb/{key}/architecture"],
  ["GET", "/mods/{mod}/file?path=", "read a file under the mod folder (src/files/..., build.log, runs/{run}/check.log); >2 MB returns the tail"],
  ["POST", "/mods/{mod}/session", "link an Agent session / set stage"], ["POST", "/mods/{mod}/verify", "record an in-game result {state, evidence}"],
  ["POST", "/mods/{mod}/testrun", "build + check + headless run {timeout, by:\"agent\", screenshots:true = run on a virtual display and save screenshots, user runs only: ignored for by:\"agent\"}; unchanged inputs return the earlier run"],
  ["GET", "/mods/{mod}/runs/{run}/shots/{NN}.png", "a test run's screenshot (base-NN.png = the unmodified game); labels in the run's shots[]"],
  ["GET", "/mods/{mod}/runs", "test runs, newest first (use these ids)"], ["GET|DELETE", "/mods/{mod}/runs/{run}", "one run + its log files"],
  ["DELETE", "/mods/{mod}/logs/{name}.log", "delete decompile/build/loader log"], ["GET|DELETE", "/mods/logs?folder={target}", "all logs of a game's mods"],
  ["DELETE", "/mods/{mod}", "delete the project"],
  ["GET", "/kb, /kb/search?q=, /kb/for/{target}", "knowledge bases"],
  ["GET|PUT|POST", "/kb/{key}, /kb/{key}/architecture|search|sessions|notes[/{id}]|validation", "one game's notes and architecture"],
  ["GET|POST|DELETE", "/kb/{key}/decompiled[/binary|file|tree|raw|extract|textures|scene]", "decompiled output and progress"],
  ["GET|POST", "/mods/{mod}/build-check, /mods/rewrites/{rewrite}/build-check", "validate a mod (every file packed, the whole patched game compiles, runs like the original) or a rewrite (parity checklist, compiles, runs); POST starts, GET = progress/last report"],
  ["GET|POST", "/kb/{key}/build-check", "validate the decompiled build: completeness, compile, run vs the original (POST starts, GET = progress/last report)"],
  ["GET", "/library?engine=&q=&kind=", "saved functions: reusable code per engine (mod loaders, hooks, patches)"],
  ["POST", "/library", "save a function {engine, title, kind, summary, usage, language, engineVersion, tags, status, statusNote, source{game,kb,mod,run}, files[{name,text}]}"],
  ["GET|PUT|DELETE", "/library/{id}", "one saved function with its files"],
  ["GET", "/tools?engine=", "engines and tool addons installed in REA from Core Backend (+ a prompt brief); runs only inside REA"],
  ["POST", "/tools/godotpcktool", "GodotPckTool on a mod's game pack {mod, action:list|extract, file?, include?:regex, exclude?, all?, hashes?}; extract writes to rea-mods/{mod}/pck-extract/"],
  ["GET", "/outbox", "queued agent hand-offs"], ["POST", "/outbox/{id}/done", "mark one delivered"],
];
const API_NOTES = [
  "Every POST, PUT and DELETE needs the header X-REA: 1 (curl -H 'X-REA: 1').",
  "Installing or uninstalling a mod uses the REA page or the authorized host play-test helper; this REA API has no install endpoint.",
  "Logs: the current build is GET /mods/{mod}/file?path=build.log; a test run's logs are runs/{run}/build.log|check.log|run.log with ids from GET /mods/{mod}/runs.",
];
const apiIndex = () => ({ endpoints: ENDPOINTS.map(([method, path, what]) => ({ method, path: PREFIX + "/api" + path, what })), notes: API_NOTES });

// The listed route closest to what was asked (ids replaced, punctuation ignored).
function suggestRoute(method, api) {
  const shape = (s) => s.replace(/\?.*$/, "").replace(/\/[0-9a-f]{12}(?=\/|$)/g, "/{}").replace(/\/[0-9TZ]{15}-[0-9a-f]{4}(?=\/|$)/g, "/{}")
    .replace(/\{[a-z]+\}/g, "{}").toLowerCase().replace(/[-_]/g, "");
  const want = shape(api);
  if (method === "GET" && /\/mods\/\{\}\/.*log/.test(want))
    return `Logs are files: GET ${PREFIX}/api/mods/{mod}/file?path=build.log (current build), or path=runs/{run}/build.log|check.log|run.log with ids from GET ${PREFIX}/api/mods/{mod}/runs.`;
  if (/\/mods\/\{\}\/(un)?install/.test(want)) return "Installing is only done by the user on the REA page (Mods → Install). Tell the user the build is ready instead.";
  let best = null, bestScore = 0;
  for (const [m, paths] of ENDPOINTS) for (const one of paths.split(", ")) {
    const variants = one.includes("|") ? (() => {
      const [head, alts] = [one.slice(0, one.lastIndexOf("/") + 1), one.slice(one.lastIndexOf("/") + 1)];
      return alts.includes("|") ? alts.split("|").map((a) => head + a.replace(/\[.*\]$/, "")) : [one];
    })() : [one];
    for (const v of variants) {
      const a = want.split("/"), b = shape(v).split("/");
      let score = 0;
      for (let i = 0; i < Math.min(a.length, b.length); i++) score += a[i] === b[i] ? 2 : (a[i] && b[i] && (a[i].includes(b[i]) || b[i].includes(a[i]))) ? 1 : 0;
      score -= Math.abs(a.length - b.length);
      if (m.split("|").includes(method)) score += 0.5;
      if (score > bestScore) { bestScore = score; best = `${m} ${PREFIX}/api${v}`; }
    }
  }
  return best && bestScore >= 3 ? `Did you mean ${best}?` : "";
}

async function route(req, res) {
  const url = new URL(req.url, "http://local");
  const p = url.pathname;
  if (p === PREFIX || p === PREFIX + "/") return serveStatic(res, "index.html");
  if (p.startsWith(PREFIX + "/static/")) return serveStatic(res, p.slice(PREFIX.length + 8));
  if (p === PREFIX + "/docs" || p === PREFIX + "/api") return send(res, 200, apiIndex());
  if (!p.startsWith(PREFIX + "/api/")) throw httpError(404, "Not found");
  const api = p.slice(PREFIX.length + 4);
  const method = req.method;
  // Every state change needs this header: a cross-site form or link can't send it.
  if (method !== "GET" && method !== "HEAD" && req.headers["x-rea"] !== "1")
    throw httpError(403, "Missing X-REA header: send -H 'X-REA: 1' with every POST, PUT and DELETE (GET needs none).");
  if (method === "GET" && ["", "/", "/docs", "/help", "/openapi.json", "/index"].includes(api)) return send(res, 200, apiIndex());

  if (method === "GET" && api === "/health")
    return send(res, 200, { ok: true, version: VERSION, mcp_bridge: Boolean(gateway),
      sessions: sessions.size, analyzing: [...sessions.values()].filter((s) => s.state === "analyzing").length });

  // The one Master Agent model + thinking level REA uses for all its tasks (model "" = the Agentic
  // settings' model; thinking defaults to medium).
  if (api === "/settings") {
    const file = path.join(DATA, "settings.json");
    if (method === "GET") {
      let settings = {};
      try { settings = JSON.parse(await fsp.readFile(file, "utf8")); } catch {}
      return send(res, 200, { model: reaModel(settings), tasks: MODEL_TASKS });
    }
    if (method === "PUT") {
      const body = await readJson(req);
      const c = body.model || {};
      const reasoning = String(c.reasoning ?? "medium");
      if (!["", "default", "none", "minimal", "low", "medium", "high", "xhigh", "max"].includes(reasoning))
        throw httpError(400, "Invalid thinking level");
      const model = { model: String(c.model || "").slice(0, 200), provider: c.model ? String(c.provider || "").slice(0, 120) : "", reasoning };
      await fsp.mkdir(DATA, { recursive: true });
      await fsp.writeFile(file, JSON.stringify({ model }, null, 1));
      return send(res, 200, { model, tasks: MODEL_TASKS });
    }
  }

  if (method === "GET" && api === "/targets") {
    const folders = Object.values(targets).filter((t) => t.type === "folder");
    const list = Object.values(targets).sort((a, b) => b.added - a.added).map((t) => {
      const out = publicTarget(t);
      if (t.type !== "folder" && !(t.parent && targets[t.parent])) {
        const holder = folders.find((f) => t.path.startsWith(f.path + path.sep));
        if (holder) Object.assign(out, { parent: holder.id, rel: path.relative(holder.path, t.path) });
        else delete out.parent;
      }
      return out;
    });
    return send(res, 200, { version: VERSION, targets: list });
  }

  if (method === "GET" && api === "/files") {
    const rootKey = url.searchParams.get("root") || "workspace";
    if (rootKey === "folders" && !fs.existsSync(ROOTS.folders)) return send(res, 200, { root: rootKey, dir: "", entries: [] });
    const dir = await inside(rootKey, url.searchParams.get("dir") || "");
    const realRoot = await fsp.realpath(ROOTS[rootKey]);
    const entries = [];
    for (const entry of await fsp.readdir(dir, { withFileTypes: true })) {
      if (entry.name.startsWith(".")) continue;
      const full = path.join(dir, entry.name);
      const stat = await fsp.stat(full).catch(() => null);
      if (!stat) continue;
      entries.push({ name: entry.name, dir: stat.isDirectory(), size: stat.size, mtime: stat.mtimeMs,
        rel: path.relative(realRoot, full) });
    }
    entries.sort((a, b) => (b.dir - a.dir) || b.mtime - a.mtime);
    return send(res, 200, { root: rootKey, dir: path.relative(realRoot, dir), entries: entries.slice(0, 500) });
  }

  if (method === "POST" && api === "/targets") {
    const body = await readJson(req);
    let file;
    if (body.folder) {
      // A file inside a folder target: its path must stay inside that folder.
      const parent = targets[String(body.folder)];
      if (!parent || parent.type !== "folder") throw httpError(404, "Unknown folder");
      file = await fsp.realpath(path.join(parent.path, String(body.rel || ""))).catch(() => null);
      if (!file || !file.startsWith(parent.path + path.sep)) throw httpError(404, "Not found");
      body.root = parent.source;
      body.parent = parent;
    } else file = await inside(body.root, body.rel);
    const stat = await fsp.stat(file);
    const folder = stat.isDirectory();
    if (!folder && !stat.isFile()) throw httpError(400, "Pick a file or folder");
    if (folder && file === (await fsp.realpath(ROOTS[body.root]))) throw httpError(400, "Pick a folder inside it");
    const existing = Object.values(targets).find((t) => t.path === file);
    if (existing) return send(res, 200, publicTarget(existing));
    const target = { id: newId(), name: path.basename(file), path: file, size: folder ? 0 : stat.size, source: body.root,
      added: Date.now(), ...(folder ? { type: "folder" } : {}) };
    if (body.parent && !folder) {
      // A file opened from a folder's list sits under that folder in the sidebar, in rank order.
      const rel = path.relative(body.parent.path, file);
      const f = { rel, size: stat.size, ...(await sniff(file, path.basename(file), stat.size)) };
      Object.assign(target, { parent: body.parent.id, rel, kind: f.kind, rank: rankOf({ ...f, ...reInfo(f) },
        await mainNames(body.parent.path, body.parent.name)) });
    }
    targets[target.id] = target;
    await saveTargets();
    return send(res, 201, publicTarget(target));
  }

  if (method === "POST" && api === "/uploads") {
    const body = await readJson(req);
    const size = Number(body.size);
    if (!Number.isSafeInteger(size) || size <= 0 || size > MAX_UPLOAD) throw httpError(400, "File must be 1 byte to 4 GB");
    const id = newId();
    await fsp.mkdir(UPLOADS, { recursive: true });
    const file = path.join(UPLOADS, `.part-${id}`);
    await fsp.writeFile(file, "");
    uploads.set(id, { name: safeName(body.name), size, received: 0, file });
    return send(res, 201, { id });
  }

  let match = api.match(/^\/uploads\/([0-9a-f]{12})(\/done)?$/);
  if (match) {
    const upload = uploads.get(match[1]);
    if (!upload) throw httpError(404, "Unknown upload");
    if (method === "PUT" && !match[2]) {
      if (Number(url.searchParams.get("offset")) !== upload.received) throw httpError(409, `Expected offset ${upload.received}`);
      const out = fs.createWriteStream(upload.file, { flags: "a" });
      let size = 0;
      for await (const chunk of req) {
        size += chunk.length;
        if (upload.received + size > upload.size || size > 16 * 1024 * 1024) { out.destroy(); throw httpError(413, "Chunk too large"); }
        if (!out.write(chunk)) await new Promise((r) => out.once("drain", r));
      }
      await new Promise((resolve, reject) => out.end((error) => (error ? reject(error) : resolve())));
      upload.received += size;
      return send(res, 200, { received: upload.received });
    }
    if (method === "POST" && match[2]) {
      if (upload.received !== upload.size) throw httpError(409, "Upload is incomplete");
      const id = newId();
      const dir = path.join(UPLOADS, id);
      await fsp.mkdir(dir, { recursive: true });
      const file = path.join(dir, upload.name);
      await fsp.rename(upload.file, file);
      uploads.delete(match[1]);
      const target = { id, name: upload.name, path: file, size: upload.size, source: "upload", added: Date.now() };
      targets[id] = target;
      await saveTargets();
      return send(res, 201, publicTarget(target));
    }
    if (method === "DELETE" && !match[2]) {
      await fsp.rm(upload.file, { force: true });
      uploads.delete(match[1]);
      return send(res, 200, { ok: true });
    }
  }

  if (api.startsWith("/kb") && await handleKb({ method, api, url, req, res, send, readJson, httpError, targets })) return;
  if (api.startsWith("/tools") && await handleTools({ method, api, url, req, res, send, readJson, httpError })) return;
  if (api.startsWith("/outbox") && await handleOutbox({ method, api, req, res, send, readJson, httpError })) return;
  if (api.startsWith("/goals/") && await handleGoals({ method, api, req, res, send, readJson, httpError })) return;
  if (api.startsWith("/mods") && await handleProjectCheck({ method, api, res, send, httpError })) return;
  if (api.startsWith("/mods") && await handleTestRuns({ method, api, url, req, res, send, readJson, httpError, targets })) return;
  if (api.startsWith("/library") && await handleLibrary({ method, api, url, req, res, send, readJson, httpError })) return;
  if (api.startsWith("/rules") && await handleRules({ method, api, url, req, res, send, readJson, httpError, targets, detectGame })) return;
  if (api.startsWith("/mods") && await handleMods({ method, api, url, req, res, send, readJson, httpError, targets })) return;

  match = api.match(/^\/targets\/([0-9a-f]{12})(?:\/(open|close|call|inventory|read))?$/);
  if (match) {
    const target = targets[match[1]];
    if (!target) throw httpError(404, "Unknown file");
    const action = match[2];
    if (method === "GET" && !action) return send(res, 200, publicTarget(target));
    if (method === "GET" && action === "inventory") {
      if (target.type !== "folder") throw httpError(400, "Not a folder");
      if (!fs.existsSync(target.path)) throw httpError(410, "The folder is no longer mounted");
      const cached = inventories.get(target.id);
      const withLanguages = async (data) => ({ ...data,
        languages: detectGameLanguages(data.files, await recoveredLanguageFiles(target.path), data.engines) });
      if (cached && !url.searchParams.get("refresh") && Date.now() - cached.at < 10 * 60 * 1000)
        return send(res, 200, await withLanguages(cached.data));
      const data = await inventory(target);
      inventories.set(target.id, { at: Date.now(), data });
      return send(res, 200, await withLanguages(data));
    }
    if (method === "GET" && action === "read") return send(res, 200, await readTargetBytes(target, url.searchParams));
    if (target.type === "folder" && (action === "open" || action === "call")) throw httpError(400, "Open a file inside the folder");
    if (method === "POST" && action === "open") {
      if (!fs.existsSync(target.path)) throw httpError(410, "The file is gone");
      openSession(target);
      return send(res, 200, publicTarget(target));
    }
    if (method === "POST" && action === "close") { closeSession(target.id); return send(res, 200, publicTarget(target)); }
    if (method === "POST" && action === "call") {
      const body = await readJson(req);
      const allowed = TOOLS[body.tool];
      if (!allowed) throw httpError(400, "Unknown question");
      const args = {};
      for (const key of allowed) {
        if (body.args?.[key] === undefined) continue;
        const value = String(body.args[key]);
        if (value.length > 500) throw httpError(400, `${key} is too long`);
        args[key] = value;
      }
      if (args.mode && !["literal", "regex"].includes(args.mode)) throw httpError(400, "Bad mode");
      // A function this version of the file already had decompiled comes from the game's decompiled/ folder.
      const game = body.tool === "procedure_pseudo_code" && args.procedure ? gameOf(target) : null;
      if (game) {
        const text = await readItem(game.game.path, game.rel, "ghidra", args.procedure).catch(() => null);
        if (text !== null) return send(res, 200, { result: text, limitations: [], provider: "ghidra", cached: true });
      }
      const session = sessions.get(target.id);
      if (!session || session.state !== "ready") throw httpError(409, "Open the file first");
      touch(session);
      try {
        const result = await session.proc.call(body.tool, args, 600000);
        const value = result.normalized_result ?? result.result ?? result;
        if (body.tool === "list_procedures" && Array.isArray(value)) {
          session.names = new Map(value.map((p) => [p.address, String(p.value || p.address).replace(/^<EXTERNAL>::/, "")]));
          noteDecomp(target, { total: value.filter((p) => !p.procedure?.external).length, action: "listed functions" });
        }
        if (game && typeof value === "string" && value.trim()) {
          const file = await saveItem(game.game.path, game.rel, "ghidra", args.procedure, value).catch(() => "");
          noteDecomp(target, { items: [{ id: args.procedure, name: session.names?.get(args.procedure) || args.procedure, kind: "function", file }] });
        }
        return send(res, 200, { result: value, limitations: result.limitations || [], provider: result.provider?.name || "" });
      } catch (error) {
        if (session.proc.closed) { session.state = "error"; session.error = error.message; }
        throw httpError(422, error.message);
      }
    }
    if (method === "DELETE" && !action) {
      closeSession(target.id);
      // Uploaded copies are ours to delete; files picked from the agent's folders stay.
      if (target.source === "upload" && target.path.startsWith(UPLOADS + path.sep))
        await fsp.rm(path.dirname(target.path), { recursive: true, force: true });
      delete targets[target.id];
      await saveTargets();
      return send(res, 200, { ok: true });
    }
  }
  const hint = suggestRoute(method, api);
  throw httpError(404, `Not found: ${method} ${PREFIX}/api${api}. ${hint ? hint + " " : ""}GET ${PREFIX}/api/ lists every endpoint.`);
}

const server = http.createServer((req, res) => {
  route(req, res).catch((error) => {
    if (res.headersSent) { res.destroy(); return; }
    send(res, error.status || 500, { detail: error.status ? error.message : "Internal error" });
    if (!error.status) console.error(error);
  });
});
server.requestTimeout = 0;
server.listen(PORT, "0.0.0.0", () => console.log(`REA web on :${PORT}${PREFIX}/, MCP bridge on :${MCP_PORT}/mcp`));
startGateway();

for (const signal of ["SIGTERM", "SIGINT"]) process.on(signal, () => {
  stopping = true;
  for (const id of [...sessions.keys()]) closeSession(id);
  gateway?.kill("SIGTERM");
  server.close();
  setTimeout(() => process.exit(0), 3000).unref();
});
