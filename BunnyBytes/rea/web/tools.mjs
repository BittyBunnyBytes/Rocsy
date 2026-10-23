// Installed REA engines and tool addons (Models → Manager → Core Backend → REA Engines / REA Tool
// Addons), so the agent hears about what is actually installed, when it is relevant to the game.
//
//   GET  /tools[?engine=godot]       installed runtimes + addons, and a one-paragraph brief for prompts
//   POST /tools/godotpcktool         {mod, action: list|extract, file?, include?, exclude?, all?, hashes?}
//
// Core Backend installs into a host folder mounted read-only at ENGINES/portable (also at /official
// and /godotsteam for the test runner); test runs cache their own downloads directly in ENGINES.
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { MODS } from "./mods.mjs";

const TEST = process.env.REA_TEST_DIR || "/home/node/rea-test";
const ENGINES = path.join(TEST, "engines");
const MANAGED = path.join(ENGINES, "portable");
const PCK_CACHE = path.join(TEST, "pck-cache");
const API = "http://127.0.0.1:8976/REA/api";
const INSTALL_HINT = "the user installs it in Models → Manager → Core Backend → REA Tool Addons";

// What each addon is for and how the agent reaches it (it runs in REA, not in the agent's terminal).
const ADDONS = {
  godotpcktool: {
    engines: ["godot"],
    use: "lists, extracts and hashes res:// files of a Godot .pck or an executable with an embedded pack",
    how: `POST ${API}/tools/godotpcktool {"mod":"<mod id>","action":"list"|"extract","include":"<regex>"} ` +
      `(X-REA: 1; file defaults to the mod's pack; extract writes to the mod's pck-extract/ folder)`,
  },
};

const readJson = async (file) => { try { return JSON.parse(await fsp.readFile(file, "utf8")); } catch { return null; } };
const dirs = async (dir) => (await fsp.readdir(dir, { withFileTypes: true }).catch(() => []))
  .filter((d) => d.isDirectory() && !d.name.startsWith(".")).map((d) => d.name).sort();
const isFile = (p) => { try { return fs.statSync(p).isFile(); } catch { return false; } };

export async function listTools() {
  const tools = [];
  for (const name of await dirs(MANAGED)) {
    const dir = path.join(MANAGED, name);
    let m;
    if ((m = name.match(/^godot-(\d+\.\d+(?:\.\d+)?)$/))) {
      const bin = (await fsp.readdir(dir).catch(() => [])).find((f) => /^Godot_v.*-stable_linux/.test(f));
      if (bin) tools.push({ id: name, kind: "runtime", label: `Godot ${m[1]} (official)`, version: m[1], engines: ["godot"],
        path: path.join(dir, bin), use: `test runs and build checks use it automatically for Godot ${m[1]} games` });
    } else if ((m = name.match(/^godotsteam-(.+)$/))) {
      if (isFile(path.join(dir, "linux-352-editor.64")))
        tools.push({ id: name, kind: "runtime", label: `GodotSteam ${m[1]}`, version: m[1], engines: ["godot"],
          path: path.join(dir, "linux-352-editor.64"), use: "test runs use it automatically for games that need GodotSteam" });
    } else if ((m = name.match(/^(love2d|tic80)-(.+)$/))) {
      const man = await readJson(path.join(dir, "manifest.json"));
      if (man?.binary && isFile(path.join(dir, man.binary)))
        tools.push({ id: m[1], kind: "runtime", label: `${m[1] === "love2d" ? "LÖVE" : "TIC-80"} ${m[2]}`, version: m[2],
          engines: ["generic"], path: path.join(dir, man.binary),
          use: m[1] === "love2d" ? "runtime for Lua games and .love packages (not used by test runs)" : "runtime for TIC-80 cartridges (not used by test runs)" });
    }
  }
  for (const name of await dirs(path.join(MANAGED, "addons"))) {
    const m = name.match(/^([a-z0-9]+)-(.+)$/);
    const dir = path.join(MANAGED, "addons", name);
    const man = m && await readJson(path.join(dir, "manifest.json"));
    if (!man?.binary || !isFile(path.join(dir, man.binary))) continue;
    const info = ADDONS[m[1]] || { engines: [], use: "", how: "" };
    tools.push({ id: m[1], kind: "addon", label: `${m[1] === "godotpcktool" ? "GodotPckTool" : m[1]} ${m[2]}`, version: m[2],
      engines: info.engines, path: path.join(dir, man.binary), use: info.use, how: info.how });
  }
  // Runtimes a test run downloaded by itself (not managed by Core Backend).
  for (const name of await dirs(ENGINES)) {
    const m = name.match(/^godot-(\d+\.\d+(?:\.\d+)?)$/);
    if (m && !tools.some((t) => t.id === name))
      tools.push({ id: name, kind: "runtime", label: `Godot ${m[1]} (test-run cache)`, version: m[1], engines: ["godot"],
        path: path.join(ENGINES, name), use: `test runs use it automatically for Godot ${m[1]} games` });
  }
  return tools;
}

