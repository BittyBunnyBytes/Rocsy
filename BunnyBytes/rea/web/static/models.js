// REA model + image readers for the Decompiled tab's 3D viewer (no libraries, nothing executed).
// Every model reader returns { surfaces, vertices, faces, note } where a surface is triangles,
// expanded: { index, name, pos, uv|null, nrm, col|null (per-vertex RGBA), texUrl?|texCanvas?, texRef?, color? }.
// texRef = an image the file names (resolved against the decompiled tree by ctx.resolve).
// ctx = { resolve(ref) → url|null, fetchRef(ref) → ArrayBuffer|null, find(name) → ArrayBuffer|null }.
"use strict";

const DX_MODEL_EXT = new Set(["glb", "gltf", "obj", "fbx", "dae", "3ds", "ply", "stl", "x", "md2", "md3", "mdl", "smd"]);
const DX_DECODE_EXT = new Set(["tga", "dds", "pcx"]);
const DX_VERT_CAP = 6000000;

// ------------------------------------------------------------------------------ helpers --
function dxFlatNormals(pos) {
  const n = new Float32Array(pos.length);
  for (let i = 0; i + 8 < pos.length; i += 9) {
    const ax = pos[i + 3] - pos[i], ay = pos[i + 4] - pos[i + 1], az = pos[i + 5] - pos[i + 2];
    const bx = pos[i + 6] - pos[i], by = pos[i + 7] - pos[i + 1], bz = pos[i + 8] - pos[i + 2];
    let x = ay * bz - az * by, y = az * bx - ax * bz, z = ax * by - ay * bx;
    const l = Math.hypot(x, y, z) || 1; x /= l; y /= l; z /= l;
    for (let k = 0; k < 9; k += 3) { n[i + k] = x; n[i + k + 1] = y; n[i + k + 2] = z; }
  }
  return n;
}

// Collects triangles for one surface; corners are pushed three at a time.
class DxBuilder {
  constructor(index, name) { Object.assign(this, { index, name, p: [], t: [], n: [], c: [], hasT: false, hasN: true, hasC: false }); }
  corner(p, t, n, c) {
    this.p.push(p[0], p[1], p[2]);
    if (t) { this.t.push(t[0], t[1]); this.hasT = true; } else this.t.push(0, 0);
    if (n) this.n.push(n[0], n[1], n[2]); else { this.n.push(0, 0, 0); this.hasN = false; }
    if (c) { this.c.push(c[0], c[1], c[2], c[3] ?? 1); this.hasC = true; } else this.c.push(1, 1, 1, 1);
  }
  get count() { return this.p.length / 3; }
  done(extra = {}) {
    const pos = Float32Array.from(this.p);
    return { index: this.index, name: this.name, pos, uv: this.hasT ? Float32Array.from(this.t) : null,
      nrm: this.hasN ? Float32Array.from(this.n) : dxFlatNormals(pos), col: this.hasC ? Float32Array.from(this.c) : null, ...extra };
  }
}

// Column-major 4x4.
const dxI = () => [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
function dxMul(a, b) { const o = new Array(16).fill(0); for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) for (let k = 0; k < 4; k++) o[c * 4 + r] += a[k * 4 + r] * b[c * 4 + k]; return o; }
const dxApply = (m, v) => [m[0] * v[0] + m[4] * v[1] + m[8] * v[2] + m[12], m[1] * v[0] + m[5] * v[1] + m[9] * v[2] + m[13], m[2] * v[0] + m[6] * v[1] + m[10] * v[2] + m[14]];
const dxApplyN = (m, v) => [m[0] * v[0] + m[4] * v[1] + m[8] * v[2], m[1] * v[0] + m[5] * v[1] + m[9] * v[2], m[2] * v[0] + m[6] * v[1] + m[10] * v[2]];
function dxQuatTRS(q, t = [0, 0, 0], s = [1, 1, 1]) {
  const [x, y, z, w] = q;
  return [(1 - 2 * (y * y + z * z)) * s[0], 2 * (x * y + z * w) * s[0], 2 * (x * z - y * w) * s[0], 0,
    2 * (x * y - z * w) * s[1], (1 - 2 * (x * x + z * z)) * s[1], 2 * (y * z + x * w) * s[1], 0,
    2 * (x * z + y * w) * s[2], 2 * (y * z - x * w) * s[2], (1 - 2 * (x * x + y * y)) * s[2], 0, t[0], t[1], t[2], 1];
}
// Z-up formats (Quake, 3DS, Source, Blender exports) shown Y-up.
const dxZup = (v) => [v[0], v[2], -v[1]];
const dxZupSurfaces = (surfaces) => {
  for (const s of surfaces) for (const a of [s.pos, s.nrm]) for (let i = 0; i < a.length; i += 3) { const y = a[i + 1]; a[i + 1] = a[i + 2]; a[i + 2] = -y; }
  return surfaces;
};
const dxUtf8 = (buf) => new TextDecoder("utf-8").decode(buf);
const dxCstr = (u8, at, max) => { let s = ""; for (let i = 0; i < max && u8[at + i]; i++) s += String.fromCharCode(u8[at + i]); return s; };

// --------------------------------------------------------------------------- entry point --
async function dxParseModel(ext, buffer, ctx) {
  switch (ext) {
    case "obj": return dxParseObj(dxUtf8(buffer), ctx);
    case "glb": return dxParseGlb(buffer, ctx);
    case "gltf": return dxParseGltfJson(JSON.parse(dxUtf8(buffer)), [], ctx);
    case "fbx": return dxParseFbx(buffer, ctx);
    case "dae": return dxParseDae(dxUtf8(buffer), ctx);
    case "3ds": return dxParse3ds(buffer, ctx);
    case "ply": return dxParsePly(buffer, ctx);
    case "stl": return dxParseStl(buffer);
    case "x": return dxParseX(buffer, ctx);
    case "md2": return dxParseMd2(buffer, ctx);
    case "md3": return dxParseMd3(buffer, ctx);
    case "mdl": return dxParseMdl(buffer, ctx);
    case "smd": return dxParseSmd(dxUtf8(buffer), ctx);
    default: throw new Error(`.${ext} isn't a 3D format REA can show`);
  }
}

// -------------------------------------------------------------------------------- OBJ --
function dxParseObj(text) {
  const v = [], vt = [], vn = [], vc = [], surfaces = [];
  let cur = null, faces = 0, verts = 0;
  const use = (name) => {
    cur = surfaces.find((s) => s.name === name);
    if (!cur) surfaces.push(cur = new DxBuilder(surfaces.length, name));
  };
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (line.startsWith("v ")) {
      const p = line.split(/\s+/);
      v.push(+p[1] || 0, +p[2] || 0, +p[3] || 0);
      vc.push(p.length >= 7 ? [+p[4], +p[5], +p[6], 1] : null);   // "v x y z r g b" vertex colours
    } else if (line.startsWith("vt ")) { const p = line.split(/\s+/); vt.push(+p[1] || 0, 1 - (+p[2] || 0)); }
    else if (line.startsWith("vn ")) { const p = line.split(/\s+/); vn.push(+p[1] || 0, +p[2] || 0, +p[3] || 0); }
    else if (line.startsWith("usemtl ")) use(line.slice(7).trim());
    else if (line.startsWith("f ") && verts < DX_VERT_CAP) {
      if (!cur) use("");
      const nv = v.length / 3, nt = vt.length / 2, nn = vn.length / 3;
      const idx = line.split(/\s+/).slice(1).map((tok) => {
        const [a, b, c] = tok.split("/").map((x) => x === undefined || x === "" ? NaN : parseInt(x, 10));
        const fix = (i, n) => Number.isNaN(i) ? -1 : i < 0 ? n + i : i - 1;
        return [fix(a, nv), fix(b, nt), fix(c, nn)];
      }).filter(([a]) => a >= 0 && a < nv);
      for (let i = 1; i + 1 < idx.length; i++) for (const [a, b, c] of [idx[0], idx[i], idx[i + 1]]) {
        cur.corner([v[a * 3], v[a * 3 + 1], v[a * 3 + 2]], b >= 0 ? [vt[b * 2], vt[b * 2 + 1]] : null, c >= 0 ? [vn[c * 3], vn[c * 3 + 1], vn[c * 3 + 2]] : null, vc[a]);
        verts++;
      }
      faces++;
    }
  }
  return { surfaces: surfaces.filter((s) => s.count).map((s) => s.done()), vertices: v.length / 3, faces, note: verts >= DX_VERT_CAP ? "very large: only part is drawn" : "" };
}

// ------------------------------------------------------------------------------- glTF --
async function dxParseGlb(buffer, ctx) {
  const view = new DataView(buffer);
  if (buffer.byteLength < 20 || view.getUint32(0, true) !== 0x46546c67) throw new Error("Not a binary glTF file");
  const jsonLen = view.getUint32(12, true);
  const gltf = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 20, jsonLen)));
  const binStart = 20 + jsonLen + 8;
  const binLen = binStart <= buffer.byteLength ? new DataView(buffer, binStart - 8, 4).getUint32(0, true) : 0;
  return dxParseGltfJson(gltf, [buffer.slice(binStart, binStart + binLen)], ctx);
}

