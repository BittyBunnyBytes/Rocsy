// Decompilation progress per game: what has been decompiled already, by which tool, and where
// the output is, so nobody (the page, a mod project, the Master Agent) decompiles the same thing
// twice or has to guess how far an earlier session got.
//   rea-kb/<KEY>/decompiled/progress.json      the record (binaries → tools → items + a log)
//   rea-kb/<KEY>/decompiled/<rel>/<tool>/...   the output, mirroring the game folder's layout
// A record is tied to the binary's size + mtime: when the game updates, it shows as stale and is
// not reused.
import { createHash } from "node:crypto";
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { extractResources } from "./peres.mjs";
import * as textures from "./textures.mjs";
import * as scene3d from "./scene.mjs";

export const KB = "/opt/data/rocsy-workspace/rea-kb";
const MAX_ITEMS = 20000;
const MAX_LOG = 500;
const MAX_CONTENT = 4 * 1024 * 1024;

export function kbKey(target) {
  const slug = target.name.replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^[._-]+|[._-]+$/g, "").slice(0, 40) || "game";
  return `${slug}-${createHash("sha1").update(target.path).digest("hex").slice(0, 8)}`;
}

export const kbDir = (key) => path.join(KB, key);

export async function ensureKb(target) {
  const key = kbKey(target);
  const dir = kbDir(key);
  await fsp.mkdir(path.join(dir, "notes"), { recursive: true });
  if (!fs.existsSync(path.join(dir, "kb.json"))) {
    await fsp.writeFile(path.join(dir, "kb.json"), JSON.stringify({ key, game: target.name, path: target.path, created: Date.now() }, null, 1));
    await fsp.writeFile(path.join(dir, "ARCHITECTURE.md"), `# ${target.name}: architecture\n\n_Nothing mapped yet. The Master Agent fills this in as it learns how the game is built._\n`);
  }
  return key;
}

// The KB of the game folder a path belongs to.
export const ensureKbForGame = (gamePath) => ensureKb({ name: path.basename(gamePath), path: gamePath });

const rootOf = (key) => path.join(kbDir(key), "decompiled");
const progressFile = (key) => path.join(rootOf(key), "progress.json");
const segment = (s) => s.replace(/[^\w.+@ -]+/g, "_").replace(/^\.+/, "_").slice(0, 120) || "_";
export const TOOL_RE = /^[a-z0-9][a-z0-9-]{0,30}$/;

// "Game_Data/Managed/Assembly-CSharp.dll" (or an absolute path inside the game) → clean relative path.
export function cleanRel(gamePath, rel) {
  let r = String(rel || "").trim().replace(/\\/g, "/");
  if (r.startsWith(gamePath + "/")) r = r.slice(gamePath.length + 1);
  r = path.posix.normalize(r).replace(/^\.\/+/, "");
  if (!r || r === "." || r.startsWith("/") || r.split("/").includes("..") || r.length > 500) return null;
  return r;
}

// Output folder for one binary + tool, relative to decompiled/.
export const outRel = (rel, tool) => [...rel.split("/").map(segment), tool].join("/");
export const outDir = (key, rel, tool) => path.join(rootOf(key), outRel(rel, tool));

async function fingerprint(gamePath, rel) {
  const stat = await fsp.stat(path.join(gamePath, rel)).catch(() => null);
  return stat ? { size: stat.isFile() ? stat.size : 0, mtime: Math.round(stat.mtimeMs), dir: stat.isDirectory() } : null;
}

async function load(key) {
  try {
    const data = JSON.parse(await fsp.readFile(progressFile(key), "utf8"));
    return { binaries: data.binaries || {}, log: Array.isArray(data.log) ? data.log : [] };
  } catch { return { binaries: {}, log: [] }; }
}

// One writer at a time per game (page, mod jobs and the agent's API calls all land here).
const locks = new Map();
function locked(key, fn) {
  const run = (locks.get(key) || Promise.resolve()).then(fn, fn);
  locks.set(key, run.catch(() => {}));
  return run;
}