// One paragraph for agent prompts: only what is installed AND relevant to this engine (all when none given).
export function toolsBrief(tools, engine = "") {
  const relevant = tools.filter((t) => !engine || t.engines.includes(engine));
  if (!relevant.length) return "";
  const line = (t) => `${t.label}: ${t.use}${t.how ? `; use it through ${t.how}` : ""}`;
  return `Installed in REA${engine ? ` for this ${engine} game` : ""} (Core Backend): ${relevant.map(line).join(" | ")}. ` +
    `These run inside REA, not in your terminal; don't download your own copies. Current list: GET ${API}/tools${engine ? `?engine=${engine}` : ""}.`;
}

// ------------------------------------------------------------------------------ GodotPckTool --
let pckBusy = false;

// GodotPckTool reads standalone packs only. For an executable with an embedded pack, make a cached
// file it can read. Godot 3 (pack format 1) stores absolute offsets inside the executable, so the
// cache is a sparse file with the pack directory at 0 and the file data at its original offsets;
// later formats store offsets relative to the pack, so the pack is cut out as is.
async function standalonePack(file) {
  const fd = await fsp.open(file, "r");
  try {
    const { size: total, mtimeMs } = await fd.stat();
    const head = Buffer.alloc(4);
    await fd.read(head, 0, 4, 0);
    if (head.toString("latin1") === "GDPC") return file;
    const tail = Buffer.alloc(12);
    if (total < 12) throw new Error("not a Godot pack");
    await fd.read(tail, 0, 12, total - 12);
    if (tail.subarray(8).toString("latin1") !== "GDPC") throw new Error("no Godot pack in this file (no GDPC header or embedded-pack trailer)");
    const size = Number(tail.readBigUInt64LE(0));
    const start = total - 12 - size;
    const header = Buffer.alloc(8);
    await fd.read(header, 0, 8, start);
    if (start < 0 || header.subarray(0, 4).toString("latin1") !== "GDPC") throw new Error("embedded pack trailer points at no pack");
    const format = header.readUInt32LE(4);
    const tag = createHash("sha1").update(file).digest("hex").slice(0, 16);
    const out = path.join(PCK_CACHE, `${tag}-${createHash("sha1").update(`${total}:${mtimeMs}`).digest("hex").slice(0, 12)}.pck`);
    if (isFile(out)) return out;
    await fsp.mkdir(PCK_CACHE, { recursive: true });
    for (const old of await fsp.readdir(PCK_CACHE)) if (old.startsWith(tag + "-")) await fsp.rm(path.join(PCK_CACHE, old), { force: true });
    const tmp = `${out}.${process.pid}.tmp`;
    const copy = async (from, length, to) => {
      const dest = await fsp.open(tmp, fs.existsSync(tmp) ? "r+" : "w");
      try {
        const buf = Buffer.alloc(1 << 20);
        for (let done = 0; done < length;) {
          const { bytesRead } = await fd.read(buf, 0, Math.min(buf.length, length - done), from + done);
          if (!bytesRead) throw new Error("short read while copying the embedded pack");
          await dest.write(buf, 0, bytesRead, to + done);
          done += bytesRead;
        }
      } finally { await dest.close(); }
    };
    try {
      if (format === 1) {
        // The directory ends before the first file's data; if it is longer than the executable part in
        // front of the pack it gets cut off and GodotPckTool reports the pack as broken.
        await copy(start, size, start);
        await copy(start, Math.min(size, start), 0);
      } else {
        await copy(start, size, 0);
      }
      await fsp.rename(tmp, out);
    } catch (error) { await fsp.rm(tmp, { force: true }); throw error; }
    return out;
  } finally { await fd.close(); }
}

function run(bin, args, timeoutMs = 300000) {
  return new Promise((resolve) => {
    const child = spawn(bin, args, { stdio: ["ignore", "pipe", "pipe"] });
    let out = "", err = "";
    child.stdout.on("data", (d) => { if (out.length < 8 << 20) out += d; });
    child.stderr.on("data", (d) => { if (err.length < 1 << 20) err += d; });
    const timer = setTimeout(() => child.kill("SIGKILL"), timeoutMs);
    child.on("error", (e) => { clearTimeout(timer); resolve({ code: -1, out, err: String(e.message) }); });
    child.on("close", (code) => { clearTimeout(timer); resolve({ code, out, err }); });
  });
}