async function dxParseGltfJson(gltf, glbBuffers, ctx) {
  // Buffers: the GLB chunk, data: URIs or files next to the model.
  const buffers = [];
  for (const [i, b] of (gltf.buffers || []).entries()) {
    if (!b.uri && glbBuffers[i]) buffers.push(glbBuffers[i]);
    else if (/^data:/.test(b.uri || "")) buffers.push(await (await fetch(b.uri)).arrayBuffer());
    else buffers.push(b.uri ? await ctx.fetchRef(decodeURIComponent(b.uri)) : null);
  }
  const views = buffers.map((b) => b && new DataView(b));
  const read = (i) => {
    const acc = gltf.accessors?.[i];
    const bv = acc && gltf.bufferViews?.[acc.bufferView];
    const view = bv && views[bv.buffer ?? 0];
    if (!acc || !bv || !view) return null;
    const comps = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4 }[acc.type] || 1;
    const sz = { 5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4 }[acc.componentType] || 4;
    const stride = bv.byteStride || comps * sz;
    const base = (bv.byteOffset || 0) + (acc.byteOffset || 0);
    const get = { 5120: (o) => view.getInt8(o), 5121: (o) => view.getUint8(o), 5122: (o) => view.getInt16(o, true), 5123: (o) => view.getUint16(o, true),
      5125: (o) => view.getUint32(o, true), 5126: (o) => view.getFloat32(o, true) }[acc.componentType];
    if (!get || base + (acc.count - 1) * stride + comps * sz > view.byteLength) return null;
    const scale = acc.normalized ? { 5120: 127, 5121: 255, 5122: 32767, 5123: 65535 }[acc.componentType] || 1 : 1;
    const out = new Float32Array(acc.count * comps);
    for (let k = 0; k < acc.count; k++) for (let c = 0; c < comps; c++) out[k * comps + c] = get(base + k * stride + c * sz) / scale;
    return { data: out, comps };
  };
  const images = new Map();
  const texOf = (ti) => {
    const src = gltf.textures?.[ti]?.source;
    const img = gltf.images?.[src];
    if (!img) return {};
    if (images.has(src)) return images.get(src);
    let out = {};
    if (img.bufferView !== undefined) {
      const bv = gltf.bufferViews[img.bufferView];
      const buf = buffers[bv.buffer ?? 0];
      if (buf) out = { texBlob: new Blob([new Uint8Array(buf, bv.byteOffset || 0, bv.byteLength)], { type: img.mimeType || "image/png" }) };
    } else if (/^data:image\//.test(img.uri || "")) out = { texUrl: img.uri };
    else if (img.uri) out = { texRef: decodeURIComponent(img.uri) };
    images.set(src, out);
    return out;
  };
  const trs = (n) => n.matrix || dxQuatTRS(n.rotation || [0, 0, 0, 1], n.translation, n.scale);
  const surfaces = [];
  let vertices = 0, faces = 0, skipped = 0, total = 0, index = 0;
  const addMesh = (mi, m) => {
    for (const prim of gltf.meshes?.[mi]?.primitives || []) {
      const at = index++;
      if (prim.extensions?.KHR_draco_mesh_compression || prim.extensions?.EXT_meshopt_compression || (prim.mode ?? 4) !== 4) { skipped++; continue; }
      const p = prim.attributes?.POSITION !== undefined && read(prim.attributes.POSITION);
      if (!p) { skipped++; continue; }
      const t = prim.attributes.TEXCOORD_0 !== undefined && read(prim.attributes.TEXCOORD_0);
      const n = prim.attributes.NORMAL !== undefined && read(prim.attributes.NORMAL);
      const c = prim.attributes.COLOR_0 !== undefined && read(prim.attributes.COLOR_0);
      const idx = prim.indices !== undefined ? read(prim.indices)?.data : Uint32Array.from({ length: p.data.length / 3 }, (_, i) => i);
      if (!idx || total + idx.length > DX_VERT_CAP) { skipped++; continue; }
      total += idx.length;
      const mat = gltf.materials?.[prim.material];
      const b = new DxBuilder(at, mat?.name || gltf.meshes[mi].name || "");
      for (const i of idx) {
        b.corner(dxApply(m, [p.data[i * 3], p.data[i * 3 + 1], p.data[i * 3 + 2]]), t ? [t.data[i * 2], t.data[i * 2 + 1]] : null,
          n ? dxApplyN(m, [n.data[i * 3], n.data[i * 3 + 1], n.data[i * 3 + 2]]) : null,
          c ? [c.data[i * c.comps], c.data[i * c.comps + 1], c.data[i * c.comps + 2], c.comps === 4 ? c.data[i * 4 + 3] : 1] : null);
      }
      const pbr = mat?.pbrMetallicRoughness || {};
      surfaces.push(b.done({ ...(pbr.baseColorTexture ? texOf(pbr.baseColorTexture.index) : {}), color: pbr.baseColorFactor || (mat ? [1, 1, 1, 1] : null) }));
      vertices += p.data.length / 3;
      faces += idx.length / 3;
    }
  };
  const seen = new Set();
  const walk = (ni, parent) => {
    const node = gltf.nodes?.[ni];
    if (!node || seen.has(ni)) return;
    seen.add(ni);
    const m = dxMul(parent, trs(node));
    if (node.mesh !== undefined) addMesh(node.mesh, m);
    for (const c of node.children || []) walk(c, m);
  };
  const roots = gltf.scenes?.[gltf.scene ?? 0]?.nodes;
  if (roots) for (const r of roots) walk(r, dxI()); else (gltf.meshes || []).forEach((_, i) => addMesh(i, dxI()));
  const missing = (gltf.buffers || []).filter((b, i) => !buffers[i]).length;
  const note = [`${gltf.materials?.length || 0} materials`, `${gltf.images?.length || 0} images`, gltf.animations?.length ? `${gltf.animations.length} animations (not played)` : "",
    skipped ? `${skipped} compressed/unsupported parts not drawn` : "", missing ? `${missing} buffer file(s) not found` : ""].filter(Boolean).join(", ");
  return { surfaces, vertices, faces, note };
}