async function save(key, data) {
  await fsp.mkdir(rootOf(key), { recursive: true });
  const tmp = progressFile(key) + ".tmp";
  await fsp.writeFile(tmp, JSON.stringify(data, null, 1));
  await fsp.rename(tmp, progressFile(key));
}

// Record progress. update = {rel, tool, by, status?, total?, files?, sha256?, format?, project?, note?,
// items?: [{id, name?, kind?, file?}], action?}. Items merge by id; status "complete" marks the tool done.
export function record(gamePath, update) {
  return (async () => {
    const key = await ensureKbForGame(gamePath);
    const rel = cleanRel(gamePath, update.rel);
    if (!rel) throw Object.assign(new Error("rel must be a path inside the game folder"), { status: 400 });
    if (!TOOL_RE.test(update.tool || "")) throw Object.assign(new Error("tool must be a short lowercase name, e.g. ghidra, ilspy"), { status: 400 });
    const fp = await fingerprint(gamePath, rel);
    return locked(key, async () => {
      const data = await load(key);
      const now = Date.now();
      let bin = data.binaries[rel];
      // The game file changed since the old record: start over for this binary.
      if (bin && fp && (bin.size !== fp.size || bin.mtime !== fp.mtime) && !fp.dir) bin = null;
      bin ||= { rel, created: now, tools: {}, items: {} };
      Object.assign(bin, fp ? { size: fp.size, mtime: fp.mtime, dir: fp.dir } : {}, { updated: now });
      if (update.sha256) bin.sha256 = String(update.sha256).slice(0, 64);
      if (update.format) bin.format = String(update.format).slice(0, 60);
      const t = (bin.tools[update.tool] ||= { status: "partial", started: now, out: outRel(rel, update.tool) });
      t.updated = now;
      t.by = String(update.by || "agent").slice(0, 20);
      if (update.status === "complete" || update.status === "partial" || update.status === "failed") t.status = update.status;
      if (Number.isFinite(Number(update.total)) && Number(update.total) > 0) t.total = Math.round(Number(update.total));
      if (Number.isFinite(Number(update.files)) && update.files !== undefined) t.files = Math.round(Number(update.files));
      if (update.project) t.project = String(update.project).slice(0, 20);
      if (update.note !== undefined) t.note = String(update.note).slice(0, 2000);
      let added = 0;
      for (const item of (update.items || []).slice(0, 2000)) {
        const id = String(item.id || item.name || "").slice(0, 200);
        if (!id) continue;
        const k = `${update.tool}:${id}`;
        if (!bin.items[k]) {
          if (Object.keys(bin.items).length >= MAX_ITEMS) continue;
          added++;
        }
        bin.items[k] = { tool: update.tool, id, name: String(item.name || id).slice(0, 300), kind: String(item.kind || "function").slice(0, 30),
          ...(item.file ? { file: String(item.file) } : {}), at: now, by: t.by };
      }
      t.done = Object.values(bin.items).filter((x) => x.tool === update.tool).length;
      if (t.total && t.done >= t.total && t.status === "partial") t.status = "complete";
      data.binaries[rel] = bin;
      const action = update.action || (update.status === "complete" ? "completed" : added ? `+${added} item${added === 1 ? "" : "s"}` : "updated");
      data.log.push({ at: now, rel, tool: update.tool, by: t.by, action, ...(update.items?.length === 1 ? { item: update.items[0].name || update.items[0].id } : {}) });
      if (data.log.length > MAX_LOG) data.log.splice(0, data.log.length - MAX_LOG);
      await save(key, data);
      textures.forget(key);
      return { key, rel, tool: t, out: outRel(rel, update.tool) };
    });
  })();
}

// A finished, still-current decompile of rel with tool, or null.
export async function lookup(gamePath, rel, tool) {
  const key = await ensureKbForGame(gamePath);
  const clean = cleanRel(gamePath, rel);
  if (!clean) return null;
  const bin = (await load(key)).binaries[clean];
  const t = bin?.tools?.[tool];
  if (!t || t.status !== "complete") return null;
  const fp = await fingerprint(gamePath, clean);
  if (!fp || fp.dir || fp.size !== bin.size || fp.mtime !== bin.mtime) return null;
  const dir = outDir(key, clean, tool);
  return fs.existsSync(dir) ? { key, dir, tool: t } : null;
}

