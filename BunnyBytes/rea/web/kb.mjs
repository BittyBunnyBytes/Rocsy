// Per-game knowledge base: ARCHITECTURE.md + notes/*.md in the agent's workspace (rea-kb/<key>/),
// written by the Master Agent (and the user) so later and long-running sessions start from what
// earlier ones learned. Retrieval (the "RAG") is BM25 over chunks of the notes, the architecture
// and the game's decompiled C#, with code-aware tokens (timeScale -> time, scale, timescale).
// No embedding model: nothing to load on the GPU the studios share, and identifiers match exactly.
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { MODS, modReadiness } from "./mods.mjs";
import { KB, kbKey, ensureKb, handleDecompiled } from "./decomp.mjs";
import { validateProject } from "./validate.mjs";
import { handleBuildCheck } from "./buildcheck.mjs";

export { KB, kbKey, ensureKb };
export { tokens, buildIndex };

const MAX_NOTE = 200 * 1024;
const MAX_CODE_CHUNKS = 60000;
const STOP = new Set("the a an and or of to in on for is are be it this that with as by at from was were if then else not no we you i our can will should into out up so do does done has have had its their there here when what which who how get set new var let return public private static void class using namespace true false null".split(" "));
const indexes = new Map(); // key -> {sig, docs, df, avg} for notes+architecture+mod architectures
const codeIndexes = new Map(); // key -> {sig, docs, df, avg}

const dirOf = (key) => path.join(KB, key);
const validKey = (key) => /^[A-Za-z0-9._-]{1,40}-[0-9a-f]{8}$/.test(key);

async function meta(key) {
  try { return JSON.parse(await fsp.readFile(path.join(dirOf(key), "kb.json"), "utf8")); } catch { return null; }
}

