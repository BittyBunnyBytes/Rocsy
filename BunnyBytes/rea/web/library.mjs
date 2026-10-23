// Saved functions: reusable code the Master Agent (or the user) proved on one game and can reuse on
// the next game of the same engine: mod loaders, hook/patch helpers, autoload injectors, UI bits,
// build tricks. One folder per entry in the agent's workspace so the agent can also copy files
// straight into a mod's src/:
//   rea-library/<engine>/<id>/entry.json   title, kind, summary, usage, status, source game/mod...
//   rea-library/<engine>/<id>/files/<name> the code, as many files as the piece needs
import { randomBytes } from "node:crypto";
import fsp from "node:fs/promises";
import path from "node:path";
import { tokens, buildIndex } from "./kb.mjs";

export const LIBRARY = "/opt/data/rocsy-workspace/rea-library";
export const ENGINES = { "unity-mono": "Unity (Mono)", "unity-il2cpp": "Unity (IL2CPP)", unreal: "Unreal Engine", godot: "Godot",
  rpgmaker: "RPG Maker", renpy: "Ren'Py", electron: "Electron / NW.js", generic: "Other / custom engine" };
export const KINDS = ["mod-loader", "hook", "patch", "ui", "utility", "build", "other"];
const STATUSES = ["works", "untested", "broken"];
const MAX_FILE = 256 * 1024, MAX_FILES = 20, MAX_TEXT = 64 * 1024;

const clip = (v, n) => String(v ?? "").slice(0, n);
const line = (v, n) => clip(v, n).replace(/[\r\n]+/g, " ").trim();
const validId = (id) => /^[a-z0-9-]{3,64}$/.test(id);
const tagList = (v) => (Array.isArray(v) ? v : String(v || "").split(",")).map((t) => line(t, 40)).filter(Boolean).slice(0, 16);

// Paths inside files/: relative, no "..", plain characters (rocsy/loader.gd, Plugin.cs).
function cleanName(name) {
  const n = String(name || "").replace(/\\/g, "/").replace(/^\/+/, "").trim();
  if (!n || n.length > 160 || !/^[A-Za-z0-9._\/ -]+$/.test(n) || n.split("/").some((p) => !p || p === "." || p === "..")) return null;
  return n;
}

async function readEntry(dir) {
  try { return JSON.parse(await fsp.readFile(path.join(dir, "entry.json"), "utf8")); } catch { return null; }
}

async function allEntries() {
  const out = [];
  for (const engine of Object.keys(ENGINES))
    for (const id of await fsp.readdir(path.join(LIBRARY, engine)).catch(() => [])) {
      if (!validId(id)) continue;
      const dir = path.join(LIBRARY, engine, id);
      const entry = await readEntry(dir);
      if (entry) out.push({ ...entry, id, engine, dir });
    }
  return out.sort((a, b) => (b.updated || 0) - (a.updated || 0));
}

async function findEntry(id) {
  if (!validId(id)) return null;
  for (const engine of Object.keys(ENGINES)) {
    const dir = path.join(LIBRARY, engine, id);
    const entry = await readEntry(dir);
    if (entry) return { ...entry, id, engine, dir };
  }
  return null;
}

async function readFiles(entry) {
  const files = [];
  for (const name of entry.files || []) {
    const text = await fsp.readFile(path.join(entry.dir, "files", name), "utf8").catch(() => null);
    if (text !== null) files.push({ name, text });
  }
  return files;
}

function fields(body, current, httpError) {
  const next = { ...current };
  if (body.title !== undefined) next.title = line(body.title, 140);
  if (!next.title) throw httpError(400, "Give the function a title");
  for (const [key, n] of [["summary", 600], ["engineVersion", 80], ["language", 40]])
    if (body[key] !== undefined) next[key] = line(body[key], n);
  if (body.usage !== undefined) next.usage = clip(body.usage, MAX_TEXT);
  if (body.kind !== undefined) next.kind = KINDS.includes(body.kind) ? body.kind : "other";
  if (body.tags !== undefined) next.tags = tagList(body.tags);
  if (body.status !== undefined) {
    if (!STATUSES.includes(body.status)) throw httpError(400, `status is one of ${STATUSES.join(", ")}`);
    next.status = body.status;
  }
  if (body.statusNote !== undefined) next.statusNote = clip(body.statusNote, 2000);
  if (body.source !== undefined && typeof body.source === "object" && body.source)
    next.source = { game: line(body.source.game, 120), kb: line(body.source.kb, 60), mod: line(body.source.mod, 20), run: line(body.source.run, 40) };
  return next;
}

function cleanFiles(list, httpError) {
  if (!Array.isArray(list)) throw httpError(400, "files is a list of {name, text}");
  if (list.length > MAX_FILES) throw httpError(400, `At most ${MAX_FILES} files`);
  const seen = new Set();
  return list.map((f) => {
    const name = cleanName(f?.name);
    if (!name) throw httpError(400, `Bad file name: ${f?.name}. Use a relative path like loader.gd or rocsy/hook.gd`);
    if (seen.has(name)) throw httpError(400, `Duplicate file ${name}`);
    seen.add(name);
    const text = String(f.text ?? "");
    if (Buffer.byteLength(text) > MAX_FILE) throw httpError(413, `${name} is over 256 KB`);
    return { name, text };
  });
}

async function writeFiles(dir, files) {
  await fsp.rm(path.join(dir, "files"), { recursive: true, force: true });
  for (const f of files) {
    const file = path.join(dir, "files", f.name);
    await fsp.mkdir(path.dirname(file), { recursive: true });
    await fsp.writeFile(file, f.text);
  }
}