// Current recovered source filenames for the game overview. The original file
// fingerprint gates stale output, and generated native pseudocode is excluded.
export async function recoveredLanguageFiles(gamePath) {
  const key = kbKey({ name: path.basename(gamePath), path: gamePath });
  const data = await load(key);
  const out = [];
  const counts = new Map();
  for (const bin of Object.values(data.binaries)) {
    const fp = await fingerprint(gamePath, bin.rel);
    if (!fp || (!fp.dir && (fp.size !== bin.size || fp.mtime !== bin.mtime))) continue;
    for (const [tool, status] of Object.entries(bin.tools || {})) {
      if (status.status === "failed" || ["ghidra", "rizin", "ida", "binary-ninja", "peres", "ue4ss-dump"].includes(tool)) continue;
      const listed = [];
      await walkFiles(outDir(key, bin.rel, tool), "", listed, 20000);
      for (const [rel, size] of listed) {
        const ext = path.extname(rel).slice(1).toLowerCase();
        if (!size || !/^(agc|byc|c|cc|cpp|cxx|cs|fs|gd|go|hx|java|js|cjs|mjs|kt|kts|lua|py|rpy|rpyc|rs|as|swift|ts|tsx|uc|vb)$/.test(ext) || (counts.get(ext) || 0) >= 3) continue;
        counts.set(ext, (counts.get(ext) || 0) + 1);
        out.push({ rel: `${bin.rel}/${rel}`, tool });
      }
    }
  }
  return out;
}

// Copy a tree with hard links (same filesystem: instant, no extra space), falling back to copies.
export async function linkTree(from, to) {
  await fsp.mkdir(to, { recursive: true });
  for (const e of await fsp.readdir(from, { withFileTypes: true })) {
    const a = path.join(from, e.name), b = path.join(to, e.name);
    if (e.isDirectory()) await linkTree(a, b);
    else if (e.isFile()) {
      await fsp.rm(b, { force: true });
      await fsp.link(a, b).catch(() => fsp.copyFile(a, b));
    }
  }
}

// Save one decompiled item's text (e.g. a function's pseudocode) under the binary's tool folder.
export async function saveItem(gamePath, rel, tool, name, text, ext = "c") {
  const key = await ensureKbForGame(gamePath);
  const clean = cleanRel(gamePath, rel);
  const dir = outDir(key, clean, tool);
  await fsp.mkdir(dir, { recursive: true });
  const file = `${segment(name).slice(0, 100)}.${ext}`;
  await fsp.writeFile(path.join(dir, file), text);
  return `${outRel(clean, tool)}/${file}`;
}

// An item's saved text, only while its record is current (an older game version's files that are
// still in the folder are never served).
export async function readItem(gamePath, rel, tool, name) {
  const key = await ensureKbForGame(gamePath);
  const clean = cleanRel(gamePath, rel);
  const bin = clean && (await load(key)).binaries[clean];
  const item = bin?.items?.[`${tool}:${name}`];
  if (!item?.file) return null;
  const fp = await fingerprint(gamePath, clean);
  if (!fp || fp.size !== bin.size || fp.mtime !== bin.mtime) return null;
  const file = path.resolve(rootOf(key), item.file);
  if (!file.startsWith(rootOf(key) + path.sep)) return null;
  return fsp.readFile(file, "utf8").catch(() => null);
}


