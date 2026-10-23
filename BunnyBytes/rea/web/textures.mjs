// Texture associations for the 3D models in a game's decompiled/ folder, for the Decompiled tab's
// model viewer. Found automatically where the game says so:
//   - Godot scenes (.tscn/.escn) that use the mesh, through their material slots / overrides to the
//     material (.tres or inline) and its albedo texture / colour (shader materials: texture params)
//   - an OBJ's .mtl (map_Kd / Kd per material)
//   - same-name images next to it (a guess, labelled as one)
// glTF/GLB textures embedded in the file are read by the page itself. Links the user picks or the
// Master Agent finds are kept in decompiled/textures.json and win over the automatic ones.
import fsp from "node:fs/promises";
import path from "node:path";

const IMAGE = /\.(png|jpe?g|webp|gif|bmp|tga|dds|pcx)$/i;     // tga/dds/pcx are decoded by the page
const MODEL = /\.(glb|gltf|obj|fbx|dae|3ds|ply|stl|x|md2|md3|mdl|smd)$/i;
const CACHE_MS = 60000;
const cache = new Map();

const saveFile = (root) => path.join(root, "textures.json");
export async function loadSaved(root) {
  try { const d = JSON.parse(await fsp.readFile(saveFile(root), "utf8")); return d && typeof d.models === "object" ? d : { models: {} }; }
  catch { return { models: {} }; }
}