async function save(entry) {
  const { dir, id, ...rest } = entry;
  await fsp.mkdir(dir, { recursive: true });
  await fsp.writeFile(path.join(dir, "entry.json.tmp"), JSON.stringify({ id, ...rest }, null, 1));
  await fsp.rename(path.join(dir, "entry.json.tmp"), path.join(dir, "entry.json"));
}

const listed = ({ dir, usage, ...e }) => ({ ...e, path: dir, hasUsage: Boolean(usage) });

// BM25 over title/tags/summary/usage (counted twice) plus the code, so "autoload inject" or
// "HarmonyPatch Update" find the right piece.
async function searchEntries(entries, q) {
  const docs = await Promise.all(entries.map(async (e) => {
    const code = (await readFiles(e)).map((f) => `${f.name}\n${f.text}`).join("\n").slice(0, 40000);
    const words = `${e.title}\n${(e.tags || []).join(" ")}\n${e.kind}\n${e.summary || ""}\n${e.usage || ""}`;
    return { entry: e, title: e.title, text: `${words}\n${words}\n${code}` };
  }));
  const index = buildIndex(docs);
  const terms = [...new Set(tokens(q))];
  const n = docs.length;
  return docs.map((doc) => {
    let score = 0;
    for (const t of terms) {
      const f = doc.tf.get(t);
      if (!f) continue;
      const df = index.df.get(t) || 0;
      score += Math.log(1 + (n - df + 0.5) / (df + 0.5)) * (f * 2.2) / (f + 1.2 * (0.25 + 0.75 * doc.len / index.avg));
    }
    return { entry: doc.entry, score: score * (doc.entry.status === "works" ? 1.2 : doc.entry.status === "broken" ? 0.5 : 1) };
  }).filter((r) => r.score > 0).sort((a, b) => b.score - a.score).map((r) => ({ ...r.entry, score: Number(r.score.toFixed(3)) }));
}

// Routes under /REA/api/library. Returns true when it handled the request.
export async function handleLibrary({ method, api, url, req, res, send, readJson, httpError }) {
  if (api === "/library" && method === "GET") {
    const engine = url.searchParams.get("engine") || "";
    if (engine && !ENGINES[engine]) throw httpError(400, `Unknown engine. One of: ${Object.keys(ENGINES).join(", ")}`);
    const kind = url.searchParams.get("kind") || "";
    const q = String(url.searchParams.get("q") || "").trim();
    const all = await allEntries();
    const counts = Object.fromEntries(Object.keys(ENGINES).map((e) => [e, 0]));
    for (const e of all) counts[e.engine]++;
    let entries = all.filter((e) => (!engine || e.engine === engine) && (!kind || e.kind === kind));
    if (q.length >= 2) entries = await searchEntries(entries, q);
    send(res, 200, { root: LIBRARY, kinds: KINDS, engines: Object.entries(ENGINES).map(([id, label]) => ({ id, label, count: counts[id] })),
      entries: entries.map(listed) });
    return true;
  }
  if (api === "/library" && method === "POST") {
    const body = await readJson(req, MAX_FILES * MAX_FILE + MAX_TEXT + 8192);
    if (!ENGINES[body.engine]) throw httpError(400, `engine is one of: ${Object.keys(ENGINES).join(", ")} (the mod project's engine)`);
    const files = cleanFiles(body.files || [], httpError);
    if (!files.length) throw httpError(400, "Save at least one file of code ({name, text})");
    const slug = line(body.title, 140).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 48) || "function";
    const id = `${slug}-${randomBytes(2).toString("hex")}`;
    const entry = fields(body, { id, engine: body.engine, dir: path.join(LIBRARY, body.engine, id), kind: "other", tags: [],
      summary: "", usage: "", engineVersion: "", language: "", status: "untested", statusNote: "", source: {},
      author: body.author === "user" ? "user" : "agent", created: Date.now() }, httpError);
    entry.files = files.map((f) => f.name);
    entry.updated = Date.now();
    await save(entry);
    await writeFiles(entry.dir, files);
    send(res, 201, { ...listed(entry), usage: entry.usage, files });
    return true;
  }
  const match = api.match(/^\/library\/([a-z0-9-]+)$/);
  if (!match) return false;
  const entry = await findEntry(match[1]);
  if (!entry) throw httpError(404, `Unknown saved function ${match[1]}: GET /library lists the ids`);
  if (method === "GET") { send(res, 200, { ...listed(entry), usage: entry.usage || "", files: await readFiles(entry) }); return true; }
  if (method === "PUT") {
    const body = await readJson(req, MAX_FILES * MAX_FILE + MAX_TEXT + 8192);
    // The user's own entries are theirs: the agent may only add a status/evidence to them.
    if (entry.author === "user" && body.by !== "user") {
      const allowed = ["status", "statusNote", "by"];
      const extra = Object.keys(body).filter((k) => !allowed.includes(k));
      if (extra.length) throw httpError(403, `This function was saved by the user: only status/statusNote can be changed (not ${extra.join(", ")}). Save your version as a new entry.`);
    }
    const next = fields(body, entry, httpError);
    if (body.files !== undefined) {
      const files = cleanFiles(body.files, httpError);
      if (!files.length) throw httpError(400, "Keep at least one file");
      await writeFiles(entry.dir, files);
      next.files = files.map((f) => f.name);
    }
    next.updated = Date.now();
    await save(next);
    send(res, 200, { ...listed(next), usage: next.usage || "", files: await readFiles(next) });
    return true;
  }
  if (method === "DELETE") {
    await fsp.rm(entry.dir, { recursive: true, force: true });
    send(res, 200, { ok: true });
    return true;
  }
  return false;
}