// ------------------------------------------------------------------ constructed file system --
// The Decompiled tab's explorer: every recorded binary as a folder with its recovered Scripts and
// extracted Assets (plus engine/import leftovers), built from the tools' output folders. Ghidra
// (and other per-item) output is named after the function instead of its address file.
const KINDS = {
  code: "gd cs js mjs cjs ts tsx jsx lua py rpy rb c cc cpp cxx h hpp hxx java kt smali hx as vb fs go rs sh bat cmd ps1 wat il uc php pl moon",
  project: "csproj sln vbproj fsproj godot props targets gradle pom asmdef",
  image: "png jpg jpeg gif webp bmp ico cur svg avif tga dds psd exr hdr ktx ktx2 tif tiff dib pcx",
  audio: "ogg wav mp3 flac opus m4a aac wma xm it mod s3m mid midi bank fsb wem",
  video: "webm mp4 ogv mkv avi mov m4v bik bk2 usm",
  model: "glb gltf obj fbx dae mtl 3ds blend ply stl x md2 md3 mdl smd",
  scene: "tscn scn unity prefab umap tmx tmj map bsp",
  shader: "gdshader shader hlsl glsl vert frag fx cginc compute shadergraph",
  font: "ttf otf woff woff2 fnt fon ttc",
  data: "json xml yaml yml csv tsv txt md ini cfg conf toml tres res po pot lang properties plist html htm css log rvdata2 rxdata",
  engine: "import md5 gdc gde stex ctex oggstr mp3str sample mesh material anim remap pdb meta cache uid",
};
const KIND_OF = new Map(Object.entries(KINDS).flatMap(([k, list]) => list.split(" ").map((e) => [e, k])));
// Godot's import cache (.import/, .godot/imported/) is engine output, whatever the extension.
export const kindOf = (name) => /(^|\/)(\.import|\.godot\/imported)\//.test(name) ? "engine" : KIND_OF.get(path.extname(name).slice(1).toLowerCase()) || "other";
const MAX_TREE = 80000;

async function walkFiles(dir, base, out, cap) {
  let entries;
  try { entries = await fsp.readdir(dir, { withFileTypes: true }); } catch { return; }
  for (const e of entries) {
    if (out.length >= cap) return;
    const full = path.join(dir, e.name), rel = base ? `${base}/${e.name}` : e.name;
    if (e.isDirectory()) await walkFiles(full, rel, out, cap);
    else if (e.isFile()) out.push([rel, (await fsp.stat(full).catch(() => null))?.size ?? 0]);
  }
}

async function buildTree(key, m) {
  const root = rootOf(key);
  const data = await load(key);
  const binaries = [];
  let count = 0, truncated = false;
  for (const bin of Object.values(data.binaries).sort((a, b) => a.rel.localeCompare(b.rel))) {
    const fp = await fingerprint(m.path, bin.rel);
    const files = [];
    const fromItems = new Map();
    for (const item of Object.values(bin.items || {})) if (item.file) fromItems.set(item.file, item);
    const tools = Object.keys(bin.tools);
    const walked = [];
    for (const tool of tools) {
      const out = outRel(bin.rel, tool);
      const list = [];
      await walkFiles(path.join(root, out), "", list, Math.max(0, MAX_TREE - count));
      if (count + list.length >= MAX_TREE) truncated = true;
      count += list.length;
      walked.push([tool, out, list]);
    }
    // A binary's embedded resources get their own folder; other tools' trees get a tool folder only
    // when more than one of them wrote files.
    const prefixed = walked.filter(([tool, , list]) => tool !== "peres" && list.some(([rel]) => !fromItems.has(`${outRel(bin.rel, tool)}/${rel}`))).length > 1;
    const taken = new Set();
    for (const [tool, out, list] of walked) {
      for (const [rel, size] of list) {
        const real = `${out}/${rel}`;
        const item = fromItems.get(real);
        let v;
        if (item) {
          // "Namespace::Class::method" → Functions/Namespace/Class/method.c
          const parts = String(item.name || item.id).replace(/^<EXTERNAL>::/, "").split("::").map(segment);
          v = ["Functions", ...parts].join("/") + path.extname(rel);
          if (taken.has(v)) v = v.replace(/(\.[^./]*)?$/, ` @${segment(item.id)}$1`);
        } else v = tool === "peres" ? `Embedded resources/${rel}` : prefixed ? `${tool}/${rel}` : rel;
        taken.add(v);
        files.push({ v, p: real, s: size, k: item ? "code" : kindOf(rel), t: tool });
      }
    }
    binaries.push({ rel: bin.rel, format: bin.format || "", missing: !fp, stale: Boolean(fp && !fp.dir && (fp.size !== bin.size || fp.mtime !== bin.mtime)),
      tools: Object.fromEntries(Object.entries(bin.tools).map(([name, t]) => [name, { status: t.status, files: t.files, done: t.done, total: t.total, out: t.out }])),
      files });
  }
  return { key, dir: root, game: m.path, binaries, truncated };
}