const regex = (value, name, httpError) => {
  if (value === undefined || value === "") return "";
  if (typeof value !== "string" || value.length > 300) throw httpError(400, `${name} must be a regex string of at most 300 characters`);
  try { new RegExp(value); } catch { throw httpError(400, `${name} is not a valid regex`); }
  return value;
};

async function godotPckTool(body, httpError) {
  const tool = (await listTools()).find((t) => t.id === "godotpcktool");
  if (!tool) throw httpError(409, `GodotPckTool is not installed: ${INSTALL_HINT}.`);
  if (!/^[0-9a-f]{12}$/.test(String(body.mod || ""))) throw httpError(400, "mod (12-hex mod project id) is required");
  const project = await readJson(path.join(MODS, body.mod, "project.json"));
  if (!project) throw httpError(404, "Unknown mod project");
  const action = body.action || "list";
  if (!["list", "extract"].includes(action)) throw httpError(400, "action is list or extract");
  const rel = String(body.file || project.pckRel || "");
  if (!rel) throw httpError(400, "file (path inside the game folder) is required: this project has no Godot pack");
  const game = await fsp.realpath(project.gamePath);
  const file = await fsp.realpath(path.resolve(game, rel)).catch(() => null);
  if (!file || !file.startsWith(game + path.sep) || !isFile(file)) throw httpError(400, `file ${rel} is not a file inside the game folder`);
  const include = regex(body.include, "include", httpError), exclude = regex(body.exclude, "exclude", httpError);
  if (action === "extract" && !include && body.all !== true)
    throw httpError(400, "extract needs include (a regex of res:// paths, e.g. \"^res://Scenes/Player/\") or all:true");
  if (pckBusy) throw httpError(409, "GodotPckTool is already running; try again when it finishes");
  pckBusy = true;
  try {
    const pack = await standalonePack(file).catch((e) => { throw httpError(422, `${rel}: ${e.message}`); });
    const filters = [...(include ? ["-i", include] : []), ...(exclude ? ["-e", exclude] : [])];
    if (action === "list") {
      const r = await run(tool.path, ["-p", pack, "-a", "list", ...filters, ...(body.hashes ? ["--print-hashes"] : [])]);
      if (r.code !== 0) throw httpError(502, `GodotPckTool failed (${r.code}): ${(r.err || r.out).slice(-600)}`);
      const lines = r.out.split("\n");
      const version = lines.find((l) => l.startsWith("Pck version"))?.trim() || "";
      const files = lines.filter((l) => l.startsWith("res://")).map((l) => {
        const m = l.match(/^(res:\/\/.*?) size: (\d+)(?:.*?(?:md5|hash)[^0-9a-f]*([0-9a-f]{32,}))?/i);
        return m ? { path: m[1], size: Number(m[2]), ...(m[3] ? { md5: m[3] } : {}) } : { path: l.trim() };
      });
      return { pack: rel, embedded: pack !== file, version, total: files.length, truncated: files.length > 2000, files: files.slice(0, 2000) };
    }
    const out = path.join(MODS, project.id, "pck-extract");
    await fsp.mkdir(out, { recursive: true });
    const r = await run(tool.path, ["-p", pack, "-a", "extract", "-o", out, "-q", ...filters]);
    const written = [];
    const walk = async (dir) => {
      for (const d of await fsp.readdir(dir, { withFileTypes: true }).catch(() => [])) {
        const p = path.join(dir, d.name);
        if (d.isDirectory()) await walk(p); else written.push(path.relative(out, p).split(path.sep).join("/"));
      }
    };
    await walk(out);
    if (r.code !== 0) throw httpError(502, `GodotPckTool failed (${r.code}): ${(r.err || r.out).slice(-600)}`);
    return { pack: rel, embedded: pack !== file, folder: out, files: written.length, sample: written.slice(0, 50),
      note: "pck-extract/ is a read-only reference copy of the game's files (exported/imported formats as packed). " +
        "Copy what you change into src/files/<res path>; never put .import/ data there." };
  } finally { pckBusy = false; }
}

export async function handleTools({ method, api, url, req, res, send, readJson: readBody, httpError }) {
  if (method === "GET" && api === "/tools") {
    const engine = String(url.searchParams.get("engine") || "").replace(/[^a-z0-9-]/g, "");
    const tools = await listTools();
    return send(res, 200, { tools: engine ? tools.filter((t) => t.engines.includes(engine)) : tools, brief: toolsBrief(tools, engine),
      install: INSTALL_HINT }), true;
  }
  if (method === "POST" && api === "/tools/godotpcktool")
    return send(res, 200, await godotPckTool(await readBody(req), httpError)), true;
  return false;
}