// -------------------------------------------------------------------------- FBX binary --
async function dxInflate(u8) {
  const stream = new Blob([u8]).stream().pipeThrough(new DecompressionStream("deflate"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function dxParseFbx(buffer) {
  const u8 = new Uint8Array(buffer);
  let top, version;
  if (dxCstr(u8, 0, 20) === "Kaydara FBX Binary  ") ({ top, version } = await dxFbxBinary(buffer));
  else {
    const text = dxUtf8(u8);
    if (!/FBXHeaderExtension\s*:/.test(text.slice(0, 8192))) throw new Error("Not an FBX file");
    ({ top, version } = dxFbxAscii(text));
    if (version < 7000) throw new Error(`ASCII FBX ${version}: only FBX 7 and newer can be shown`);
  }
  return dxFbxScene(top, version);
}

// ASCII FBX 7.x → the same node tree as the binary reader ({name, props, children}).
//   Key: v, v, … { children }     arrays: Key: *N { a: v,v,… }
function dxFbxAscii(text) {
  // Strings have no escapes in FBX ("D:\" is a whole string; quotes are written &quot;).
  const re = /;[^\n]*|"([^"]*)"|([A-Za-z_][\w|.-]*)\s*:|(\*\d+)|([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)|([{},])|(\n)|([A-Za-z_][\w]*)/g;
  const tokens = [];
  for (const m of text.matchAll(re)) {
    if (m[0][0] === ";") continue;
    if (m[1] !== undefined) tokens.push({ t: "v", v: m[1] });
    else if (m[2] !== undefined) tokens.push({ t: "key", v: m[2] });
    else if (m[3] !== undefined) tokens.push({ t: "v", v: m[3] });
    else if (m[4] !== undefined) {
      const raw = m[4];
      tokens.push({ t: "v", v: /^[-+]?\d{16,}$/.test(raw) ? BigInt(raw) : Number(raw) });
    } else if (m[5] !== undefined) tokens.push({ t: m[5] });
    else if (m[6] !== undefined) tokens.push({ t: "nl" });
    else tokens.push({ t: "v", v: m[7] });            // bare words like Y / T / W
  }
  let i = 0;
  const skipNl = () => { while (tokens[i]?.t === "nl") i++; };
  const list = () => {
    const nodes = [];
    for (;;) {
      skipNl();
      const tk = tokens[i];
      if (!tk || tk.t === "}") { i++; return nodes; }
      if (tk.t !== "key") { i++; continue; }
      i++;
      const node = { name: tk.v, props: [], children: [] };
      while (tokens[i] && tokens[i].t === "v") {
        node.props.push(tokens[i++].v);
        if (tokens[i]?.t === ",") { i++; skipNl(); } else break;
      }
      if (tokens[i]?.t === "{") { i++; node.children = list(); }
      // Arrays: "*N { a: … }" → one typed array prop.
      if (typeof node.props[0] === "string" && node.props[0][0] === "*" && node.children.length === 1 && node.children[0].name === "a") {
        const vals = node.children[0].props;
        node.props = [/Index|Materials|Edges/.test(node.name) ? Int32Array.from(vals, Number) : Float64Array.from(vals, Number)];
        node.children = [];
      }
      nodes.push(node);
    }
  };
  const top = list();
  const header = top.find((n) => n.name === "FBXHeaderExtension");
  const version = Number(header?.children.find((n) => n.name === "FBXVersion")?.props[0]) || 0;
  return { top, version };
}

async function dxFbxBinary(buffer) {
  const u8 = new Uint8Array(buffer);
  const view = new DataView(buffer);
  const version = view.getUint32(23, true);
  const big = version >= 7500;
  const pending = [];
  const readNode = (at) => {
    const end = big ? Number(view.getBigUint64(at, true)) : view.getUint32(at, true);
    if (end === 0) return { next: at + (big ? 25 : 13), node: null };
    const numProps = big ? Number(view.getBigUint64(at + 8, true)) : view.getUint32(at + 4, true);
    const listLen = big ? Number(view.getBigUint64(at + 16, true)) : view.getUint32(at + 8, true);
    const nameAt = at + (big ? 24 : 12);
    const nameLen = u8[nameAt];
    const node = { name: dxCstr(u8, nameAt + 1, nameLen), props: [], children: [] };
    let p = nameAt + 1 + nameLen;
    for (let i = 0; i < numProps; i++) {
      const type = String.fromCharCode(u8[p++]);
      if (type === "Y") { node.props.push(view.getInt16(p, true)); p += 2; }
      else if (type === "C") { node.props.push(Boolean(u8[p])); p += 1; }
      else if (type === "I") { node.props.push(view.getInt32(p, true)); p += 4; }
      else if (type === "F") { node.props.push(view.getFloat32(p, true)); p += 4; }
      else if (type === "D") { node.props.push(view.getFloat64(p, true)); p += 8; }
      else if (type === "L") { node.props.push(view.getBigInt64(p, true)); p += 8; }
      else if (type === "S" || type === "R") {
        const len = view.getUint32(p, true);
        const raw = u8.subarray(p + 4, p + 4 + len);
        node.props.push(type === "S" ? new TextDecoder().decode(raw) : raw);
        p += 4 + len;
      } else if ("fdlib".includes(type)) {
        const count = view.getUint32(p, true), enc = view.getUint32(p + 4, true), clen = view.getUint32(p + 8, true);
        const raw = u8.subarray(p + 12, p + 12 + clen);
        const slot = node.props.length;
        node.props.push(null);
        const decode = (bytes) => {
          const b = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
          node.props[slot] = type === "f" ? new Float32Array(b, 0, count) : type === "d" ? new Float64Array(b, 0, count) : type === "i" ? new Int32Array(b, 0, count)
            : type === "l" ? new BigInt64Array(b, 0, count) : new Uint8Array(b, 0, count);
        };
        if (enc === 1) pending.push(dxInflate(raw).then(decode)); else decode(raw);
        p += 12 + clen;
      } else throw new Error(`Unknown FBX property type ${type}`);
    }
    p = at + (big ? 24 : 12) + 1 + nameLen + listLen;
    while (p < end) {
      const r = readNode(p);
      if (!r.node) { p = r.next; break; }
      node.children.push(r.node);
      p = r.next;
    }
    return { next: end, node };
  };
  const top = [];
  for (let at = 27; at < buffer.byteLength - 13;) {
    const r = readNode(at);
    if (!r.node) break;
    top.push(r.node);
    at = r.next;
  }
  await Promise.all(pending);
  return { top, version };
}

function dxFbxScene(top, version) {
  const child = (n, name) => n?.children.find((c) => c.name === name);
  const props70 = (n) => {
    const out = {};
    for (const p of child(n, "Properties70")?.children || []) if (p.name === "P") out[p.props[0]] = p.props.slice(4);
    return out;
  };
  const objects = new Map();
  for (const n of top.find((t) => t.name === "Objects")?.children || []) objects.set(String(n.props[0]), n);
  const links = (top.find((t) => t.name === "Connections")?.children || []).filter((c) => c.name === "C").map((c) => ({ type: c.props[0], child: String(c.props[1]), parent: String(c.props[2]), prop: c.props[3] }));
  const parentsOf = (id, kind) => links.filter((l) => l.child === id && (!kind || objects.get(l.parent)?.name === kind)).map((l) => l.parent);
  const childrenOf = (id, kind) => links.filter((l) => l.parent === id && (!kind || objects.get(l.child)?.name === kind)).map((l) => l.child);
  // Binary names are "Name\0\1Class", ASCII ones "Class::Name".
  const clean = (s) => String(s || "").split("\u0000")[0].replace(/^\w+::/, "");
  // Model transforms: T · Rpre · R · S (Euler XYZ, degrees), parent chain.
  const rad = Math.PI / 180;
  const euler = ([x = 0, y = 0, z = 0]) => {
    const cx = Math.cos(x * rad), sx = Math.sin(x * rad), cy = Math.cos(y * rad), sy = Math.sin(y * rad), cz = Math.cos(z * rad), sz = Math.sin(z * rad);
    // Rz · Ry · Rx, column-major
    return [cz * cy, sz * cy, -sy, 0, cz * sy * sx - sz * cx, sz * sy * sx + cz * cx, cy * sx, 0, cz * sy * cx + sz * sx, sz * sy * cx - cz * sx, cy * cx, 0, 0, 0, 0, 1];
  };
  const localOf = (model) => {
    const p = props70(model);
    const t = p["Lcl Translation"] || [0, 0, 0], s = p["Lcl Scaling"] || [1, 1, 1];
    let m = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, t[0], t[1], t[2], 1];
    if (p.PreRotation) m = dxMul(m, euler(p.PreRotation));
    m = dxMul(m, euler(p["Lcl Rotation"] || [0, 0, 0]));
    return dxMul(m, [s[0], 0, 0, 0, 0, s[1], 0, 0, 0, 0, s[2], 0, 0, 0, 0, 1]);
  };
  const worldOf = (id, depth = 0) => {
    const model = objects.get(id);
    if (!model || depth > 64) return dxI();
    const parent = parentsOf(id, "Model")[0];
    return parent ? dxMul(worldOf(parent, depth + 1), localOf(model)) : localOf(model);
  };
  // Material → diffuse texture (embedded Video content or a file name) + colour.
  const materialLook = (mid) => {
    const mat = objects.get(mid);
    const p = props70(mat);
    const color = p.DiffuseColor ? [p.DiffuseColor[0], p.DiffuseColor[1], p.DiffuseColor[2], 1] : p.Diffuse ? [p.Diffuse[0], p.Diffuse[1], p.Diffuse[2], 1] : null;
    const texId = links.find((l) => l.parent === mid && objects.get(l.child)?.name === "Texture" && /Diffuse|Base/i.test(l.prop || "DiffuseColor"))?.child
      || childrenOf(mid, "Texture")[0];
    const tex = texId && objects.get(texId);
    const out = { name: clean(mat?.props[1]), color };
    if (tex) {
      const video = childrenOf(texId, "Video").map((v) => objects.get(v))[0];
      const content = child(video, "Content")?.props[0];
      const ref = clean(child(tex, "RelativeFilename")?.props[0] || child(tex, "FileName")?.props[0] || child(video, "RelativeFilename")?.props[0]);
      if (content instanceof Uint8Array && content.length > 16) {
        const ext = ref.split(".").pop().toLowerCase();
        if (DX_DECODE_EXT.has(ext)) out.texCanvas = dxDecodeImage(ext, content.slice().buffer);
        else out.texBlob = new Blob([content], { type: ext === "png" ? "image/png" : "image/jpeg" });
      }
      if (ref) out.texRef = ref;
    }
    return out;
  };
  const surfaces = [];
  let vertices = 0, faces = 0, total = 0;
  for (const [gid, geo] of objects) {
    if (geo.name !== "Geometry" || geo.props[2] !== "Mesh") continue;
    const verts = child(geo, "Vertices")?.props[0];
    const poly = child(geo, "PolygonVertexIndex")?.props[0];
    if (!verts || !poly) continue;
    const layer = (name, dataName, indexName) => {
      const l = child(geo, name);
      if (!l) return null;
      return { data: child(l, dataName)?.props[0], index: child(l, indexName)?.props[0], map: child(l, "MappingInformationType")?.props[0], ref: child(l, "ReferenceInformationType")?.props[0] };
    };
    const nl = layer("LayerElementNormal", "Normals", "NormalsIndex"), ul = layer("LayerElementUV", "UV", "UVIndex"), cl = layer("LayerElementColor", "Colors", "ColorIndex");
    const ml = child(geo, "LayerElementMaterial");
    const matIdx = child(ml, "Materials")?.props[0];
    const matAll = child(ml, "MappingInformationType")?.props[0] === "AllSame";
    // Element value for polygon-vertex k (vertex v, polygon f).
    const pick = (l, k, v, f, size) => {
      if (!l?.data) return null;
      let i = /ByPolygonVertex/.test(l.map) ? k : /ByVert/.test(l.map) ? v : /ByPolygon/.test(l.map) ? f : 0;
      if (/Index/.test(l.ref || "") && l.index) i = l.index[i];
      return i < 0 || i * size >= l.data.length ? null : Array.from(l.data.subarray(i * size, i * size + size));
    };
    for (const mid of parentsOf(gid, "Model")) {
      const world = worldOf(mid);
      const mats = childrenOf(mid, "Material");
      const looks = mats.map(materialLook);
      const builders = new Map();
      let f = 0, start = 0;
      for (let k = 0; k < poly.length; k++) {
        if (poly[k] >= 0) continue;
        const corners = [];
        for (let j = start; j <= k; j++) {
          const v = j === k ? ~poly[j] : poly[j];
          const n = pick(nl, j, v, f, 3), t = pick(ul, j, v, f, 2), c = pick(cl, j, v, f, 4);
          corners.push([dxApply(world, [verts[v * 3], verts[v * 3 + 1], verts[v * 3 + 2]]), t ? [t[0], 1 - t[1]] : null, n ? dxApplyN(world, n) : null, c]);
        }
        const mi = matIdx ? (matAll ? matIdx[0] : matIdx[f] ?? 0) : 0;
        let b = builders.get(mi);
        if (!b) builders.set(mi, b = new DxBuilder(0, looks[mi]?.name || `material ${mi}`));
        if (total < DX_VERT_CAP) for (let i = 1; i + 1 < corners.length; i++) { for (const c of [corners[0], corners[i], corners[i + 1]]) b.corner(...c); total += 3; }
        faces++; f++; start = k + 1;
      }
      for (const [mi, b] of builders) {
        if (!b.count) continue;
        b.index = surfaces.length;
        const { name, ...look } = looks[mi] || {};
        surfaces.push(b.done(look));
      }
      vertices += verts.length / 3;
    }
  }
  return { surfaces, vertices, faces, note: `FBX ${Math.floor(version / 1000)}.${Math.floor(version % 1000 / 100)}${total >= DX_VERT_CAP ? ", very large: only part is drawn" : ""}` };
}