// Files are served from decompiled/ only after resolving links (tool output from game data could
// contain a symlink pointing anywhere).
export async function containedFile(root, rel) {
  const file = path.resolve(root, String(rel || ""));
  if (!file.startsWith(root + path.sep)) return null;
  const real = await fsp.realpath(file).catch(() => null);
  const realRoot = await fsp.realpath(root).catch(() => root);
  return real && real.startsWith(realRoot + path.sep) ? real : null;
}

const MIME = {
  png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp", bmp: "image/bmp", dib: "image/bmp",
  ico: "image/x-icon", cur: "image/x-icon", svg: "image/svg+xml", avif: "image/avif",
  ogg: "audio/ogg", oga: "audio/ogg", opus: "audio/ogg", wav: "audio/wav", mp3: "audio/mpeg", flac: "audio/flac", m4a: "audio/mp4", aac: "audio/aac",
  webm: "video/webm", mp4: "video/mp4", m4v: "video/mp4", ogv: "video/ogg",
  ttf: "font/ttf", otf: "font/otf", woff: "font/woff", woff2: "font/woff2",
  glb: "model/gltf-binary", gltf: "model/gltf+json",
};

export async function sendRaw(req, res, file, url) {
  const stat = await fsp.stat(file);
  const ext = path.extname(file).slice(1).toLowerCase();
  const type = MIME[ext] || (["code", "project", "data", "scene", "shader"].includes(kindOf(file)) || ext === "obj" || ext === "mtl" ? "text/plain; charset=utf-8" : "application/octet-stream");
  const headers = { "Content-Type": type, "Accept-Ranges": "bytes", "Cache-Control": "private, max-age=300",
    "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'",
    "Content-Disposition": `${url.searchParams.get("download") ? "attachment" : "inline"}; filename*=UTF-8''${encodeURIComponent(path.basename(file))}` };
  let start = 0, end = stat.size - 1, status = 200;
  const range = String(req.headers.range || "").match(/^bytes=(\d*)-(\d*)$/);
  if (range && stat.size) {
    if (range[1]) { start = Number(range[1]); if (range[2]) end = Math.min(Number(range[2]), end); }
    else if (range[2]) start = Math.max(0, stat.size - Number(range[2]));
    if (start > end || start >= stat.size) { res.writeHead(416, { "Content-Range": `bytes */${stat.size}` }); res.end(); return; }
    status = 206;
    headers["Content-Range"] = `bytes ${start}-${end}/${stat.size}`;
  }
  headers["Content-Length"] = String(stat.size ? end - start + 1 : 0);
  res.writeHead(status, headers);
  if (req.method === "HEAD" || !stat.size) { res.end(); return; }
  fs.createReadStream(file, { start, end }).on("error", () => res.destroy()).pipe(res);
}