// ---------------------------------------------------------------- Godot text resources --
const attrs = (s) => Object.fromEntries([...s.matchAll(/(\w+)=("([^"]*)"|[^\s\]]+)/g)].map((m) => [m[1], m[3] ?? m[2]]));
const refOf = (v, kind) => String(v || "").match(new RegExp(`${kind}\\(\\s*"?([^")\\s]+)"?\\s*\\)`))?.[1];

export function parseGodot(text) {
  const ext = new Map(), subs = new Map(), nodes = [];
  let resource = null, cur = null;
  for (const line of text.split("\n")) {
    const head = line.match(/^\[(\w+)\s*(.*?)\]\s*$/);
    if (head) {
      const a = attrs(head[2]);
      cur = { props: {}, ...a };
      if (head[1] === "ext_resource" && a.id) ext.set(String(a.id), { path: a.path || "", type: a.type || "" });
      else if (head[1] === "sub_resource" && a.id) subs.set(String(a.id), cur);
      else if (head[1] === "node") nodes.push(cur);
      else if (head[1] === "resource") resource = cur;
      if (head[1] === "ext_resource") cur = null;
      continue;
    }
    const prop = cur && line.match(/^([\w/]+)\s*=\s*(.+)$/);
    if (prop) cur.props[prop[1]] = prop[2].trim();
  }
  return { ext, subs, nodes, resource };
}

const COLOR = (v) => {
  const m = String(v || "").match(/Color\(\s*([-\d.e]+)\s*,\s*([-\d.e]+)\s*,\s*([-\d.e]+)\s*(?:,\s*([-\d.e]+))?\s*\)/);
  return m ? [+m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4]] : null;
};
const TEX_PARAM = /albedo|diffuse|base|main|colou?r|tex/i;

// ------------------------------------------------------------------------------- index --
// Per knowledge base: the images and models of every binary, plus every Godot scene that uses a mesh.
async function readText(file, max = 4 * 1024 * 1024) {
  const stat = await fsp.stat(file).catch(() => null);
  if (!stat?.isFile() || stat.size > max) return null;
  const buf = await fsp.readFile(file);
  return buf.subarray(0, 4096).includes(0) ? null : buf.toString("utf8");
}

// getTree() = decomp.mjs buildTree, only called when the cached index is too old.
export async function index(key, root, getTree) {
  const hit = cache.get(key);
  if (hit && Date.now() - hit.at < CACHE_MS) return hit;
  const tree = await getTree();
  const files = new Map();               // p → {p, v, k, bin, out}
  const outs = [];                       // tool output roots (res:// base for Godot output)
  for (const bin of tree.binaries) {
    for (const [, t] of Object.entries(bin.tools)) if (t.out) outs.push(t.out);
    for (const f of bin.files) files.set(f.p, { ...f, bin: bin.rel });
  }
  const resBase = (p) => outs.filter((o) => p.startsWith(o + "/")).sort((a, b) => b.length - a.length)[0] || "";
  const uses = new Map();                // model p → [{scene, surfaces, texture?, note}]
  const parsed = new Map();
  const parse = async (p) => {
    if (!parsed.has(p)) parsed.set(p, readText(path.join(root, p)).then((t) => t === null ? null : parseGodot(t)).catch(() => null));
    return parsed.get(p);
  };
  const resolveRes = (from, res) => {
    if (!res) return null;
    if (res.startsWith("res://")) return `${resBase(from)}/${res.slice(6)}`;
    return path.posix.normalize(path.posix.join(path.posix.dirname(from), res));
  };
  // A texture reference → an image path the page can show (follows one .tres hop, e.g. AtlasTexture).
  const textureOf = async (from, doc, value) => {
    const id = refOf(value, "ExtResource");
    const e = id && doc.ext.get(id);
    const p = e && resolveRes(from, e.path);
    if (!p) return null;
    if (IMAGE.test(p) && files.has(p)) return p;
    if (/\.tres$/i.test(p)) {
      const sub = await parse(p);
      for (const v of Object.values(sub?.resource?.props || {})) {
        const t = await textureOf(p, sub, v);
        if (t) return t;
      }
    }
    return null;
  };
  // A material reference (ExtResource .tres / SubResource) → {texture?, color?, material}.
  const materialOf = async (from, doc, value) => {
    let res = null, rdoc = doc, rfrom = from, name = "";
    const sid = refOf(value, "SubResource");
    const eid = refOf(value, "ExtResource");
    if (sid) { res = doc.subs.get(sid); name = `${path.posix.basename(from)} #${sid}`; }
    else if (eid) {
      const e = doc.ext.get(eid);
      const p = e && resolveRes(from, e.path);
      if (!p) return null;
      name = path.posix.basename(p);
      if (IMAGE.test(p)) return files.has(p) ? { texture: p, material: name } : null;
      rdoc = await parse(p);
      rfrom = p;
      res = rdoc?.resource;
    }
    if (!res) return null;
    const props = res.props || {};
    const out = { material: name };
    const albedo = props.albedo_texture;
    if (albedo) out.texture = await textureOf(rfrom, rdoc, albedo);
    if (!out.texture) {
      const params = Object.entries(props).filter(([k]) => /^shader_param(eter)?\//.test(k));
      params.sort(([a], [b]) => TEX_PARAM.test(b) - TEX_PARAM.test(a));
      for (const [k, v] of params) {
        const t = await textureOf(rfrom, rdoc, v);
        if (t) { out.texture = t; out.param = k.split("/").pop(); break; }
      }
      if (!out.color) for (const [k, v] of params) if (/albedo|colou?r|base/i.test(k) && COLOR(v)) { out.color = COLOR(v); break; }
    }
    if (COLOR(props.albedo_color)) out.color = COLOR(props.albedo_color);
    return out.texture || out.color ? out : null;
  };

  for (const f of files.values()) {
    if (!/\.(tscn|escn)$/i.test(f.p)) continue;
    const doc = await parse(f.p);
    if (!doc) continue;
    for (const node of doc.nodes) {
      const props = node.props || {};
      const meshId = refOf(props.mesh, "ExtResource");
      const instId = refOf(node.instance, "ExtResource");
      const model = (meshId || instId) && resolveRes(f.p, doc.ext.get(meshId || instId)?.path);
      if (!model || !MODEL.test(model) || !files.has(model)) continue;
      const surfaces = [];
      let whole = null;
      // Instanced glTF scenes: overrides sit on child nodes under the instance.
      const owners = instId ? doc.nodes.filter((n) => n === node || String(n.parent || "").split("/")[0] === node.name || n.parent === node.name) : [node];
      for (const n of owners) {
        for (const [k, v] of Object.entries(n.props || {})) {
          const slot = k.match(/^(?:material|surface_material_override)\/(\d+)$/);
          if (!slot && k !== "material_override") continue;
          const m = await materialOf(f.p, doc, v);
          if (!m) continue;
          if (slot && !instId) surfaces.push({ index: +slot[1], ...m });
          else whole ||= m;
        }
      }
      if (!surfaces.length && !whole) continue;
      const list = uses.get(model) || uses.set(model, []).get(model);
      if (list.length < 12 && !list.some((x) => x.scene === f.p && x.node === node.name))
        list.push({ scene: f.p, node: node.name, surfaces, ...(whole ? { texture: whole.texture, color: whole.color, material: whole.material } : {}) });
    }
  }
  const result = { at: Date.now(), files, uses, resolveRes };
  cache.set(key, result);
  return result;
}

// --------------------------------------------------------------------------- per model --
const norm = (p) => path.posix.basename(p).toLowerCase().replace(/\.[^.]+$/, "")
  .replace(/[_ -]?(albedo|diffuse|diff|basecolou?r|base_colou?r|colou?r|col|tex|texture|alb|d|c)$/i, "").replace(/[^a-z0-9]+/g, "");

async function mtlCandidate(root, ix, p) {
  if (!/\.obj$/i.test(p)) return null;
  const text = await readText(path.join(root, p), 64 * 1024 * 1024);
  if (!text) return null;
  const libs = [...text.matchAll(/^mtllib\s+(.+?)\s*$/gm)].map((m) => m[1]);
  const surfaces = [];
  for (const lib of libs) {
    const mtl = ix.resolveRes(p, lib);
    const src = mtl && ix.files.has(mtl) ? await readText(path.join(root, mtl)) : null;
    if (!src) continue;
    let cur = null;
    for (const line of src.split("\n")) {
      const [cmd, ...rest] = line.trim().split(/\s+/);
      if (cmd === "newmtl") surfaces.push(cur = { name: rest.join(" ") });
      else if (cur && cmd === "Kd" && rest.length >= 3) cur.color = [+rest[0], +rest[1], +rest[2], 1];
      else if (cur && cmd === "map_Kd" && rest.length) {
        const t = ix.resolveRes(mtl, rest.at(-1).replace(/\\/g, "/"));
        if (t && ix.files.has(t) && IMAGE.test(t)) cur.texture = t;
        else if (t) cur.missing = rest.at(-1);
      }
    }
  }
  const useful = surfaces.filter((s) => s.texture || s.color);
  return useful.length ? { id: "mtl", label: `Material file (${libs.join(", ")})`, source: "mtl", surfaces: useful } : null;
}

function nameCandidates(ix, p) {
  const model = ix.files.get(p);
  const base = norm(p);
  if (!base || !model) return [];
  const dir = path.posix.dirname(p);
  const found = [];
  for (const f of ix.files.values()) {
    if (f.k !== "image" || f.bin !== model.bin || !IMAGE.test(f.p)) continue;
    const n = norm(f.p);
    if (n === base || (n.length > 3 && (n.startsWith(base) || base.startsWith(n)))) found.push([f.p, (n === base ? 0 : 2) + (path.posix.dirname(f.p) === dir ? 0 : 1)]);
  }
  return found.sort((a, b) => a[1] - b[1] || a[0].localeCompare(b[0])).slice(0, 4)
    .map(([t], i) => ({ id: `name${i}`, label: `Same name: ${path.posix.basename(t)} (a guess)`, source: "name", texture: t, surfaces: [] }));
}

export async function candidates(key, root, getTree, p) {
  const ix = await index(key, root, getTree);
  const saved = (await loadSaved(root)).models[p] || null;
  const list = [];
  if (saved && !saved.none) list.push({ id: "saved", label: `Saved by ${saved.by === "agent" ? "the agent" : "you"}${saved.note ? `: ${saved.note}` : ""}`, source: "saved", ...saved });
  for (const [i, u] of (ix.uses.get(p) || []).entries())
    list.push({ id: `scene${i}`, label: `Scene ${path.posix.basename(u.scene)} (node ${u.node})`, source: "scene", ...u });
  const mtl = await mtlCandidate(root, ix, p).catch(() => null);
  if (mtl) list.push(mtl);
  list.push(...nameCandidates(ix, p));
  return { model: p, saved, candidates: list };
}

// Models with nothing that gives them a look yet (embedded glTF textures count), for the agent.
export async function unresolved(key, root, getTree) {
  const ix = await index(key, root, getTree);
  const saved = (await loadSaved(root)).models;
  const out = [];
  let total = 0;
  for (const f of ix.files.values()) {
    if (f.k !== "model" || !MODEL.test(f.p)) continue;
    total++;
    if (saved[f.p] || ix.uses.has(f.p)) continue;
    if (/\.obj$/i.test(f.p) && await mtlCandidate(root, ix, f.p).catch(() => null)) continue;
    if (await carriesTextures(root, ix, f)) continue;
    out.push({ path: f.p, virtual: f.v, binary: f.bin, guesses: nameCandidates(ix, f.p).map((c) => c.texture) });
  }
  return { total, unresolved: out.length, models: out.slice(0, 500) };
}

// Does the model file bring its own look: embedded images (glb, Half-Life/Quake mdl, FBX media) or
// image names that exist in the decompiled tree (fbx, dae, 3ds, x, md2, md3, smd, gltf, ply)?
async function carriesTextures(root, ix, f) {
  const file = path.join(root, f.p);
  const ext = path.extname(f.p).slice(1).toLowerCase();
  if (ext === "glb" && await glbHasImages(file)) return true;
  if (ext === "stl") return false;
  let head;
  try {
    const fh = await fsp.open(file, "r");
    try { head = Buffer.alloc(Math.min((await fh.stat()).size, 32 * 1024 * 1024)); await fh.read(head, 0, head.length, 0); } finally { await fh.close(); }
  } catch { return false; }
  if (ext === "mdl") {
    const id = head.toString("latin1", 0, 4);
    if (id === "IDPO") return true;
    if (id === "IDST" && head.length > 184 && head.readInt32LE(4) === 10) {
      if (head.readInt32LE(180) > 0) return true;
      const t = f.p.replace(/\.mdl$/i, "t.mdl").toLowerCase();
      return [...ix.files.keys()].some((p) => p.toLowerCase() === t);
    }
    return false;
  }
  if (ext === "fbx" && head.includes(Buffer.from("Content")) && head.includes(Buffer.from("Video"))) return true;
  // Image names inside the file, matched by file name (or the same name with another image extension).
  // Linear scan: find each image extension, then walk back over file-name characters (a regex with
  // an unbounded name part backtracks badly on big binaries).
  const names = new Set();
  const text = head.toString("latin1");
  const nameChar = /[\w\-. ]/;
  for (const m of text.matchAll(/\.(?:png|jpe?g|tga|dds|bmp|pcx|gif|webp)(?![a-z0-9])/gi)) {
    let i = m.index;
    while (i > 0 && m.index - i < 64 && nameChar.test(text[i - 1])) i--;
    if (i < m.index) names.add(text.slice(i, m.index + m[0].length).trim().toLowerCase());
    if (names.size > 2000) break;
  }
  // MD3 shader / SMD material names often have no extension: compare their last path part to image names.
  if (ext === "md3" || ext === "smd") for (const m of text.slice(0, 4 * 1024 * 1024).matchAll(/[\w\-./]{3,64}/g)) names.add(m[0].split("/").pop().toLowerCase());
  if (!names.size) return false;
  const stems = new Set([...names].map((n) => n.replace(/\.[^.]+$/, "")));
  for (const g of ix.files.values()) {
    if (g.k !== "image" || g.bin !== f.bin) continue;
    const base = path.posix.basename(g.p).toLowerCase();
    if (names.has(base) || stems.has(base.replace(/\.[^.]+$/, ""))) return true;
  }
  return false;
}

async function glbHasImages(file) {
  try {
    const fh = await fsp.open(file, "r");
    try {
      const head = Buffer.alloc(20);
      await fh.read(head, 0, 20, 0);
      if (head.readUInt32LE(0) !== 0x46546c67) return false;
      const len = Math.min(head.readUInt32LE(12), 16 * 1024 * 1024);
      const json = Buffer.alloc(len);
      await fh.read(json, 0, len, 20);
      return (JSON.parse(json.toString("utf8")).images || []).length > 0;
    } finally { await fh.close(); }
  } catch { return false; }
}

// Save (or clear) a model's association. body = {model, texture?, surfaces?: [{index?, name?, texture?, color?}],
// none?: true (looked, there is no texture), note?, by}.
export async function save(key, root, getTree, body, locked) {
  const ix = await index(key, root, getTree);
  const model = String(body.model || "");
  const f = ix.files.get(model);
  if (!f || f.k !== "model") throw Object.assign(new Error("model must be a 3D model path from the tree (p)"), { status: 400 });
  const image = (t) => {
    if (t === undefined || t === null || t === "") return undefined;
    if (!ix.files.has(String(t)) || !IMAGE.test(String(t))) throw Object.assign(new Error(`${t}: not a png/jpg/webp/gif/bmp in this game's decompiled tree`), { status: 400 });
    return String(t);
  };
  const color = (c) => Array.isArray(c) && c.length >= 3 && c.slice(0, 4).every((x) => Number.isFinite(+x)) ? c.slice(0, 4).map(Number) : undefined;
  const entry = body.clear ? null : {
    ...(image(body.texture) ? { texture: image(body.texture) } : {}),
    surfaces: (Array.isArray(body.surfaces) ? body.surfaces.slice(0, 64) : []).map((s) => ({
      ...(Number.isInteger(s?.index) ? { index: s.index } : {}), ...(s?.name ? { name: String(s.name).slice(0, 120) } : {}),
      ...(image(s?.texture) ? { texture: image(s.texture) } : {}), ...(color(s?.color) ? { color: color(s.color) } : {}) }))
      .filter((s) => (s.index !== undefined || s.name) && (s.texture || s.color)),
    ...(body.none ? { none: true } : {}),
    note: String(body.note || "").slice(0, 1000), by: body.by === "agent" ? "agent" : "user", at: Date.now(),
  };
  if (entry && !entry.texture && !entry.surfaces.length && !entry.none) throw Object.assign(new Error("Give a texture, surfaces, or none: true"), { status: 400 });
  await locked(key, async () => {
    const data = await loadSaved(root);
    if (entry) data.models[model] = entry; else delete data.models[model];
    await fsp.writeFile(saveFile(root) + ".tmp", JSON.stringify(data, null, 1));
    await fsp.rename(saveFile(root) + ".tmp", saveFile(root));
  });
  return { model, saved: entry };
}

export const forget = (key) => cache.delete(key);