// ---------------------------------------------------------------------------- COLLADA --
function dxParseDae(text) {
  const doc = new DOMParser().parseFromString(text, "application/xml");
  if (doc.querySelector("parsererror")) throw new Error("Not valid COLLADA XML");
  const byId = new Map([...doc.querySelectorAll("[id]")].map((e) => [e.getAttribute("id"), e]));
  const ref = (url) => byId.get(String(url || "").replace(/^#/, ""));
  const kids = (e, tag) => [...(e?.children || [])].filter((c) => c.localName === tag);
  const floats = (e) => (e?.textContent || "").trim().split(/\s+/).filter(Boolean).map(Number);
  const zup = doc.querySelector("asset > up_axis")?.textContent.trim() === "Z_UP";
  const imageFile = (id) => {
    const img = byId.get(id);
    const init = img && [...img.getElementsByTagName("init_from")][0];
    return init ? (init.querySelector("ref")?.textContent || init.textContent).trim() : "";
  };
  // material id → {texRef?, color?}
  const lookOf = (matId) => {
    const mat = byId.get(matId);
    const effect = ref(mat?.querySelector("instance_effect")?.getAttribute("url"));
    const out = { name: mat?.getAttribute("name") || matId || "" };
    if (!effect) return out;
    const diffuse = effect.querySelector("diffuse");
    const tex = diffuse?.querySelector("texture");
    if (tex) {
      // sampler2D newparam → surface newparam → image (1.4), or straight to an image (1.5 / sloppy exporters).
      const sid = tex.getAttribute("texture");
      const params = [...effect.getElementsByTagName("newparam")];
      const sampler = params.find((p) => p.getAttribute("sid") === sid);
      let image = sid;
      const src = sampler?.querySelector("source")?.textContent.trim();
      if (src) {
        const surface = params.find((p) => p.getAttribute("sid") === src);
        image = surface?.querySelector("init_from")?.textContent.trim() || src;
      } else if (sampler?.querySelector("instance_image")) image = sampler.querySelector("instance_image").getAttribute("url").replace(/^#/, "");
      out.texRef = imageFile(image) || "";
    }
    const c = diffuse?.querySelector("color");
    if (c) { const v = floats(c); out.color = [v[0], v[1], v[2], v[3] ?? 1]; }
    return out;
  };
  const matrixOf = (node) => {
    let m = dxI();
    for (const t of node.children) {
      const v = floats(t);
      if (t.localName === "matrix" && v.length === 16) m = dxMul(m, [v[0], v[4], v[8], v[12], v[1], v[5], v[9], v[13], v[2], v[6], v[10], v[14], v[3], v[7], v[11], v[15]]);
      else if (t.localName === "translate") m = dxMul(m, [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, v[0], v[1], v[2], 1]);
      else if (t.localName === "scale") m = dxMul(m, [v[0], 0, 0, 0, 0, v[1], 0, 0, 0, 0, v[2], 0, 0, 0, 0, 1]);
      else if (t.localName === "rotate") {
        const [x, y, z] = v, a = v[3] * Math.PI / 180, l = Math.hypot(x, y, z) || 1, s = Math.sin(a / 2);
        m = dxMul(m, dxQuatTRS([x / l * s, y / l * s, z / l * s, Math.cos(a / 2)]));
      }
    }
    return m;
  };
  const surfaces = [];
  const looks = new Map();
  let vertices = 0, faces = 0, total = 0;
  const source = (url) => {
    const s = ref(url);
    if (!s) return null;
    if (s.localName === "vertices") return source(kids(s, "input").find((i) => i.getAttribute("semantic") === "POSITION")?.getAttribute("source"));
    const acc = s.querySelector("technique_common accessor");
    return { data: floats(s.querySelector("float_array")), stride: +(acc?.getAttribute("stride") || 3) };
  };
  const addGeometry = (geo, world, bind) => {
    const mesh = kids(geo, "mesh")[0];
    if (!mesh) return;
    for (const prim of mesh.children) {
      if (!["triangles", "polylist", "polygons"].includes(prim.localName)) continue;
      const inputs = kids(prim, "input").map((i) => ({ sem: i.getAttribute("semantic"), off: +(i.getAttribute("offset") || 0), set: +(i.getAttribute("set") || 0), src: source(i.getAttribute("source")) }));
      const stride = Math.max(...inputs.map((i) => i.off)) + 1;
      const pos = inputs.find((i) => i.sem === "VERTEX"), nor = inputs.find((i) => i.sem === "NORMAL"), tex = inputs.filter((i) => i.sem === "TEXCOORD").sort((a, b) => a.set - b.set)[0];
      const colr = inputs.find((i) => i.sem === "COLOR");
      if (!pos?.src) continue;
      const symbol = prim.getAttribute("material") || "";
      const matId = bind.get(symbol) || symbol;
      if (!looks.has(matId)) looks.set(matId, lookOf(matId));
      const { name, ...look } = looks.get(matId);
      const b = new DxBuilder(surfaces.length, name || symbol);
      const polys = prim.localName === "polygons" ? kids(prim, "p").map(floats) : [floats(kids(prim, "p")[0])];
      const counts = prim.localName === "polylist" ? floats(kids(prim, "vcount")[0]) : null;
      const corner = (p, k) => {
        const get = (inp, n) => inp?.src ? Array.from({ length: n }, (_, j) => inp.src.data[p[k * stride + inp.off] * inp.src.stride + j]) : null;
        const t = get(tex, 2);
        return [dxApply(world, get(pos, 3)), t ? [t[0], 1 - t[1]] : null, nor ? dxApplyN(world, get(nor, 3)) : null, colr ? [...get(colr, 3), 1] : null];
      };
      for (const p of polys) {
        const sizes = counts || (prim.localName === "polygons" ? [p.length / stride] : Array(Math.floor(p.length / stride / 3)).fill(3));
        let k = 0;
        for (const n of sizes) {
          if (total < DX_VERT_CAP) for (let i = 1; i + 1 < n; i++) { for (const c of [k, k + i, k + i + 1]) b.corner(...corner(p, c)); total += 3; }
          k += n; faces++;
        }
      }
      vertices += pos.src.data.length / 3;
      if (b.count) surfaces.push(b.done(look));
    }
  };
  const walk = (node, parent, depth = 0) => {
    if (depth > 64) return;
    const m = dxMul(parent, matrixOf(node));
    for (const ig of kids(node, "instance_geometry")) {
      const bind = new Map([...ig.getElementsByTagName("instance_material")].map((im) => [im.getAttribute("symbol"), im.getAttribute("target").replace(/^#/, "")]));
      const geo = ref(ig.getAttribute("url"));
      if (geo) addGeometry(geo, m, bind);
    }
    for (const c of kids(node, "node")) walk(c, m, depth + 1);
  };
  const scene = ref(doc.querySelector("scene > instance_visual_scene")?.getAttribute("url")) || doc.querySelector("library_visual_scenes > visual_scene");
  if (scene) for (const n of kids(scene, "node")) walk(n, dxI());
  if (!surfaces.length) for (const g of doc.querySelectorAll("library_geometries > geometry")) addGeometry(g, dxI(), new Map());
  return { surfaces: zup ? dxZupSurfaces(surfaces) : surfaces, vertices, faces, note: `COLLADA${zup ? ", Z-up" : ""}${total >= DX_VERT_CAP ? ", very large: only part is drawn" : ""}` };
}

// -------------------------------------------------------------------------------- 3DS --
function dxParse3ds(buffer) {
  const view = new DataView(buffer), u8 = new Uint8Array(buffer);
  if (view.getUint16(0, true) !== 0x4d4d) throw new Error("Not a 3DS file");
  const materials = new Map();
  const objects = [];
  const cstr = (at) => { let s = "", i = at; while (i < u8.length && u8[i]) s += String.fromCharCode(u8[i++]); return [s, i + 1]; };
  const chunks = (start, end, fn) => {
    for (let p = start; p + 6 <= end;) {
      const id = view.getUint16(p, true), len = view.getUint32(p + 2, true);
      if (len < 6 || p + len > end) break;
      fn(id, p + 6, p + len);
      p += len;
    }
  };
  const colorOf = (s, e) => {
    let c = null;
    chunks(s, e, (id, a) => {
      if (id === 0x0010 || id === 0x0013) c = [view.getFloat32(a, true), view.getFloat32(a + 4, true), view.getFloat32(a + 8, true), 1];
      else if ((id === 0x0011 || id === 0x0012) && !c) c = [u8[a] / 255, u8[a + 1] / 255, u8[a + 2] / 255, 1];
    });
    return c;
  };
  chunks(6, u8.length, (id, s, e) => {
    if (id !== 0x3d3d) return;
    chunks(s, e, (id2, s2, e2) => {
      if (id2 === 0xafff) {
        const m = {};
        chunks(s2, e2, (id3, s3, e3) => {
          if (id3 === 0xa000) m.name = cstr(s3)[0];
          else if (id3 === 0xa020) m.color = colorOf(s3, e3);
          else if (id3 === 0xa200) chunks(s3, e3, (id4, s4) => { if (id4 === 0xa300) m.texRef = cstr(s4)[0]; });
        });
        if (m.name) materials.set(m.name, m);
      } else if (id2 === 0x4000) {
        const [name, after] = cstr(s2);
        chunks(after, e2, (id3, s3, e3) => {
          if (id3 !== 0x4100) return;
          const o = { name, verts: null, faces: null, uv: null, groups: [] };
          chunks(s3, e3, (id4, s4, e4) => {
            if (id4 === 0x4110) { const n = view.getUint16(s4, true); o.verts = new Float32Array(buffer.slice(s4 + 2, s4 + 2 + n * 12)); }
            else if (id4 === 0x4140) { const n = view.getUint16(s4, true); o.uv = new Float32Array(buffer.slice(s4 + 2, s4 + 2 + n * 8)); }
            else if (id4 === 0x4120) {
              const n = view.getUint16(s4, true);
              o.faces = new Uint16Array(n * 3);
              for (let i = 0; i < n; i++) for (let k = 0; k < 3; k++) o.faces[i * 3 + k] = view.getUint16(s4 + 2 + i * 8 + k * 2, true);
              chunks(s4 + 2 + n * 8, e4, (id5, s5) => {
                if (id5 !== 0x4130) return;
                const [mat, at] = cstr(s5);
                const count = view.getUint16(at, true);
                o.groups.push({ mat, faces: Array.from({ length: count }, (_, i) => view.getUint16(at + 2 + i * 2, true)) });
              });
            }
          });
          if (o.verts && o.faces) objects.push(o);
        });
      }
    });
  });
  const builders = new Map();
  let vertices = 0, faces = 0;
  for (const o of objects) {
    const nf = o.faces.length / 3;
    const owner = new Array(nf).fill("");
    for (const g of o.groups) for (const f of g.faces) if (f < nf) owner[f] = g.mat;
    for (let f = 0; f < nf; f++) {
      const mat = owner[f];
      let b = builders.get(mat);
      if (!b) builders.set(mat, b = new DxBuilder(builders.size, mat || o.name));
      for (let k = 0; k < 3; k++) {
        const i = o.faces[f * 3 + k];
        b.corner([o.verts[i * 3], o.verts[i * 3 + 1], o.verts[i * 3 + 2]], o.uv ? [o.uv[i * 2], 1 - o.uv[i * 2 + 1]] : null, null, null);
      }
    }
    vertices += o.verts.length / 3;
    faces += nf;
  }
  const surfaces = [...builders].filter(([, b]) => b.count).map(([mat, b]) => { const m = materials.get(mat) || {}; return b.done({ ...(m.texRef ? { texRef: m.texRef } : {}), color: m.color || null }); });
  return { surfaces: dxZupSurfaces(surfaces), vertices, faces, note: `${objects.length} objects, ${materials.size} materials` };
}

// -------------------------------------------------------------------------------- PLY --
function dxParsePly(buffer) {
  const u8 = new Uint8Array(buffer);
  const headEnd = dxUtf8(u8.subarray(0, Math.min(u8.length, 65536))).indexOf("end_header");
  if (!dxUtf8(u8.subarray(0, 4)).startsWith("ply") || headEnd < 0) throw new Error("Not a PLY file");
  const header = dxUtf8(u8.subarray(0, headEnd)).split(/\r?\n/);
  let bodyAt = headEnd + "end_header".length;
  while (u8[bodyAt] === 13 || u8[bodyAt] === 10) { bodyAt++; if (u8[bodyAt - 1] === 10) break; }
  const format = header.find((l) => l.startsWith("format"))?.split(/\s+/)[1] || "ascii";
  const texRef = header.find((l) => /^comment\s+TextureFile\s+/i.test(l))?.replace(/^comment\s+TextureFile\s+/i, "").trim();
  const elements = [];
  for (const line of header) {
    const p = line.trim().split(/\s+/);
    if (p[0] === "element") elements.push({ name: p[1], count: +p[2], props: [] });
    else if (p[0] === "property" && elements.length) elements.at(-1).props.push(p[1] === "list" ? { list: true, countType: p[2], type: p[3], name: p[4] } : { type: p[1], name: p[2] });
  }
  const SIZE = { char: 1, uchar: 1, int8: 1, uint8: 1, short: 2, ushort: 2, int16: 2, uint16: 2, int: 4, uint: 4, int32: 4, uint32: 4, float: 4, float32: 4, double: 8, float64: 8 };
  const view = new DataView(buffer);
  const le = format === "binary_little_endian";
  let at = bodyAt;
  const tokens = format === "ascii" ? dxUtf8(u8.subarray(bodyAt)).split(/\s+/).filter(Boolean) : null;
  let tk = 0;
  const readOne = (type) => {
    if (tokens) return +tokens[tk++];
    const s = SIZE[type], u = /u/.test(type);
    let v;
    switch (s) {
      case 1: v = u ? view.getUint8(at) : view.getInt8(at); break;
      case 2: v = u ? view.getUint16(at, le) : view.getInt16(at, le); break;
      case 4: v = /float/.test(type) ? view.getFloat32(at, le) : u ? view.getUint32(at, le) : view.getInt32(at, le); break;
      default: v = view.getFloat64(at, le);
    }
    at += s;
    return v;
  };
  const data = {};
  for (const el of elements) {
    const rows = [];
    for (let i = 0; i < el.count; i++) {
      const row = {};
      for (const p of el.props) {
        if (p.list) { const n = readOne(p.countType); row[p.name] = Array.from({ length: n }, () => readOne(p.type)); }
        else row[p.name] = readOne(p.type);
      }
      rows.push(row);
    }
    data[el.name] = rows;
  }
  const verts = data.vertex || [];
  const b = new DxBuilder(0, texRef ? texRef.replace(/\.[^.]+$/, "") : "");
  const uvOf = (v) => v.s !== undefined ? [v.s, 1 - v.t] : v.u !== undefined ? [v.u, 1 - v.v] : v.texture_u !== undefined ? [v.texture_u, 1 - v.texture_v] : null;
  const colOf = (v) => v.red !== undefined ? [v.red / 255, v.green / 255, v.blue / 255, v.alpha !== undefined ? v.alpha / 255 : 1] : null;
  const norOf = (v) => v.nx !== undefined ? [v.nx, v.ny, v.nz] : null;
  let faces = 0;
  for (const f of data.face || []) {
    const idx = f.vertex_indices || f.vertex_index || [];
    const tc = f.texcoord;
    for (let i = 1; i + 1 < idx.length && b.count < DX_VERT_CAP; i++) for (const k of [0, i, i + 1]) {
      const v = verts[idx[k]];
      if (!v) continue;
      b.corner([v.x, v.y, v.z], tc ? [tc[k * 2], 1 - tc[k * 2 + 1]] : uvOf(v), norOf(v), colOf(v));
    }
    faces++;
  }
  if (!faces && verts.length) throw new Error("A point cloud (no faces): nothing to draw as a surface");
  return { surfaces: b.count ? [b.done(texRef ? { texRef } : {})] : [], vertices: verts.length, faces, note: `PLY ${format}${texRef ? `, texture ${texRef}` : ""}` };
}

// -------------------------------------------------------------------------------- STL --
function dxParseStl(buffer) {
  const view = new DataView(buffer);
  const n = buffer.byteLength >= 84 ? view.getUint32(80, true) : 0;
  const b = new DxBuilder(0, "");
  if (buffer.byteLength === 84 + n * 50) {
    for (let i = 0; i < n && b.count < DX_VERT_CAP; i++) {
      const o = 84 + i * 50;
      const nrm = [view.getFloat32(o, true), view.getFloat32(o + 4, true), view.getFloat32(o + 8, true)];
      const ok = Math.hypot(...nrm) > 0.5;
      for (let k = 0; k < 3; k++) b.corner([view.getFloat32(o + 12 + k * 12, true), view.getFloat32(o + 16 + k * 12, true), view.getFloat32(o + 20 + k * 12, true)], null, ok ? nrm : [0, 0, 0], null);
    }
    const s = b.done();
    return { surfaces: [s.nrm.some((x) => x) ? s : { ...s, nrm: dxFlatNormals(s.pos) }], vertices: n * 3, faces: n, note: "STL (no textures in this format)" };
  }
  const nums = [...dxUtf8(new Uint8Array(buffer)).matchAll(/vertex\s+(\S+)\s+(\S+)\s+(\S+)/g)].map((m) => [+m[1], +m[2], +m[3]]);
  for (let i = 0; i + 2 < nums.length; i += 3) for (let k = 0; k < 3; k++) b.corner(nums[i + k], null, null, null);
  return { surfaces: b.count ? [b.done()] : [], vertices: nums.length, faces: Math.floor(nums.length / 3), note: "ASCII STL (no textures in this format)" };
}

// ------------------------------------------------------------------- DirectX .x (text) --
function dxParseX(buffer) {
  const head = dxUtf8(new Uint8Array(buffer, 0, Math.min(16, buffer.byteLength)));
  if (!head.startsWith("xof ")) throw new Error("Not a DirectX .x model");
  if (head.slice(8, 11) !== "txt") throw new Error(`Only text .x models can be shown (this one is "${head.slice(8, 12)}")`);
  const src = dxUtf8(new Uint8Array(buffer)).slice(16).replace(/\/\/[^\n]*|#[^\n]*/g, "");
  // Tokens: { } identifiers numbers strings; ; and , only separate values.
  const tokens = src.match(/"[^"]*"|[{}]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|[A-Za-z_][\w.-]*/g) || [];
  let i = 0;
  const parseBlock = () => {
    // at: Type [name] {  … }
    const node = { type: tokens[i++], name: "", data: [], children: [], ref: "" };
    if (tokens[i] !== "{") node.name = tokens[i++];
    i++;
    while (i < tokens.length && tokens[i] !== "}") {
      const t = tokens[i];
      if (t === "{") { i++; node.children.push({ type: "ref", ref: tokens[i++] }); i++; }
      else if (/^[A-Za-z_]/.test(t) && (tokens[i + 1] === "{" || tokens[i + 2] === "{")) node.children.push(parseBlock());
      else { node.data.push(t.startsWith('"') ? t.slice(1, -1) : +t); i++; }
    }
    i++;
    return node;
  };
  const top = [];
  while (i < tokens.length) {
    if (tokens[i] === "template") { while (i < tokens.length && tokens[i] !== "}") i++; i++; continue; }
    if (/^[A-Za-z_]/.test(tokens[i]) && (tokens[i + 1] === "{" || tokens[i + 2] === "{")) top.push(parseBlock()); else i++;
  }
  const named = new Map();
  const index = (n) => { if (n.name) named.set(n.name, n); for (const c of n.children || []) index(c); };
  top.forEach(index);
  const matLook = (m) => {
    if (!m) return {};
    if (m.type === "ref") m = named.get(m.ref);
    if (!m) return {};
    const tex = m.children.find((c) => c.type === "TextureFilename" || c.type === "TextureFileName");
    return { name: m.name, color: m.data.length >= 4 ? [m.data[0], m.data[1], m.data[2], 1] : null, ...(tex?.data[0] ? { texRef: String(tex.data[0]) } : {}) };
  };
  const builders = new Map();
  let vertices = 0, faces = 0;
  const addMesh = (mesh, world) => {
    const d = mesh.data;
    let p = 0;
    const nv = d[p++];
    const verts = d.slice(p, p + nv * 3); p += nv * 3;
    const nf = d[p++];
    const polys = [];
    for (let f = 0; f < nf; f++) { const k = d[p++]; polys.push(d.slice(p, p + k)); p += k; }
    const uvNode = mesh.children.find((c) => c.type === "MeshTextureCoords");
    const uv = uvNode ? uvNode.data.slice(1) : null;
    const matList = mesh.children.find((c) => c.type === "MeshMaterialList");
    const nMat = matList?.data[0] || 0, faceMat = matList ? matList.data.slice(2, 2 + matList.data[1]) : [];
    const mats = (matList?.children || []).slice(0, Math.max(nMat, 1)).map(matLook);
    polys.forEach((poly, f) => {
      const mi = faceMat.length === 1 ? faceMat[0] : faceMat[f] ?? 0;
      const key = `${mesh.name}|${mi}`;
      let b = builders.get(key);
      if (!b) builders.set(key, b = Object.assign(new DxBuilder(builders.size, mats[mi]?.name || mesh.name || ""), { look: mats[mi] || {} }));
      for (let k = 1; k + 1 < poly.length; k++) for (const v of [poly[0], poly[k], poly[k + 1]])
        b.corner(dxApply(world, [verts[v * 3], verts[v * 3 + 1], verts[v * 3 + 2]]), uv ? [uv[v * 2], uv[v * 2 + 1]] : null, null, null);
    });
    vertices += nv; faces += nf;
  };
  const walk = (n, parent, depth = 0) => {
    if (depth > 64) return;
    let m = parent;
    const ft = n.children?.find((c) => c.type === "FrameTransformMatrix");
    if (ft && ft.data.length >= 16) m = dxMul(parent, ft.data.slice(0, 16));
    if (n.type === "Mesh") addMesh(n, m);
    for (const c of n.children || []) if (c.type === "Frame" || c.type === "Mesh") walk(c, m, depth + 1);
  };
  for (const n of top) if (n.type === "Frame" || n.type === "Mesh") walk(n, dxI());
  const surfaces = [...builders.values()].filter((b) => b.count).map((b) => { const { name, ...look } = b.look; return b.done(look); });
  return { surfaces, vertices, faces, note: "DirectX .x" };
}

// --------------------------------------------------------------------- Quake MD2 / MD3 --
function dxParseMd2(buffer) {
  const v = new DataView(buffer), u8 = new Uint8Array(buffer);
  if (dxCstr(u8, 0, 4) !== "IDP2") throw new Error("Not a Quake 2 MD2 model");
  const H = (i) => v.getInt32(i * 4, true);
  const [sw, sh, , nSkins, nXyz, nSt, nTris, , nFrames, oSkins, oSt, oTris, oFrames] = [H(2), H(3), H(4), H(5), H(6), H(7), H(8), H(9), H(10), H(11), H(12), H(13), H(14)];
  const skins = Array.from({ length: nSkins }, (_, i) => dxCstr(u8, oSkins + i * 64, 64));
  const scale = [0, 1, 2].map((k) => v.getFloat32(oFrames + k * 4, true)), tr = [0, 1, 2].map((k) => v.getFloat32(oFrames + 12 + k * 4, true));
  const vert = (i) => dxZup([0, 1, 2].map((k) => u8[oFrames + 40 + i * 4 + k] * scale[k] + tr[k]));
  const b = new DxBuilder(0, skins[0] ? skins[0].split("/").pop() : "skin");
  for (let t = 0; t < nTris; t++) {
    const o = oTris + t * 12;
    for (const k of [0, 2, 1]) {
      const vi = v.getUint16(o + k * 2, true), si = v.getUint16(o + 6 + k * 2, true);
      if (vi >= nXyz || si >= nSt) continue;
      b.corner(vert(vi), [v.getInt16(oSt + si * 4, true) / sw, v.getInt16(oSt + si * 4 + 2, true) / sh], null, null);
    }
  }
  return { surfaces: b.count ? [b.done(skins[0] ? { texRef: skins[0], texRefs: skins } : {})] : [], vertices: nXyz, faces: nTris,
    note: `MD2, frame 1 of ${nFrames}, ${nSkins} skin${nSkins === 1 ? "" : "s"}` };
}

function dxParseMd3(buffer) {
  const v = new DataView(buffer), u8 = new Uint8Array(buffer);
  if (dxCstr(u8, 0, 4) !== "IDP3") throw new Error("Not a Quake 3 MD3 model");
  const nFrames = v.getInt32(76, true), nSurf = v.getInt32(84, true);
  let at = v.getInt32(100, true), vertices = 0, faces = 0;
  const surfaces = [];
  for (let s = 0; s < nSurf && at + 108 <= buffer.byteLength; s++) {
    const name = dxCstr(u8, at + 4, 64);
    const nShaders = v.getInt32(at + 76, true), nVerts = v.getInt32(at + 80, true), nTris = v.getInt32(at + 84, true);
    const oTris = at + v.getInt32(at + 88, true), oSh = at + v.getInt32(at + 92, true), oSt = at + v.getInt32(at + 96, true), oXyz = at + v.getInt32(at + 100, true);
    const shader = nShaders ? dxCstr(u8, oSh, 64) : "";
    const b = new DxBuilder(s, name);
    for (let t = 0; t < nTris; t++) for (const k of [0, 2, 1]) {
      const i = v.getInt32(oTris + t * 12 + k * 4, true);
      if (i < 0 || i >= nVerts) continue;
      const p = dxZup([0, 1, 2].map((c) => v.getInt16(oXyz + i * 8 + c * 2, true) / 64));
      b.corner(p, [v.getFloat32(oSt + i * 8, true), v.getFloat32(oSt + i * 8 + 4, true)], null, null);
    }
    if (b.count) surfaces.push(b.done(shader ? { texRef: shader } : {}));
    vertices += nVerts; faces += nTris;
    at += v.getInt32(at + 104, true);
  }
  return { surfaces, vertices, faces, note: `MD3, frame 1 of ${nFrames}` };
}

// ------------------------------------------------------ MDL: Quake 1 / GoldSrc (Half-Life) --
function dxPalCanvas(w, h, idx, palette, transparent255 = false) {
  const canvas = document.createElement("canvas");
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(w, h);
  for (let i = 0; i < w * h; i++) {
    const c = idx[i];
    img.data[i * 4] = palette[c * 3]; img.data[i * 4 + 1] = palette[c * 3 + 1]; img.data[i * 4 + 2] = palette[c * 3 + 2];
    img.data[i * 4 + 3] = transparent255 && c === 255 ? 0 : 255;
  }
  ctx.putImageData(img, 0, 0);
  return canvas;
}

async function dxParseMdl(buffer, ctx) {
  const u8 = new Uint8Array(buffer), v = new DataView(buffer);
  const id = dxCstr(u8, 0, 4), version = v.getInt32(4, true);
  if (id === "IDPO") return dxParseMdlQuake(buffer, ctx);
  if (id !== "IDST") throw new Error("Not a Quake or Half-Life MDL model");
  if (version !== 10) throw new Error(`Source engine model (version ${version}): its mesh is in the .vvd/.vtx files next to it, which REA's viewer doesn't read yet`);
  const I = (o) => v.getInt32(o, true);
  // Textures live in this file or in "<name>T.mdl" next to it.
  let tex = { buf: buffer, v, u8 };
  if (!I(180)) {
    const tb = await ctx.fetchRef(`${ctx.stem}t.mdl`) || await ctx.fetchRef(`${ctx.stem}T.mdl`);
    if (tb) tex = { buf: tb, v: new DataView(tb), u8: new Uint8Array(tb) };
  }
  const tI = (o) => tex.v.getInt32(o, true);
  const textures = [];
  for (let i = 0; i < tI(180); i++) {
    const o = tI(184) + i * 80;
    const name = dxCstr(tex.u8, o, 64), flags = tI(o + 64), w = tI(o + 68), h = tI(o + 72), at = tI(o + 76);
    if (w <= 0 || h <= 0 || at + w * h + 768 > tex.buf.byteLength) { textures.push({ name, w: 1, h: 1 }); continue; }
    textures.push({ name, w, h, canvas: dxPalCanvas(w, h, tex.u8.subarray(at, at + w * h), tex.u8.subarray(at + w * h, at + w * h + 768), Boolean(flags & 0x40)) });
  }
  const numSkinRef = tI(192), skins = tI(200);
  const skinTex = (ref) => textures[tex.v.getInt16(skins + ref * 2, true)] || textures[ref];
  // Bones in their rest pose.
  const bones = [];
  for (let i = 0; i < I(140); i++) {
    const o = I(144) + i * 112;
    const parent = I(o + 32), val = [0, 1, 2, 3, 4, 5].map((k) => v.getFloat32(o + 64 + k * 4, true));
    const [r, p, y] = [val[3] / 2, val[4] / 2, val[5] / 2];
    const sr = Math.sin(r), cr = Math.cos(r), sp = Math.sin(p), cp = Math.cos(p), sy = Math.sin(y), cy = Math.cos(y);
    const q = [sr * cp * cy - cr * sp * sy, cr * sp * cy + sr * cp * sy, cr * cp * sy - sr * sp * cy, cr * cp * cy + sr * sp * sy];
    const local = dxQuatTRS(q, val.slice(0, 3));
    bones.push(parent >= 0 && bones[parent] ? dxMul(bones[parent], local) : local);
  }
  const builders = new Map();
  let vertices = 0, faces = 0;
  for (let bp = 0; bp < I(204); bp++) {
    const bo = I(208) + bp * 76;
    if (I(bo + 64) < 1) continue;
    const mo = I(bo + 72);                                   // first (default) sub-model
    const nMesh = I(mo + 72), meshAt = I(mo + 76), nVerts = I(mo + 80), vinfo = I(mo + 84), vAt = I(mo + 88);
    const nNorms = I(mo + 92), ninfo = I(mo + 96), nAt = I(mo + 100);
    const vert = (i) => dxZup(dxApply(bones[u8[vinfo + i]] || dxI(), [0, 1, 2].map((k) => v.getFloat32(vAt + i * 12 + k * 4, true))));
    const norm = (i) => i < nNorms ? dxZup(dxApplyN(bones[u8[ninfo + i]] || dxI(), [0, 1, 2].map((k) => v.getFloat32(nAt + i * 12 + k * 4, true)))) : null;
    for (let m = 0; m < nMesh; m++) {
      const me = meshAt + m * 20;
      const skinref = I(me + 8);
      const t = skinref < numSkinRef ? skinTex(skinref) : textures[skinref];
      let b = builders.get(skinref);
      if (!b) builders.set(skinref, b = Object.assign(new DxBuilder(builders.size, t?.name || `skin ${skinref}`), { tex: t }));
      let p = I(me + 4);
      for (let guard = 0; guard < 100000; guard++) {
        let n = v.getInt16(p, true); p += 2;
        if (!n) break;
        const fan = n < 0; n = Math.abs(n);
        const cs = [];
        for (let k = 0; k < n; k++, p += 8) {
          const vi = v.getInt16(p, true), ni = v.getInt16(p + 2, true), s = v.getInt16(p + 4, true), tt = v.getInt16(p + 6, true);
          if (vi < nVerts) cs.push([vert(vi), t?.w ? [s / t.w, tt / t.h] : null, norm(ni), null]);
        }
        for (let k = 2; k < cs.length; k++) {
          const tri = fan ? [cs[0], cs[k - 1], cs[k]] : k % 2 ? [cs[k - 1], cs[k - 2], cs[k]] : [cs[k - 2], cs[k - 1], cs[k]];
          for (const c of tri) b.corner(...c);
          faces++;
        }
      }
      vertices += 0;
    }
    vertices += nVerts;
  }
  const surfaces = [...builders.values()].filter((b) => b.count).map((b) => b.done(b.tex?.canvas ? { texCanvas: b.tex.canvas, texRef: b.tex.name } : b.tex?.name ? { texRef: b.tex.name } : {}));
  return { surfaces, vertices, faces, note: `Half-Life model, rest pose, ${textures.length} embedded texture${textures.length === 1 ? "" : "s"}${!I(180) && tex.buf === buffer ? " (texture file *T.mdl not found)" : ""}` };
}

async function dxParseMdlQuake(buffer, ctx) {
  const u8 = new Uint8Array(buffer), v = new DataView(buffer);
  const F = (o) => v.getFloat32(o, true), I = (o) => v.getInt32(o, true);
  const scale = [F(8), F(12), F(16)], tr = [F(20), F(24), F(28)];
  const nSkins = I(48), w = I(52), h = I(56), nVerts = I(60), nTris = I(64), nFrames = I(68);
  // The palette comes from the game (gfx/palette.lmp); without it, grey shades.
  const pal = await ctx.find("palette.lmp");
  const palette = pal && pal.byteLength >= 768 ? new Uint8Array(pal, 0, 768) : Uint8Array.from({ length: 768 }, (_, i) => Math.floor(i / 3));
  let p = 84, skin = null;
  for (let s = 0; s < nSkins; s++) {
    const group = I(p); p += 4;
    if (!group) { if (!skin) skin = u8.subarray(p, p + w * h); p += w * h; }
    else { const n = I(p); p += 4 + n * 4; if (!skin) skin = u8.subarray(p, p + w * h); p += n * w * h; }
  }
  const st = p; p += nVerts * 12;
  const tris = p; p += nTris * 16;
  let frame = p;
  if (I(frame) !== 0) frame += 4 + 4 + 8 + I(frame + 4) * 4; else frame += 4;   // group: count, min, max, times
  const vertsAt = frame + 8 + 16;
  const b = new DxBuilder(0, "skin");
  for (let t = 0; t < nTris; t++) {
    const front = I(tris + t * 16);
    for (const k of [0, 2, 1]) {
      const i = I(tris + t * 16 + 4 + k * 4);
      if (i < 0 || i >= nVerts) continue;
      let s = I(st + i * 12 + 4);
      if (!front && I(st + i * 12)) s += w / 2;
      b.corner(dxZup([0, 1, 2].map((c) => u8[vertsAt + i * 4 + c] * scale[c] + tr[c])), [(s + 0.5) / w, (I(st + i * 12 + 8) + 0.5) / h], null, null);
    }
  }
  return { surfaces: b.count ? [b.done(skin ? { texCanvas: dxPalCanvas(w, h, skin, palette) } : {})] : [], vertices: nVerts, faces: nTris,
    note: `Quake model, frame 1 of ${nFrames}${pal ? "" : ", palette.lmp not found (grey skin)"}` };
}

// ----------------------------------------------------------------------- Valve SMD (text) --
function dxParseSmd(text) {
  const lines = text.split(/\r?\n/);
  const at = lines.findIndex((l) => l.trim() === "triangles");
  if (at < 0) throw new Error("No triangles in this SMD (an animation-only file?)");
  const builders = new Map();
  let faces = 0;
  for (let i = at + 1; i + 3 < lines.length; i += 4) {
    const mat = lines[i].trim();
    if (mat === "end") break;
    let b = builders.get(mat);
    if (!b) builders.set(mat, b = new DxBuilder(builders.size, mat));
    for (let k = 1; k <= 3; k++) {
      const p = lines[i + k].trim().split(/\s+/).map(Number);
      b.corner(dxZup([p[1], p[2], p[3]]), [p[7], 1 - p[8]], dxZup([p[4], p[5], p[6]]), null);
    }
    faces++;
  }
  const surfaces = [...builders.values()].filter((b) => b.count).map((b) => b.done({ texRef: b.name }));
  return { surfaces, vertices: faces * 3, faces, note: "Valve SMD (reference pose)" };
}

// ------------------------------------------------------------------- TGA / DDS / PCX images --
function dxCanvas(w, h, rgba) {
  const c = document.createElement("canvas");
  c.width = w; c.height = h;
  const ctx = c.getContext("2d");
  const img = ctx.createImageData(w, h);
  img.data.set(rgba.subarray(0, w * h * 4));
  ctx.putImageData(img, 0, 0);
  return c;
}

function dxDecodeImage(ext, buffer) {
  if (ext === "tga") return dxDecodeTga(buffer);
  if (ext === "dds") return dxDecodeDds(buffer);
  if (ext === "pcx") return dxDecodePcx(buffer);
  throw new Error(`No decoder for .${ext}`);
}

function dxDecodeTga(buffer) {
  const u8 = new Uint8Array(buffer), v = new DataView(buffer);
  const idLen = u8[0], cmType = u8[1], type = u8[2], cmFirst = v.getUint16(3, true), cmLen = v.getUint16(5, true), cmBits = u8[7];
  const w = v.getUint16(12, true), h = v.getUint16(14, true), bpp = u8[16], desc = u8[17];
  if (![1, 2, 3, 9, 10, 11].includes(type) || !w || !h) throw new Error("Unsupported TGA");
  let p = 18 + idLen;
  const cmBytes = Math.ceil(cmBits / 8);
  const cmap = cmType ? u8.subarray(p, p + cmLen * cmBytes) : null;
  p += cmType ? cmLen * cmBytes : 0;
  const px = Math.ceil(bpp / 8);
  const out = new Uint8Array(w * h * 4);
  const put = (i, s, bytes, bits) => {
    if (bits === 8 && !cmap) { out[i] = out[i + 1] = out[i + 2] = s[0]; out[i + 3] = 255; }
    else if (bits <= 16 && bytes === 2) { const c = s[0] | (s[1] << 8); out[i] = ((c >> 10) & 31) * 255 / 31; out[i + 1] = ((c >> 5) & 31) * 255 / 31; out[i + 2] = (c & 31) * 255 / 31; out[i + 3] = 255; }
    else { out[i] = s[2]; out[i + 1] = s[1]; out[i + 2] = s[0]; out[i + 3] = bytes === 4 ? s[3] : 255; }
  };
  const pixel = (i, at) => {
    if (cmap) { const idx = (px === 2 ? u8[at] | (u8[at + 1] << 8) : u8[at]) - cmFirst; put(i, cmap.subarray(idx * cmBytes, idx * cmBytes + cmBytes), cmBytes, cmBits); }
    else put(i, u8.subarray(at, at + px), px, bpp);
  };
  let n = 0;
  if (type < 9) { for (; n < w * h; n++, p += px) pixel(n * 4, p); }
  else while (n < w * h && p < u8.length) {
    const head = u8[p++], count = (head & 127) + 1;
    if (head & 128) { for (let k = 0; k < count && n < w * h; k++) pixel(n++ * 4, p); p += px; }
    else for (let k = 0; k < count && n < w * h; k++, p += px) pixel(n++ * 4, p);
  }
  // Bottom-up unless descriptor bit 5 says top-down.
  if (!(desc & 32)) {
    const row = w * 4, tmp = new Uint8Array(row);
    for (let y = 0; y < h >> 1; y++) { const a = y * row, b = (h - 1 - y) * row; tmp.set(out.subarray(a, a + row)); out.copyWithin(a, b, b + row); out.set(tmp, b); }
  }
  if (bpp === 32 && !out.some((x, i) => i % 4 === 3 && x)) for (let i = 3; i < out.length; i += 4) out[i] = 255;
  return dxCanvas(w, h, out);
}

function dxDecodeDds(buffer) {
  const v = new DataView(buffer), u8 = new Uint8Array(buffer);
  if (v.getUint32(0, true) !== 0x20534444) throw new Error("Not a DDS file");
  const h = v.getUint32(12, true), w = v.getUint32(16, true);
  const four = dxCstr(u8, 84, 4), rgbBits = v.getUint32(88, true), pfFlags = v.getUint32(80, true);
  let at = 128;
  let fmt = four;
  if (four === "DX10") { const dxgi = v.getUint32(128, true); at = 148; fmt = { 71: "DXT1", 72: "DXT1", 74: "DXT3", 75: "DXT3", 77: "DXT5", 78: "DXT5", 28: "RGBA", 87: "BGRA" }[dxgi] || `DXGI ${dxgi}`; }
  const out = new Uint8Array(w * h * 4);
  const c565 = (c) => [((c >> 11) & 31) * 255 / 31, ((c >> 5) & 63) * 255 / 63, (c & 31) * 255 / 31];
  if (/^DXT[1-5]$/.test(fmt)) {
    const dxt1 = fmt === "DXT1", blockSize = dxt1 ? 8 : 16;
    for (let by = 0; by < Math.ceil(h / 4); by++) for (let bx = 0; bx < Math.ceil(w / 4); bx++, at += blockSize) {
      const cb = dxt1 ? at : at + 8;
      const c0 = v.getUint16(cb, true), c1 = v.getUint16(cb + 2, true), bits = v.getUint32(cb + 4, true);
      const a = c565(c0), b = c565(c1);
      const pal = c0 > c1 || !dxt1 ? [a, b, a.map((x, i) => (2 * x + b[i]) / 3), a.map((x, i) => (x + 2 * b[i]) / 3)] : [a, b, a.map((x, i) => (x + b[i]) / 2), [0, 0, 0]];
      // DXT5 interpolated alpha / DXT3 explicit alpha.
      let alpha = null;
      if (fmt === "DXT5" || fmt === "DXT4") {
        const a0 = u8[at], a1 = u8[at + 1];
        const ap = a0 > a1 ? [a0, a1, ...[1, 2, 3, 4, 5, 6].map((k) => ((7 - k) * a0 + k * a1) / 7)] : [a0, a1, ...[1, 2, 3, 4].map((k) => ((5 - k) * a0 + k * a1) / 5), 0, 255];
        let abits = 0n;
        for (let k = 0; k < 6; k++) abits |= BigInt(u8[at + 2 + k]) << BigInt(8 * k);
        alpha = Array.from({ length: 16 }, (_, k) => ap[Number((abits >> BigInt(3 * k)) & 7n)]);
      } else if (fmt === "DXT3" || fmt === "DXT2") alpha = Array.from({ length: 16 }, (_, k) => ((u8[at + (k >> 1)] >> ((k & 1) * 4)) & 15) * 17);
      for (let k = 0; k < 16; k++) {
        const x = bx * 4 + (k & 3), y = by * 4 + (k >> 2);
        if (x >= w || y >= h) continue;
        const ci = (bits >> (2 * k)) & 3, o = (y * w + x) * 4;
        out[o] = pal[ci][0]; out[o + 1] = pal[ci][1]; out[o + 2] = pal[ci][2];
        out[o + 3] = alpha ? alpha[k] : dxt1 && c0 <= c1 && ci === 3 ? 0 : 255;
      }
    }
  } else if (fmt === "RGBA" || fmt === "BGRA" || (!four.trim() || four === "\0\0\0\0") && (pfFlags & 0x40) && (rgbBits === 32 || rgbBits === 24)) {
    const bytes = fmt === "RGBA" || fmt === "BGRA" ? 4 : rgbBits / 8;
    const rmask = fmt === "RGBA" ? 0xff : fmt === "BGRA" ? 0xff0000 : v.getUint32(92, true);
    for (let i = 0; i < w * h; i++) {
      const o = at + i * bytes, r = rmask === 0xff ? 0 : 2, b = rmask === 0xff ? 2 : 0;
      out[i * 4] = u8[o + r]; out[i * 4 + 1] = u8[o + 1]; out[i * 4 + 2] = u8[o + b]; out[i * 4 + 3] = bytes === 4 ? u8[o + 3] : 255;
    }
  } else throw new Error(`DDS format ${fmt.trim() || "uncompressed (unusual masks)"} isn't supported`);
  return dxCanvas(w, h, out);
}

function dxDecodePcx(buffer) {
  const u8 = new Uint8Array(buffer), v = new DataView(buffer);
  if (u8[0] !== 10) throw new Error("Not a PCX file");
  const bpp = u8[3], w = v.getUint16(8, true) - v.getUint16(4, true) + 1, h = v.getUint16(10, true) - v.getUint16(6, true) + 1;
  const planes = u8[65], bpl = v.getUint16(66, true);
  const line = new Uint8Array(planes * bpl);
  const out = new Uint8Array(w * h * 4);
  const palette = bpp === 8 && planes === 1 && u8[u8.length - 769] === 12 ? u8.subarray(u8.length - 768) : null;
  let p = 128;
  for (let y = 0; y < h; y++) {
    for (let n = 0; n < line.length && p < u8.length;) {
      let c = u8[p++], count = 1;
      if ((c & 0xc0) === 0xc0) { count = c & 0x3f; c = u8[p++]; }
      while (count-- && n < line.length) line[n++] = c;
    }
    for (let x = 0; x < w; x++) {
      const o = (y * w + x) * 4;
      if (palette) { const c = line[x]; out[o] = palette[c * 3]; out[o + 1] = palette[c * 3 + 1]; out[o + 2] = palette[c * 3 + 2]; }
      else if (planes >= 3) { out[o] = line[x]; out[o + 1] = line[bpl + x]; out[o + 2] = line[2 * bpl + x]; }
      else out[o] = out[o + 1] = out[o + 2] = line[x];
      out[o + 3] = 255;
    }
  }
  return dxCanvas(w, h, out);
}