// Asset extraction a binary can get from REA itself (more tools can join here).
const extracting = new Set();
async function extractAssets(gamePath, rel, by) {
  const file = path.join(gamePath, rel);
  const real = await fsp.realpath(file).catch(() => null);
  const realGame = await fsp.realpath(gamePath).catch(() => gamePath);
  if (!real || !real.startsWith(realGame + path.sep)) throw Object.assign(new Error("Not a file in this game folder"), { status: 404 });
  const head = Buffer.alloc(2);
  const fh = await fsp.open(real, "r");
  try { await fh.read(head, 0, 2, 0); } finally { await fh.close(); }
  if (head.toString("latin1") !== "MZ") throw Object.assign(new Error("Only Windows .exe/.dll resources can be extracted here; engine packs are unpacked by the Mods decompile step"), { status: 400 });
  const key = await ensureKbForGame(gamePath);
  if (extracting.has(`${key}:${rel}`)) throw Object.assign(new Error("Already extracting this file"), { status: 409 });
  extracting.add(`${key}:${rel}`);
  try {
    await record(gamePath, { rel, tool: "peres", by, status: "partial", action: "extracting resources" });
    let result;
    try { result = await extractResources(real, outDir(key, rel, "peres")); }
    catch (error) {
      await record(gamePath, { rel, tool: "peres", by, status: "failed", note: error.message, action: "failed" });
      throw Object.assign(new Error(`Couldn't read the resources: ${error.message}`), { status: 422 });
    }
    const summary = Object.entries(result.counts).map(([k, n]) => `${n} ${k}`).join(", ") || "no resources";
    await record(gamePath, { rel, tool: "peres", by, status: "complete", files: result.files, note: `PE resources: ${summary}`, action: "extracted resources" });
    return { ...result, summary };
  } finally { extracting.delete(`${key}:${rel}`); }
}