// --------------------------------------------------------------------------- note files --
// A note is Markdown with a small front matter block; files the agent writes by hand without
// one still work (title = first heading or file name).
function parseNote(id, raw, stat) {
  let body = raw, fields = {};
  const match = raw.match(/^---\n([\s\S]*?)\n---\n?/);
  if (match) {
    body = raw.slice(match[0].length);
    for (const line of match[1].split("\n")) {
      const kv = line.match(/^(\w+):\s*(.*)$/);
      if (kv) fields[kv[1]] = kv[2].trim();
    }
  }
  const heading = body.match(/^#\s+(.+)$/m);
  return { id, title: fields.title || heading?.[1]?.trim() || id, tags: (fields.tags || "").split(",").map((t) => t.trim()).filter(Boolean),
    author: fields.author || "agent", kind: fields.kind || "note", created: Number(fields.created) || stat.birthtimeMs || stat.mtimeMs,
    updated: stat.mtimeMs, body };
}

function serializeNote(note) {
  const clean = (v) => String(v || "").replace(/\n/g, " ").trim();
  return `---\ntitle: ${clean(note.title)}\ntags: ${note.tags.map(clean).join(", ")}\nauthor: ${clean(note.author)}\nkind: ${clean(note.kind)}\ncreated: ${note.created}\n---\n${note.body.trimEnd()}\n`;
}

async function listNotes(key) {
  const dir = path.join(dirOf(key), "notes");
  const notes = [];
  for (const name of await fsp.readdir(dir).catch(() => [])) {
    if (!name.endsWith(".md")) continue;
    const file = path.join(dir, name);
    const [raw, stat] = await Promise.all([fsp.readFile(file, "utf8").catch(() => null), fsp.stat(file).catch(() => null)]);
    if (raw === null || !stat) continue;
    notes.push(parseNote(name.slice(0, -3), raw, stat));
  }
  return notes.sort((a, b) => b.updated - a.updated);
}

const noteFile = (key, id) => {
  if (!/^[A-Za-z0-9._-]{1,80}$/.test(id)) return null;
  return path.join(dirOf(key), "notes", `${id}.md`);
};

const slugId = (title) => `${String(title).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 50) || "note"}-${randomBytes(2).toString("hex")}`;

// ------------------------------------------------------------------------------- search --
// Light suffix stemming so "prefabs"/"spawning"/"spawned" meet "prefab"/"spawn".
function stem(word) {
  const t = suffix(word);
  // "pause"/"paused"/"pausing" all end up as "paus".
  return t.length > 4 && t.endsWith("e") ? t.slice(0, -1) : t;
}

function suffix(t) {
  if (t.length > 5 && t.endsWith("ing")) return t.slice(0, -3);
  if (t.length > 4 && t.endsWith("ies")) return t.slice(0, -3) + "y";
  if (t.length > 4 && (t.endsWith("es") && /(sh|ch|x|ss)es$/.test(t))) return t.slice(0, -2);
  if (t.length > 4 && t.endsWith("ed")) return t.slice(0, -2);
  if (t.length > 3 && t.endsWith("s") && !t.endsWith("ss")) return t.slice(0, -1);
  return t;
}

function tokens(text) {
  const out = [];
  const spaced = text.replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2");
  for (const t of spaced.toLowerCase().split(/[^a-z0-9]+/)) if (t.length > 1 && !STOP.has(t)) out.push(stem(t));
  // Whole identifiers too, so "timeScale" also matches as one word.
  for (const t of text.match(/[A-Za-z_][A-Za-z0-9_]{3,}/g) || []) {
    const lower = t.toLowerCase().replace(/_/g, "");
    if (/[A-Z_]/.test(t.slice(1)) && !STOP.has(lower)) out.push(lower);
  }
  return out;
}

function buildIndex(docs) {
  const df = new Map();
  let total = 0;
  for (const doc of docs) {
    const tf = new Map();
    for (const t of tokens(`${doc.title}\n${doc.title}\n${doc.text}`)) tf.set(t, (tf.get(t) || 0) + 1);
    doc.tf = tf;
    doc.len = [...tf.values()].reduce((a, b) => a + b, 0);
    total += doc.len;
    for (const t of tf.keys()) df.set(t, (df.get(t) || 0) + 1);
  }
  return { docs, df, avg: docs.length ? total / docs.length : 1 };
}

function rank(index, query, k) {
  const terms = [...new Set(tokens(query))];
  const n = index.docs.length;
  const scored = [];
  for (const doc of index.docs) {
    let score = 0;
    for (const t of terms) {
      const f = doc.tf.get(t);
      if (!f) continue;
      const df = index.df.get(t) || 0;
      const idf = Math.log(1 + (n - df + 0.5) / (df + 0.5));
      score += idf * (f * 2.2) / (f + 1.2 * (0.25 + 0.75 * doc.len / index.avg));
    }
    if (score > 0) scored.push({ doc, score: score * (doc.boost || 1) });
  }
  return scored.sort((a, b) => b.score - a.score).slice(0, k).map(({ doc, score }) => ({
    source: doc.source, kind: doc.kind, title: doc.title, score: Number(score.toFixed(3)), line: doc.line || null,
    text: snippet(doc.text, terms) }));
}

function snippet(text, terms, size = 700) {
  if (text.length <= size) return text;
  const lower = text.toLowerCase();
  let at = -1;
  for (const t of terms) { const i = lower.indexOf(t); if (i >= 0 && (at < 0 || i < at)) at = i; }
  const start = Math.max(0, Math.min(text.length - size, at < 0 ? 0 : at - 200));
  return (start ? "…" : "") + text.slice(start, start + size) + (start + size < text.length ? "…" : "");
}

// Markdown into heading-sized pieces (long sections are split again on blank lines).
function chunkMarkdown(text, base) {
  const chunks = [];
  let heading = base.title, buf = [];
  const flush = () => {
    const body = buf.join("\n").trim();
    buf = [];
    if (!body) return;
    for (let i = 0; i < body.length; i += 1500) chunks.push({ ...base, title: heading, text: body.slice(i, i + 1600) });
  };
  for (const line of text.split("\n")) {
    const h = line.match(/^#{1,4}\s+(.+)/);
    if (h) { flush(); heading = h[1].trim() === base.title ? base.title : `${base.title} › ${h[1].trim()}`; }
    buf.push(line);
  }
  flush();
  return chunks;
}

// Each mod project of this game keeps its own ARCHITECTURE.md (the mod's design); the game's
// ARCHITECTURE.md is only about the game itself.
export async function modArchitectures(gamePath, withText = false) {
  const out = [];
  for (const id of await fsp.readdir(MODS).catch(() => [])) {
    if (!/^[0-9a-f]{12}$/.test(id)) continue;
    const proj = await fsp.readFile(path.join(MODS, id, "project.json"), "utf8").then(JSON.parse).catch(() => null);
    if (!proj || proj.gamePath !== gamePath) continue;
    const file = path.join(MODS, id, "ARCHITECTURE.md");
    const stat = await fsp.stat(file).catch(() => null);
    if (!stat) continue;
    out.push({ mod: id, name: proj.name, updated: stat.mtimeMs, file, ...(withText ? { text: await fsp.readFile(file, "utf8") } : {}) });
  }
  return out;
}

async function kbIndex(key) {
  const notes = await listNotes(key);
  const archFile = path.join(dirOf(key), "ARCHITECTURE.md");
  const archStat = await fsp.stat(archFile).catch(() => null);
  const m = await meta(key);
  const mods = m ? await modArchitectures(m.path) : [];
  const sig = notes.map((n) => `${n.id}:${n.updated}`).join("|") + `|arch:${archStat?.mtimeMs}|` + mods.map((x) => `${x.mod}:${x.updated}`).join("|");
  const cached = indexes.get(key);
  if (cached?.sig === sig) return cached;
  const docs = [];
  if (archStat) docs.push(...chunkMarkdown(await fsp.readFile(archFile, "utf8"),
    { source: "ARCHITECTURE.md", kind: "architecture", title: "Architecture", boost: 1.3 }));
  for (const note of notes)
    docs.push(...chunkMarkdown(note.body, { source: `notes/${note.id}.md`, kind: note.kind === "session" ? "session" : "note",
      title: note.title, boost: note.author === "user" ? 1.2 : 1 }));
  for (const x of mods)
    docs.push(...chunkMarkdown(await fsp.readFile(x.file, "utf8").catch(() => ""),
      { source: `rea-mods/${x.mod}/ARCHITECTURE.md`, kind: "mod-architecture", title: `Mod ${x.name}`, boost: 0.9 }));
  const index = { sig, ...buildIndex(docs) };
  indexes.set(key, index);
  return index;
}

// Source the mod projects extracted (C#, UE4SS Lua/C++ headers, GDScript, JS, Ren'Py…).
const CODE_EXT = /\.(cs|lua|hpp|h|c|gd|js|rpy|py|tscn)$/i;

// The game's decompiled/extracted code, in ~60-line pieces: the newest mod project's decompiled/
// plus the knowledge base's own decompiled/ folder (Ghidra pseudocode, the agent's saved output).
async function codeIndex(key, gamePath) {
  const projects = [];
  for (const id of await fsp.readdir(MODS).catch(() => [])) {
    try {
      const p = JSON.parse(await fsp.readFile(path.join(MODS, id, "project.json"), "utf8"));
      if (p.gamePath === gamePath && p.steps?.decompile?.state === "done") projects.push(p);
    } catch {}
  }
  const best = projects.sort((a, b) => b.steps.decompile.finished - a.steps.decompile.finished)[0];
  const progress = await fsp.stat(path.join(dirOf(key), "decompiled", "progress.json")).catch(() => null);
  if (!best && !progress) return null;
  const sig = `${best ? `${best.id}:${best.steps.decompile.finished}` : ""}|${progress?.mtimeMs || 0}`;
  const cached = codeIndexes.get(key);
  if (cached?.sig === sig) return cached;
  const roots = [];
  if (best) roots.push([path.join(MODS, best.id, "decompiled"), `rea-mods/${best.id}/decompiled`]);
  // The KB copy of a mod's ILSpy output is the same code: index the KB folder only when no mod project covers it.
  if (progress) roots.push([path.join(dirOf(key), "decompiled"), `rea-kb/${key}/decompiled`, best ? /\/ilspy(\/|$)/ : null]);
  const docs = [];
  for (const [root, label, skip] of roots) {
    const stack = [root];
    while (stack.length && docs.length < MAX_CODE_CHUNKS) {
      const d = stack.pop();
      for (const e of await fsp.readdir(d, { withFileTypes: true }).catch(() => [])) {
        const full = path.join(d, e.name);
        if (e.isDirectory()) { if (!skip || !skip.test(full)) stack.push(full); continue; }
        if (!CODE_EXT.test(e.name)) continue;
        const stat = await fsp.stat(full).catch(() => null);
        if (!stat || stat.size > 4 * 1024 * 1024) continue;
        const lines = (await fsp.readFile(full, "utf8").catch(() => "")).split("\n");
        const rel = path.relative(root, full);
        for (let i = 0; i < lines.length && docs.length < MAX_CODE_CHUNKS; i += 50) {
          const text = lines.slice(i, i + 60).join("\n");
          if (!text.trim()) continue;
          const decl = text.match(/(?:class|struct|interface|enum|class_name)\s+(\w+)|(?:void|bool|int|float|string|IEnumerator|[A-Z]\w+)\s+(\w+)\s*\(|(?:func|function|def|label)\s+([\w.:]+)/);
          docs.push({ source: `${label}/${rel}`, kind: "code", line: i + 1, boost: 0.8,
            title: `${rel.replace(/\.(cs|lua|hpp|h|gd|js|rpy|py|c)$/, "")}${decl ? ` · ${decl[1] || decl[2] || decl[3]}` : ""}`, text });
        }
      }
    }
  }
  const index = { sig, ...buildIndex(docs) };
  codeIndexes.set(key, index);
  return index;
}

export async function searchKb(key, query, { k = 8, scopes = ["notes", "architecture", "mods", "code"] } = {}) {
  const m = await meta(key);
  if (!m) return { results: [], code: false };
  const results = [];
  const kb = await kbIndex(key);
  const wanted = (r) => scopes.includes(r.kind === "architecture" ? "architecture" : r.kind === "mod-architecture" ? "mods" : "notes");
  results.push(...rank(kb, query, k * 2).filter(wanted));
  let code = null;
  if (scopes.includes("code")) {
    code = await codeIndex(key, m.path);
    if (code) results.push(...rank(code, query, k));
  }
  results.sort((a, b) => b.score - a.score);
  return { results: results.slice(0, k), code: Boolean(code), chunks: kb.docs.length + (code?.docs.length || 0) };
}

// ------------------------------------------------------------------------------- routes --
const SESSION_KINDS = ["mod", "rewrite", "ask", "map", "decomp", "test", "textures"];

// Record an Agent session REA's server started for a game (e.g. via the outbox after a failed test run).
export async function addSession(gamePath, { id, kind, label = "", mod = "" }) {
  const key = await ensureKb({ name: path.basename(gamePath), path: gamePath });
  const file = path.join(dirOf(key), "sessions.json");
  let log = [];
  try { const saved = JSON.parse(await fsp.readFile(file, "utf8")); if (Array.isArray(saved)) log = saved; } catch {}
  if (log.some((x) => x.id === id && !x.removed)) return;
  log = [...log.filter((x) => x.id !== id), { id, kind: SESSION_KINDS.includes(kind) ? kind : "ask", label: String(label).slice(0, 200),
    mod: String(mod).slice(0, 20), created: Date.now() }];
  await fsp.writeFile(file, JSON.stringify(log, null, 1));
}

export async function handleKb({ method, api, url, req, res, send, readJson, httpError, targets }) {
  let match = api.match(/^\/kb\/for\/([0-9a-f]{12})$/);
  if (match && method === "GET") {
    const target = targets[match[1]];
    if (!target || target.type !== "folder") throw httpError(404, "Unknown game folder");
    send(res, 200, { key: await ensureKb(target), path: dirOf(kbKey(target)) });
    return true;
  }
  if (api === "/kb" && method === "GET") {
    const keys = await fsp.readdir(KB).catch(() => []);
    send(res, 200, { kbs: (await Promise.all(keys.map(meta))).filter(Boolean) });
    return true;
  }
  if (api === "/kb/search" && method === "GET") {
    // Every game's notes at once (no code), for when the agent doesn't know which game it needs.
    const q = String(url.searchParams.get("q") || "");
    const all = [];
    for (const key of await fsp.readdir(KB).catch(() => []))
      if (validKey(key)) all.push(...(await searchKb(key, q, { k: 5, scopes: ["notes", "architecture"] })).results.map((r) => ({ ...r, kb: key })));
    send(res, 200, { results: all.sort((a, b) => b.score - a.score).slice(0, 10) });
    return true;
  }
  // Decompilation progress + the decompiled/ folder (decomp.mjs).
  // "Validate build": is the decompiled output complete, does it compile and run (buildcheck.mjs).
  match = api.match(/^\/kb\/([A-Za-z0-9._-]+)\/build-check$/);
  if (match && validKey(match[1]) && (method === "GET" || method === "POST")) {
    const m = await meta(match[1]);
    if (!m) throw httpError(404, "Unknown knowledge base");
    return handleBuildCheck({ key: match[1], m, method, res, send, httpError });
  }
  match = api.match(/^\/kb\/([A-Za-z0-9._-]+)\/validation$/);
  if (match && validKey(match[1]) && method === "GET") {
    const m = await meta(match[1]);
    if (!m) throw httpError(404, "Unknown knowledge base");
    const projects = [];
    for (const id of await fsp.readdir(MODS).catch(() => [])) {
      if (!/^[0-9a-f]{12}$/.test(id)) continue;
      try {
        const p = JSON.parse(await fsp.readFile(path.join(MODS, id, "project.json"), "utf8"));
        if (p.gamePath === m.path && ["done", "partial"].includes(p.steps?.decompile?.state)) projects.push(p);
      } catch {}
    }
    const project = projects.sort((a, b) => (b.steps.decompile.finished || 0) - (a.steps.decompile.finished || 0))[0];
    if (!project) { send(res, 200, { project: "", checked: 0, unsupported: 0, languages: {}, parsers: {}, total: 0,
      diagnostics: [], message: "Decompile a mod project before checking source syntax." }); return true; }
    send(res, 200, await validateProject(path.join(MODS, project.id), dirOf(match[1])));
    return true;
  }
  match = api.match(/^\/kb\/([A-Za-z0-9._-]+)\/decompiled(\/binary|\/file|\/tree|\/raw|\/extract|\/textures|\/textures\/unresolved|\/scene)?$/);
  if (match && validKey(match[1])) {
    const m = await meta(match[1]);
    if (!m) throw httpError(404, "Unknown knowledge base");
    return handleDecompiled({ key: match[1], m, sub: match[2] || "", method, url, req, res, send, readJson, httpError });
  }
  match = api.match(/^\/kb\/([A-Za-z0-9._-]+)(\/architecture|\/search|\/sessions|\/notes(?:\/([A-Za-z0-9._-]+))?)?$/);
  if (!match || !validKey(match[1])) return false;
  const key = match[1];
  const m = await meta(key);
  if (!m) throw httpError(404, "Unknown knowledge base");
  const dir = dirOf(key);
  const part = match[2] || "";

  if (!part && method === "GET") {
    const arch = path.join(dir, "ARCHITECTURE.md");
    const stat = await fsp.stat(arch).catch(() => null);
    const notes = (await listNotes(key)).map(({ body, ...n }) => ({ ...n, preview: body.replace(/^#.*$/m, "").trim().slice(0, 220) }));
    const mods = (await modArchitectures(m.path)).map(({ file, ...x }) => ({ ...x, path: file }));
    send(res, 200, { ...m, dir, architecture: stat ? await fsp.readFile(arch, "utf8") : "", architectureUpdated: stat?.mtimeMs || 0, notes,
      modArchitectures: mods });
    return true;
  }
  if (part === "/search" && method === "GET") {
    const q = String(url.searchParams.get("q") || "").trim();
    if (q.length < 2) throw httpError(400, "Search for at least 2 characters");
    const scopes = String(url.searchParams.get("scope") || "notes,architecture,mods,code").split(",");
    const k = Math.min(30, Math.max(1, Number(url.searchParams.get("k")) || 8));
    send(res, 200, await searchKb(key, q, { k, scopes }));
    return true;
  }
  // Master Agent sessions REA started for this game: its own log plus every mod project's session.
  if (part === "/sessions") {
    const file = path.join(dir, "sessions.json");
    let log = [];
    try {
      const saved = JSON.parse(await fsp.readFile(file, "utf8"));
      if (Array.isArray(saved)) log = saved.filter((x) => x && typeof x.id === "string");
    } catch {}
    if (method === "POST") {
      const body = await readJson(req);
      const id = String(body.id || "");
      if (!/^[A-Za-z0-9_-]{6,80}$/.test(id)) throw httpError(400, "Bad session id");
      // Keep a tombstone so a deleted Agent session linked by a mod project stays removed.
      if (body.remove) log = [...log.filter((x) => x.id !== id), { id, removed: true }];
      else if (!log.some((x) => x.id === id && !x.removed))
        log = [...log.filter((x) => x.id !== id), { id, kind: SESSION_KINDS.includes(body.kind) ? body.kind : "ask",
          label: String(body.label || "").slice(0, 200), mod: String(body.mod || "").slice(0, 20), created: Date.now() }];
      await fsp.writeFile(file, JSON.stringify(log, null, 1));
      send(res, 200, { ok: true });
      return true;
    }
    if (method === "GET") {
      const removed = new Set(log.filter((x) => x.removed).map((x) => x.id));
      const all = new Map(log.filter((x) => !x.removed).map((x) => [x.id, x]));
      for (const id of await fsp.readdir(MODS).catch(() => [])) {
        try {
          const proj = JSON.parse(await fsp.readFile(path.join(MODS, id, "project.json"), "utf8"));
          if (proj.gamePath !== m.path || !proj.session || removed.has(proj.session)) continue;
          all.set(proj.session, { ...(all.get(proj.session) || { created: proj.created }), id: proj.session, kind: "mod",
            label: `Mod: ${proj.name}`, mod: proj.id, stage: proj.stage || "" });
        } catch {}
      }
      const sessions = await Promise.all([...all.values()].map(async (record) => {
        if (!record.mod || !/^[0-9a-f]{12}$/.test(record.mod)) return { ...record, readiness: null };
        const project = await fsp.readFile(path.join(MODS, record.mod, "project.json"), "utf8")
          .then(JSON.parse).catch(() => null);
        return { ...record, readiness: project ? await modReadiness(project) : null };
      }));
      send(res, 200, { sessions: sessions.sort((a, b) => (Number(b.created) || 0) - (Number(a.created) || 0)) });
      return true;
    }
  }
  if (part === "/architecture" && method === "GET") {
    // Listed in the API index; agents tried it and got a 404 (2026-10-16).
    const arch = path.join(dir, "ARCHITECTURE.md");
    const stat = await fsp.stat(arch).catch(() => null);
    send(res, 200, { key, game: m.game, text: stat ? await fsp.readFile(arch, "utf8") : "", updated: stat?.mtimeMs || 0 });
    return true;
  }
  if (part === "/architecture" && method === "PUT") {
    const body = await readJson(req, MAX_NOTE + 4096);
    const text = String(body.text || "");
    if (text.length > MAX_NOTE) throw httpError(413, "Too long");
    await fsp.writeFile(path.join(dir, "ARCHITECTURE.md"), text.endsWith("\n") ? text : text + "\n");
    send(res, 200, { ok: true });
    return true;
  }
  if (part === "/notes" && method === "POST") {
    const body = await readJson(req, MAX_NOTE + 4096);
    const note = { title: String(body.title || "").trim().slice(0, 140) || "Untitled", body: String(body.body || "").slice(0, MAX_NOTE),
      tags: Array.isArray(body.tags) ? body.tags.map(String).slice(0, 12) : String(body.tags || "").split(",").map((t) => t.trim()).filter(Boolean),
      author: body.author === "user" ? "user" : "agent", kind: body.kind === "session" ? "session" : "note", created: Date.now() };
    const id = slugId(note.title);
    await fsp.writeFile(noteFile(key, id), serializeNote(note));
    send(res, 201, { id, ...note });
    return true;
  }
  const id = match[3];
  if (id) {
    const file = noteFile(key, id);
    if (!file) throw httpError(400, "Bad note id");
    const raw = await fsp.readFile(file, "utf8").catch(() => null);
    if (raw === null) throw httpError(404, "Unknown note");
    const current = parseNote(id, raw, await fsp.stat(file));
    if (method === "GET") { send(res, 200, current); return true; }
    if (method === "PUT") {
      const body = await readJson(req, MAX_NOTE + 4096);
      const next = { ...current, title: body.title !== undefined ? String(body.title).slice(0, 140) : current.title,
        body: body.body !== undefined ? String(body.body).slice(0, MAX_NOTE) : current.body,
        tags: body.tags !== undefined ? (Array.isArray(body.tags) ? body.tags.map(String) : String(body.tags).split(",").map((t) => t.trim()).filter(Boolean)) : current.tags,
        author: body.author === "user" || body.author === "agent" ? body.author : current.author };
      await fsp.writeFile(file, serializeNote(next));
      send(res, 200, { id, ...next });
      return true;
    }
    if (method === "DELETE") { await fsp.unlink(file); send(res, 200, { ok: true }); return true; }
  }
  return false;
}