// ------------------------------------------------------------------------------- routes --
// /kb/<key>/decompiled[/binary|/file]; m = the KB's kb.json ({path} = the game folder).
export async function handleDecompiled({ key, m, sub, method, url, req, res, send, readJson, httpError }) {
  const root = rootOf(key);
  if (!sub && method === "GET") {
    const data = await load(key);
    const binaries = await Promise.all(Object.values(data.binaries).map(async ({ items, ...b }) => {
      const fp = await fingerprint(m.path, b.rel);
      return { ...b, items: Object.keys(items || {}).length, missing: !fp, stale: Boolean(fp && !fp.dir && (fp.size !== b.size || fp.mtime !== b.mtime)) };
    }));
    binaries.sort((a, b) => b.updated - a.updated);
    send(res, 200, { key, dir: root, game: m.path, binaries, log: data.log.slice(-80).reverse() });
    return true;
  }
  if (sub === "/binary" && method === "GET") {
    const rel = cleanRel(m.path, url.searchParams.get("rel"));
    const all = (await load(key)).binaries;
    const bin = rel && all[rel];
    if (!bin) {
      const known = Object.keys(all);
      throw httpError(404, `Nothing recorded for ${JSON.stringify(url.searchParams.get("rel") || "")} yet. ` + (known.length
        ? `Files with records: ${known.slice(0, 20).join(", ")}${known.length > 20 ? ` (+${known.length - 20} more)` : ""}. REL is the path inside the game folder.`
        : "Nothing has been decompiled for this game yet."));
    }
    const fp = await fingerprint(m.path, rel);
    const items = Object.values(bin.items || {}).sort((a, b) => a.tool.localeCompare(b.tool) || a.name.localeCompare(b.name));
    send(res, 200, { ...bin, items, dirs: Object.fromEntries(Object.keys(bin.tools).map((t) => [t, outDir(key, rel, t)])),
      missing: !fp, stale: Boolean(fp && !fp.dir && (fp.size !== bin.size || fp.mtime !== bin.mtime)) });
    return true;
  }
  if (sub === "/tree" && method === "GET") {
    send(res, 200, await buildTree(key, m));
    return true;
  }
  if (sub === "/raw" && (method === "GET" || method === "HEAD")) {
    const file = await containedFile(root, url.searchParams.get("path"));
    const stat = file && await fsp.stat(file).catch(() => null);
    if (!stat?.isFile()) throw httpError(404, "Not found");
    await sendRaw(req, res, file, url);
    return true;
  }
  if (sub === "/scene" && method === "GET") {
    // Godot scene → flattened 3D world for "Explore in 3D" (scene.mjs).
    send(res, 200, await scene3d.build(key, root, () => buildTree(key, m), String(url.searchParams.get("path") || "")));
    return true;
  }
  if (sub === "/textures" || sub === "/textures/unresolved") {
    // 3D model → texture links (textures.mjs): automatic ones + what the user / agent saved.
    const getTree = () => buildTree(key, m);
    if (sub === "/textures/unresolved" && method === "GET") { send(res, 200, await textures.unresolved(key, root, getTree)); return true; }
    if (method === "GET") { send(res, 200, await textures.candidates(key, root, getTree, String(url.searchParams.get("path") || ""))); return true; }
    if (method === "POST") { send(res, 200, await textures.save(key, root, getTree, await readJson(req), locked)); return true; }
    if (method === "DELETE") { send(res, 200, await textures.save(key, root, getTree, { model: url.searchParams.get("path"), clear: true }, locked)); return true; }
  }
  if (sub === "/extract" && method === "POST") {
    const body = await readJson(req);
    const rel = cleanRel(m.path, body.rel);
    if (!rel) throw httpError(400, "rel must be a path inside the game folder");
    send(res, 200, await extractAssets(m.path, rel, body.by === "agent" ? "agent" : "page"));
    return true;
  }
  if (sub === "/file" && method === "GET") {
    const file = await containedFile(root, url.searchParams.get("path"));
    if (!file) throw httpError(404, "Not found");
    const stat = await fsp.stat(file).catch(() => null);
    if (stat?.isDirectory()) {
      const entries = (await fsp.readdir(file, { withFileTypes: true })).slice(0, 2000)
        .map((e) => ({ name: e.name, dir: e.isDirectory() })).sort((a, b) => (b.dir - a.dir) || a.name.localeCompare(b.name));
      send(res, 200, { path: path.relative(await fsp.realpath(root), file), entries });
      return true;
    }
    if (!stat?.isFile() || stat.size > 2 * 1024 * 1024) throw httpError(404, "Not a readable text file");
    send(res, 200, { path: path.relative(await fsp.realpath(root), file), text: await fsp.readFile(file, "utf8") });
    return true;
  }
  if (!sub && method === "POST") {
    // The agent records what it decompiled; items may carry their text (written into the folder).
    const body = await readJson(req, 16 * 1024 * 1024);
    const rel = cleanRel(m.path, body.rel);
    if (!rel) throw httpError(400, "rel must be a path inside the game folder");
    if (!TOOL_RE.test(String(body.tool || ""))) throw httpError(400, "tool must be a short lowercase name, e.g. ghidra, ilspy");
    const items = [];
    for (const item of Array.isArray(body.items) ? body.items.slice(0, 2000) : []) {
      const id = String(item?.id || item?.name || "");
      if (!id) continue;
      let file = item.file ? String(item.file) : "";
      if (typeof item.content === "string") {
        if (item.content.length > MAX_CONTENT) throw httpError(413, `${id}: content too large`);
        const ext = /^[a-z0-9]{1,6}$/.test(String(item.ext || "")) ? item.ext : "c";
        file = await saveItem(m.path, rel, body.tool, id, item.content, ext);
      }
      items.push({ id, name: item.name, kind: item.kind, file });
    }
    send(res, 200, await record(m.path, { ...body, rel, items, by: body.by === "user" ? "user" : "agent" }));
    return true;
  }
  if (!sub && method === "DELETE") {
    // Forget one binary (or one tool of it) and delete its output.
    const rel = cleanRel(m.path, url.searchParams.get("rel"));
    const tool = url.searchParams.get("tool") || "";
    if (!rel) throw httpError(400, "Bad rel");
    await locked(key, async () => {
      const data = await load(key);
      const bin = data.binaries[rel];
      if (!bin) return;
      for (const t of tool ? [tool] : Object.keys(bin.tools)) {
        if (!TOOL_RE.test(t)) continue;
        await fsp.rm(outDir(key, rel, t), { recursive: true, force: true });
        delete bin.tools[t];
        for (const k of Object.keys(bin.items)) if (k.startsWith(`${t}:`)) delete bin.items[k];
      }
      if (!Object.keys(bin.tools).length) delete data.binaries[rel];
      data.log.push({ at: Date.now(), rel, tool: tool || "all", by: "user", action: "forgotten" });
      await save(key, data);
    });
    send(res, 200, { ok: true });
    return true;
  }
  return false;
}
