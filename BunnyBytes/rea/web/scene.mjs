// Walkable 3D preview of a Godot scene (.tscn/.escn) in a game's decompiled/ folder, for the
// Decompiled tab's "Explore in 3D". Flattens the scene into what the page draws:
//   - node transforms through the tree and instanced sub-scenes (with the outer scene's overrides)
//   - meshes: inline ArrayMesh (Godot 3 packed vertex arrays incl. octahedral normals and half
//     floats; Godot 4 uncompressed), text/binary(Godot 3) mesh resources, primitive meshes, CSG
//     shapes, MultiMesh instances, and glTF/OBJ model files (the page parses those itself)
//   - materials: albedo colour/texture (Spatial/StandardMaterial, shader texture params, Godot 3
//     binary .material/.tres), material slots, material_override, vertex colours, transparency
//   - one DirectionalLight, WorldEnvironment colours, a spawn point (Camera or a "spawn" marker)
// Not drawn (counted under "skipped"): GridMap, Sprite3D/Label3D, particles, binary .scn scenes,
// Godot 4 compressed meshes, model formats the page can't parse.
import fsp from "node:fs/promises";
import path from "node:path";
import * as textures from "./textures.mjs";

const MODEL_FILE = /\.(glb|gltf|obj|fbx|dae|3ds|ply|stl|x|md2|md3|mdl|smd)$/i;
const IMAGE = /\.(png|jpe?g|webp|gif|bmp)$/i;
const MAX_DEPTH = 16;
const MAX_INSTANCES = 300000;
const MAX_TEXT = 48 * 1024 * 1024;
const CACHE_MS = 5 * 60 * 1000;
const cache = new Map();

// ------------------------------------------------------------------- Godot text parser --
// Values: numbers, strings, [arrays], {dicts}, Constructor(args) → {$, a}; Pool*/Packed* arrays
// are read straight into number arrays (or bytes, for Godot 4's base64 PackedByteArray).
const POOL = /^(Pool|Packed)\w*Array$/;
function parser(src) {
  const P = { i: 0 };
  const sticky = (re) => { re.lastIndex = P.i; const m = re.exec(src); if (m) P.i = re.lastIndex; return m?.[0]; };
  const NUM = /-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?/y, ID = /[A-Za-z_][\w]*/y;
  P.ws = () => { while (P.i < src.length && src.charCodeAt(P.i) <= 32) P.i++; };
  P.eof = () => P.i >= src.length;
  P.peek = () => src[P.i];
  P.skipLine = () => { const n = src.indexOf("\n", P.i); P.i = n < 0 ? src.length : n + 1; };
  P.re = sticky;
  P.str = () => {
    let j = P.i + 1, s = "";
    while (j < src.length && src[j] !== '"') {
      if (src[j] === "\\") { const c = src[j + 1]; s += c === "n" ? "\n" : c === "t" ? "\t" : c; j += 2; } else s += src[j++];
    }
    P.i = j + 1;
    return s;
  };
  P.val = () => {
    P.ws();
    const c = src[P.i];
    if (c === '"') return P.str();
    if ((c === "&" || c === "^") && src[P.i + 1] === '"') { P.i++; return P.str(); }
    if (c === "[") {
      P.i++;
      const a = [];
      for (P.ws(); P.i < src.length && src[P.i] !== "]"; P.ws()) { a.push(P.val()); P.ws(); if (src[P.i] === ",") P.i++; }
      P.i++;
      return a;
    }
    if (c === "{") {
      P.i++;
      const o = {};
      for (P.ws(); P.i < src.length && src[P.i] !== "}"; P.ws()) {
        const k = P.val(); P.ws();
        if (src[P.i] === ":") P.i++;
        o[String(k)] = P.val(); P.ws();
        if (src[P.i] === ",") P.i++;
      }
      P.i++;
      return o;
    }
    const n = sticky(NUM);
    if (n !== undefined) return Number(n);
    const id = sticky(ID);
    if (id === undefined) { P.i++; return null; }
    if (id === "true") return true;
    if (id === "false") return false;
    if (id === "null" || id === "nil") return null;
    if (id === "inf") return Infinity;
    if (id === "nan") return NaN;
    P.ws();
    if (id === "Array" && src[P.i] === "[") { const close = src.indexOf("]", P.i); P.i = close + 1; P.ws(); } // Array[Type](…)
    if (src[P.i] !== "(") return id;
    P.i++;
    if (POOL.test(id)) {
      const end = src.indexOf(")", P.i);
      const body = src.slice(P.i, end < 0 ? src.length : end).trim();
      P.i = end < 0 ? src.length : end + 1;
      if (body.startsWith('"')) return { $: id, a: [...Buffer.from(body.slice(1, -1), "base64")] };
      return { $: id, a: body ? body.split(",").map(Number) : [] };
    }
    const a = [];
    for (P.ws(); P.i < src.length && src[P.i] !== ")"; P.ws()) { a.push(P.val()); P.ws(); if (src[P.i] === ",") P.i++; }
    P.i++;
    return { $: id, a };
  };
  return P;
}

// A text scene or resource → {format, ext: id→{path,type}, subs: id→{type, props}, nodes, resource}.
export function parseText(src) {
  const P = parser(src);
  const doc = { format: 2, ext: new Map(), subs: new Map(), nodes: [], resource: null };
  let cur = null;
  while (true) {
    P.ws();
    if (P.eof()) break;
    const c = P.peek();
    if (c === ";") { P.skipLine(); continue; }
    if (c === "[") {
      P.i++;
      const tag = P.re(/[A-Za-z_]\w*/y) || "";
      const a = {};
      while (true) {
        P.ws();
        if (P.eof() || P.peek() === "]") { P.i++; break; }
        const k = P.re(/[A-Za-z_]\w*/y);
        if (!k) { P.i++; continue; }
        P.ws();
        if (P.peek() === "=") { P.i++; a[k] = P.val(); }
      }
      cur = { tag, ...a, props: {} };
      if (tag === "gd_scene" || tag === "gd_resource") doc.format = Number(a.format) || 2;
      else if (tag === "ext_resource") { doc.ext.set(String(a.id), { path: String(a.path || ""), type: String(a.type || "") }); cur = null; }
      else if (tag === "sub_resource") doc.subs.set(String(a.id), cur);
      else if (tag === "node") doc.nodes.push(cur);
      else if (tag === "resource") doc.resource = cur;
      else if (tag === "connection" || tag === "editable") cur = null;
      continue;
    }
    const key = P.re(/[^\s=]+/y);
    P.ws();
    if (!key || P.peek() !== "=") { P.skipLine(); continue; }
    P.i++;
    const v = P.val();
    if (cur) cur.props[key.replace(/^"|"$/g, "")] = v;
  }
  return doc;
}

// ---------------------------------------------------------- Godot 3 binary resources --
// Enough of ResourceFormatBinary (format 3, Godot 3.x) for materials and meshes: the external and
// internal resource lists plus every property of the plain Variant types. Godot 4 binaries fall
// back to a string scan for image paths.
export function parseBinary(buf) {
  if (buf.length < 24 || buf.toString("latin1", 0, 4) !== "RSRC") return null;
  const big = buf.readUInt32LE(4) !== 0, real64 = buf.readUInt32LE(8) !== 0, major = buf.readUInt32LE(12);
  if (big || major !== 3) return { scanOnly: true };
  let o = 24;
  const u32 = () => { const v = buf.readUInt32LE(o); o += 4; return v; };
  const f32 = () => { const v = buf.readFloatLE(o); o += 4; return v; };
  const real = () => { if (real64) { const v = buf.readDoubleLE(o); o += 8; return v; } return f32(); };
  const ustr = () => { const n = u32(); const s = buf.toString("utf8", o, o + n).replace(/\0+$/, ""); o += n; return s; };
  const type = ustr();
  o += 8 + 14 * 4;
  const strings = Array.from({ length: u32() }, ustr);
  const doc = { format: 2, binary: true, ext: new Map(), subs: new Map(), nodes: [], resource: null };
  const extCount = u32();
  for (let i = 0; i < extCount; i++) { const t = ustr(); doc.ext.set(String(i), { type: t, path: ustr() }); }
  const internal = Array.from({ length: u32() }, () => { const p = ustr(); const off = Number(buf.readBigUInt64LE(o)); o += 8; return { p, off }; });
  const variant = () => {
    const t = u32();
    switch (t) {
      case 1: return null;
      case 2: return u32() !== 0;
      case 3: return buf.readInt32LE((o += 4) - 4);
      case 40: { const v = Number(buf.readBigInt64LE(o)); o += 8; return v; }
      case 4: return real();
      case 41: { const v = buf.readDoubleLE(o); o += 8; return v; }
      case 5: return ustr();
      case 10: return { $: "Vector2", a: [real(), real()] };
      case 11: case 13: case 14: return { $: "R4", a: [real(), real(), real(), real()] };
      case 12: return { $: "Vector3", a: [real(), real(), real()] };
      case 15: return { $: "AABB", a: Array.from({ length: 6 }, real) };
      case 16: return { $: "Basis", a: Array.from({ length: 9 }, real) };
      case 17: return { $: "Transform", a: Array.from({ length: 12 }, real) };
      case 18: return { $: "Transform2D", a: Array.from({ length: 6 }, real) };
      case 20: return { $: "Color", a: [f32(), f32(), f32(), f32()] };
      case 22: { const names = buf.readUInt16LE(o); const subs = buf.readUInt16LE(o + 2) & 0x7fff; o += 4 + (names + subs) * 4; return { $: "NodePath", a: [] }; }
      case 23: u32(); return null;
      case 24: {
        const k = u32();
        if (k === 0) return null;
        if (k === 1) { const et = ustr(); return { $: "ExtPath", a: [ustr(), et] }; }
        if (k === 2) return { $: "SubResource", a: [String(u32())] };
        if (k === 3) return { $: "ExtResource", a: [String(u32())] };
        throw new Error("object");
      }
      case 26: { const n = u32() & 0x7fffffff; const d = {}; for (let i = 0; i < n; i++) { const key = variant(); d[String(key)] = variant(); } return d; }
      case 30: { const n = u32() & 0x7fffffff; return Array.from({ length: n }, variant); }
      case 31: { const n = u32(); const a = [...buf.subarray(o, o + n)]; o += n + ((4 - (n % 4)) % 4); return { $: "PoolByteArray", a }; }
      case 32: { const n = u32(); const a = Array.from({ length: n }, () => buf.readInt32LE((o += 4) - 4)); return { $: "PoolIntArray", a }; }
      case 33: { const n = u32(); return { $: "PoolRealArray", a: Array.from({ length: n }, real) }; }
      case 34: { const n = u32(); return Array.from({ length: n }, ustr); }
      case 35: { const n = u32(); return { $: "PoolVector3Array", a: Array.from({ length: n * 3 }, real) }; }
      case 36: { const n = u32(); return { $: "PoolColorArray", a: Array.from({ length: n * 4 }, f32) }; }
      case 37: { const n = u32(); return { $: "PoolVector2Array", a: Array.from({ length: n * 2 }, real) }; }
      default: throw new Error(`variant ${t}`);
    }
  };
  internal.forEach(({ p, off }, i) => {
    o = off;
    const res = { type: "", props: {} };
    try {
      res.type = ustr();
      const n = u32();
      for (let k = 0; k < n; k++) { const name = strings[u32()]; res.props[name] = variant(); }
    } catch { /* keep the properties read so far */ }
    const local = p.match(/^local:\/\/(\d+)$/);
    if (local) doc.subs.set(local[1], res);
    if (i === internal.length - 1 || !local) doc.resource = res;
  });
  if (doc.resource && !doc.resource.type) doc.resource.type = type;
  return doc;
}

// ------------------------------------------------------------------------ transforms --
// 12 numbers: basis rows (Godot's Transform(...) text order) then origin.
const XF_I = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0];
const xfOf = (v) => (v?.$ === "Transform" || v?.$ === "Transform3D") && v.a.length === 12 ? v.a.map(Number) : null;
function mul(a, b) {
  const o = new Array(12);
  for (let r = 0; r < 3; r++) {
    for (let c = 0; c < 3; c++) o[r * 3 + c] = a[r * 3] * b[c] + a[r * 3 + 1] * b[3 + c] + a[r * 3 + 2] * b[6 + c];
    o[9 + r] = a[r * 3] * b[9] + a[r * 3 + 1] * b[10] + a[r * 3 + 2] * b[11] + a[9 + r];
  }
  return o;
}
const vec = (v, n) => Array.isArray(v?.a) && v.a.length >= n ? v.a.slice(0, n).map(Number) : null;
const num = (v, d) => Number.isFinite(Number(v)) && v !== null && v !== undefined && typeof v !== "object" ? Number(v) : d;
const color = (v) => v?.$ === "Color" ? [...v.a.map(Number), 1].slice(0, 4) : null;
const refOf = (v) => v && (v.$ === "ExtResource" || v.$ === "SubResource" || v.$ === "ExtPath") ? v : null;

// ------------------------------------------------------------------- mesh decoding --
const half = (h) => {
  const s = h & 0x8000 ? -1 : 1, e = (h >> 10) & 0x1f, f = h & 0x3ff;
  return e === 0 ? s * 2 ** -14 * (f / 1024) : e === 31 ? (f ? NaN : s * Infinity) : s * 2 ** (e - 15) * (1 + f / 1024);
};
const sgn = (x) => x < 0 ? -1 : 1;
function oct(x, y) {
  let z = 1 - Math.abs(x) - Math.abs(y);
  const t = Math.max(-z, 0);
  x += t * -sgn(x); y += t * -sgn(y);
  const l = Math.hypot(x, y, z) || 1;
  return [x / l, y / l, z / l];
}
const b64 = (arr) => Buffer.from(arr.buffer, arr.byteOffset, arr.byteLength).toString("base64");

// Godot 3 VisualServer packed arrays (mesh_surface_make_offsets_from_format).
function decodeGd3(s) {
  const fmt = num(s.format, 0), vc = num(s.vertex_count, 0), ic = num(s.index_count, 0);
  if (num(s.primitive, 4) !== 4) return { skip: "non-triangle mesh surface" };
  const bytes = Uint8Array.from(s.array_data?.a || []);
  if (!vc || !bytes.length) return { skip: "empty mesh surface" };
  const C = (bit) => (fmt & (1 << (bit + 9))) !== 0, has = (bit) => (fmt & (1 << bit)) !== 0;
  const octa = (fmt & (1 << 21)) !== 0, two = (fmt & (1 << 18)) !== 0;
  const size = [
    has(0) ? (two ? (C(0) ? 4 : 8) : (C(0) ? 8 : 12)) : 0,
    has(1) ? (octa ? (C(1) ? 2 : 4) : (C(1) ? 4 : 12)) : 0,
    has(2) ? (octa ? (C(2) ? 2 : 4) : (C(2) ? 4 : 16)) : 0,
    has(3) ? (C(3) ? 4 : 16) : 0,
    has(4) ? (C(4) ? 4 : 8) : 0,
    has(5) ? (C(5) ? 4 : 8) : 0,
    has(6) ? ((fmt & (1 << 19)) ? 8 : 4) : 0,
    has(7) ? (C(7) ? 8 : 16) : 0,
  ];
  const stride = size.reduce((a, b) => a + b, 0);
  if (stride * vc !== bytes.length) return { skip: "unrecognised packed mesh layout" };
  const off = size.map((_, i) => size.slice(0, i).reduce((a, b) => a + b, 0));
  const dv = new DataView(bytes.buffer);
  const pos = new Float32Array(vc * 3), nrm = has(1) ? new Float32Array(vc * 3) : null;
  const uv = has(4) ? new Float32Array(vc * 2) : null, col = has(3) ? new Float32Array(vc * 4) : null;
  for (let v = 0; v < vc; v++) {
    const b = v * stride;
    for (let k = 0; k < (two ? 2 : 3); k++) pos[v * 3 + k] = C(0) ? half(dv.getUint16(b + k * 2, true)) : dv.getFloat32(b + k * 4, true);
    if (nrm) {
      const o = b + off[1];
      let n;
      if (octa) n = C(1) ? oct(Math.max(-1, dv.getInt8(o) / 127), Math.max(-1, dv.getInt8(o + 1) / 127))
        : oct(Math.max(-1, dv.getInt16(o, true) / 32767), Math.max(-1, dv.getInt16(o + 2, true) / 32767));
      else n = C(1) ? [dv.getInt8(o) / 127, dv.getInt8(o + 1) / 127, dv.getInt8(o + 2) / 127]
        : [dv.getFloat32(o, true), dv.getFloat32(o + 4, true), dv.getFloat32(o + 8, true)];
      nrm.set(n, v * 3);
    }
    if (col) {
      const o = b + off[3];
      for (let k = 0; k < 4; k++) col[v * 4 + k] = C(3) ? dv.getUint8(o + k) / 255 : dv.getFloat32(o + k * 4, true);
    }
    if (uv) {
      const o = b + off[4];
      uv[v * 2] = C(4) ? half(dv.getUint16(o, true)) : dv.getFloat32(o, true);
      uv[v * 2 + 1] = C(4) ? half(dv.getUint16(o + 2, true)) : dv.getFloat32(o + 4, true);
    }
  }
  let idx = null;
  if (has(8) && ic) {
    const ib = Uint8Array.from(s.array_index_data?.a || []);
    const wide = vc >= 65536;
    if (ib.length < ic * (wide ? 4 : 2)) return { skip: "truncated mesh indices" };
    const idv = new DataView(ib.buffer);
    idx = new Uint32Array(ic);
    for (let k = 0; k < ic; k++) idx[k] = wide ? idv.getUint32(k * 4, true) : idv.getUint16(k * 2, true);
  }
  return { pos, nrm, uv, col, idx };
}

// Godot 4 surfaces without attribute compression: vertex stream = position (+ octahedral
// normal/tangent as 2 × uint16 each), attribute stream = colour (RGBA8) + UV (2 floats) + ….
function decodeGd4(s) {
  const fmt = num(s.format, 0), vc = num(s.vertex_count, 0);
  if (num(s.primitive, 3) !== 3) return { skip: "non-triangle mesh surface" };
  const vb = Uint8Array.from(s.vertex_data?.a || []);
  const has = (bit) => Math.floor(fmt / 2 ** bit) % 2 === 1;
  const stride = 12 + (has(1) ? 4 : 0) + (has(2) ? 4 : 0);
  if (!vc || vb.length !== vc * stride) return { skip: "Godot 4 compressed mesh" };
  const ab = Uint8Array.from(s.attribute_data?.a || []);
  const astride = (has(3) ? 4 : 0) + (has(4) ? 8 : 0) + (has(5) ? 8 : 0);
  const dv = new DataView(vb.buffer), av = new DataView(ab.buffer);
  const pos = new Float32Array(vc * 3), nrm = has(1) ? new Float32Array(vc * 3) : null;
  const okAttr = astride && ab.length >= vc * astride;
  const uv = has(4) && okAttr ? new Float32Array(vc * 2) : null, col = has(3) && okAttr ? new Float32Array(vc * 4) : null;
  for (let v = 0; v < vc; v++) {
    const b = v * stride;
    for (let k = 0; k < 3; k++) pos[v * 3 + k] = dv.getFloat32(b + k * 4, true);
    if (nrm) nrm.set(oct(dv.getUint16(b + 12, true) / 65535 * 2 - 1, dv.getUint16(b + 14, true) / 65535 * 2 - 1), v * 3);
    const a = v * astride;
    if (col) for (let k = 0; k < 4; k++) col[v * 4 + k] = av.getUint8(a + k) / 255;
    if (uv) { const o = a + (has(3) ? 4 : 0); uv[v * 2] = av.getFloat32(o, true); uv[v * 2 + 1] = av.getFloat32(o + 4, true); }
  }
  let idx = null;
  const ib = Uint8Array.from(s.index_data?.a || []);
  if (ib.length) {
    const wide = vc > 65535, n = ib.length / (wide ? 4 : 2), idv = new DataView(ib.buffer);
    idx = new Uint32Array(n);
    for (let k = 0; k < n; k++) idx[k] = wide ? idv.getUint32(k * 4, true) : idv.getUint16(k * 2, true);
  }
  return { pos, nrm, uv, col, idx };
}

const PRIMS = new Set(["CubeMesh", "BoxMesh", "PlaneMesh", "QuadMesh", "CylinderMesh", "SphereMesh", "CapsuleMesh", "PrismMesh"]);
// Primitive mesh → parameters for the page's generator (Godot 3 and 4 defaults differ).
function primOf(type, p, g4) {
  const v2 = (k, d) => vec(p[k], 2) || d, v3 = (k, d) => vec(p[k], 3) || d;
  switch (type) {
    case "CubeMesh": case "BoxMesh": case "PrismMesh": return { type: "box", size: v3("size", g4 ? [1, 1, 1] : [2, 2, 2]) };
    case "PlaneMesh": return { type: "plane", size: v2("size", g4 ? [2, 2] : [2, 2]), offset: v3("center_offset", [0, 0, 0]) };
    case "QuadMesh": return { type: "quad", size: v2("size", [1, 1]), offset: v3("center_offset", [0, 0, 0]) };
    case "CylinderMesh": return { type: "cylinder", top: num(p.top_radius, g4 ? 0.5 : 1), bottom: num(p.bottom_radius, g4 ? 0.5 : 1), height: num(p.height, 2) };
    case "SphereMesh": return { type: "sphere", radius: num(p.radius, g4 ? 0.5 : 1), height: num(p.height, g4 ? 1 : 2) };
    case "CapsuleMesh": return g4 ? { type: "capsule", radius: num(p.radius, 0.5), mid: Math.max(0, num(p.height, 2) - 2 * num(p.radius, 0.5)), axis: "y" }
      : { type: "capsule", radius: num(p.radius, 1), mid: num(p.mid_height, 1), axis: "z" };
  }
  return null;
}

// ------------------------------------------------------------------------- build --
export async function build(key, root, getTree, scenePath) {
  const ix = await textures.index(key, root, getTree);
  if (!ix.files.has(scenePath) || !/\.(tscn|escn)$/i.test(scenePath)) throw Object.assign(new Error("Not a Godot text scene in this decompiled folder"), { status: 404 });
  const stat = await fsp.stat(path.join(root, scenePath));
  const ck = `${key}:${scenePath}`;
  const hit = cache.get(ck);
  if (hit && hit.mtime === stat.mtimeMs && Date.now() - hit.at < CACHE_MS) return hit.result;

  const docs = new Map();
  const skipped = {};
  const skip = (why, n = 1) => { skipped[why] = (skipped[why] || 0) + n; };
  const load = (p) => {
    if (!docs.has(p)) docs.set(p, (async () => {
      const st = await fsp.stat(path.join(root, p)).catch(() => null);
      if (!st?.isFile() || st.size > MAX_TEXT) return null;
      const buf = await fsp.readFile(path.join(root, p));
      if (buf.toString("latin1", 0, 4) === "RSRC") {
        const bin = parseBinary(buf);
        if (bin && !bin.scanOnly) return bin;
        // Godot 4 binary: image paths only.
        const tex = [...buf.toString("latin1").matchAll(/res:\/\/[^\0"\n]+?\.(?:png|jpe?g|webp|bmp)/gi)].map((m) => m[0]);
        return { scan: tex, ext: new Map(), subs: new Map(), nodes: [], resource: null };
      }
      if (buf.subarray(0, 4096).includes(0)) return null;
      try { return parseText(buf.toString("utf8")); } catch { return null; }
    })());
    return docs.get(p);
  };
  const resolve = (from, res) => {
    if (!res) return null;
    const p = ix.resolveRes(from, String(res));
    return p && ix.files.has(p) ? p : null;
  };
  const extPath = (from, doc, ref) => ref.$ === "ExtPath" ? resolve(from, ref.a[0]) : resolve(from, doc.ext.get(String(ref.a[0]))?.path);

  // Materials → ids.
  const materials = [];
  const matIds = new Map();
  const texOf = async (from, doc, v, depth = 0) => {
    const ref = refOf(v);
    if (!ref || ref.$ === "SubResource" || depth > 2) return null;
    const p = extPath(from, doc, ref);
    if (!p) return null;
    if (IMAGE.test(p)) return p;
    const sub = await load(p);
    if (sub?.scan) return sub.scan.map((r) => resolve(p, r)).find(Boolean) || null;
    for (const val of Object.values(sub?.resource?.props || {})) { const t = await texOf(p, sub, val, depth + 1); if (t) return t; }
    return null;
  };
  const matFrom = async (from, doc, res) => {
    const pr = res.props || {};
    const m = { color: [1, 1, 1, 1], texture: null };
    const c = color(pr.albedo_color);
    if (c) m.color = c;
    if (pr.albedo_texture) m.texture = await texOf(from, doc, pr.albedo_texture);
    const params = Object.entries(pr).filter(([k]) => /^shader_param(eter)?\//.test(k));
    if (params.length) {
      params.sort(([a], [b]) => /albedo|diffuse|base|main|colou?r|tex/i.test(b) - /albedo|diffuse|base|main|colou?r|tex/i.test(a));
      if (!m.texture) for (const [, v] of params) { const t = await texOf(from, doc, v); if (t) { m.texture = t; break; } }
      if (!c) for (const [k, v] of params) if (/albedo|colou?r|base|tint/i.test(k) && color(v)) { m.color = color(v); break; }
    }
    // UV1 scale/offset and triplanar projection (common on Godot terrain).
    const us = vec(pr.uv1_scale, 3), uo = vec(pr.uv1_offset, 3);
    if (us && us.some((x) => x !== 1)) m.uvScale = us;
    if (uo && uo.some((x) => x !== 0)) m.uvOffset = uo;
    if (pr.uv1_triplanar === true) { m.triplanar = true; if (pr.uv1_world_triplanar === true) m.triWorld = true; }
    if (pr.vertex_color_use_as_albedo === true || pr.flags_vertex_color_use_as_albedo === true) m.vc = true;
    if (pr.flags_unshaded === true || pr.shading_mode === 0) m.unshaded = true;
    if (pr.flags_transparent === true || (doc.format >= 3 && num(pr.transparency, 0) === 1) || m.color[3] < 0.99) m.blend = true;
    if (pr.params_use_alpha_scissor === true || (doc.format >= 3 && num(pr.transparency, 0) === 2)) { m.blend = false; m.scissor = true; }
    return m;
  };
  const material = async (from, doc, v) => {
    const ref = refOf(v);
    if (!ref) return null;
    const p = ref.$ === "SubResource" ? null : extPath(from, doc, ref);
    const id = ref.$ === "SubResource" ? `${from}#${ref.a[0]}` : p;
    if (!id) return null;
    if (matIds.has(id)) return matIds.get(id);
    let m = null;
    if (ref.$ === "SubResource") { const res = doc.subs.get(String(ref.a[0])); if (res) m = await matFrom(from, doc, res); }
    else if (IMAGE.test(p)) m = { color: [1, 1, 1, 1], texture: p };
    else {
      const d = await load(p);
      if (d?.scan) m = { color: [1, 1, 1, 1], texture: d.scan.map((r) => resolve(p, r)).find(Boolean) || null };
      else if (d?.resource) m = await matFrom(p, d, d.resource);
    }
    const n = m ? materials.push(m) - 1 : null;
    matIds.set(id, n);
    return n;
  };

  // Meshes → ids.
  const meshes = [];
  const meshIds = new Map();
  let verts = 0;
  const arrayMesh = async (from, doc, res) => {
    const pr = res.props || {};
    const list = doc.format >= 3 ? (Array.isArray(pr._surfaces) ? pr._surfaces : []) :
      Object.keys(pr).filter((k) => /^surfaces\/\d+$/.test(k)).sort((a, b) => a.split("/")[1] - b.split("/")[1]).map((k) => pr[k]);
    const surfaces = [];
    for (const s of list) {
      if (!s || typeof s !== "object") continue;
      const d = doc.format >= 3 ? decodeGd4(s) : decodeGd3(s);
      if (d.skip) { skip(d.skip); surfaces.push(null); continue; }
      verts += d.pos.length / 3;
      surfaces.push({ pos: b64(d.pos), nrm: d.nrm && b64(d.nrm), uv: d.uv && b64(d.uv), col: d.col && b64(d.col), idx: d.idx && b64(d.idx),
        count: d.idx ? d.idx.length : d.pos.length / 3, mat: await material(from, doc, s.material) });
    }
    return surfaces.some(Boolean) ? { kind: "array", surfaces } : null;
  };
  const meshOfRes = async (from, doc, res) => {
    if (!res) return null;
    if (res.type === "ArrayMesh" || res.props?._surfaces || Object.keys(res.props || {}).some((k) => k.startsWith("surfaces/"))) return arrayMesh(from, doc, res);
    if (PRIMS.has(res.type)) {
      const prim = primOf(res.type, res.props || {}, doc.format >= 3);
      return prim && { kind: "prim", prim, surfaces: [{ mat: await material(from, doc, res.props?.material) }] };
    }
    return null;
  };
  const mesh = async (from, doc, v) => {
    const ref = refOf(v);
    if (!ref) return null;
    const p = ref.$ === "SubResource" ? null : extPath(from, doc, ref);
    const id = ref.$ === "SubResource" ? `${from}#${ref.a[0]}` : p;
    if (!id) { skip("missing mesh file"); return null; }
    if (meshIds.has(id)) return meshIds.get(id);
    meshIds.set(id, null);
    let m = null;
    if (ref.$ === "SubResource") m = await meshOfRes(from, doc, doc.subs.get(String(ref.a[0])));
    else if (MODEL_FILE.test(p)) m = { kind: "file", path: p };
    else {
      const d = await load(p);
      if (d?.resource) m = await meshOfRes(p, d, d.resource);
      if (!m) skip(`${path.extname(p) || "unknown"} mesh resource`);
    }
    const n = m ? meshes.push(m) - 1 : null;
    meshIds.set(id, n);
    return n;
  };

  // Draws grouped by mesh + materials, so the page can draw each group instanced.
  const groups = new Map();
  let instances = 0, nodesSeen = 0;
  const draw = (meshId, slots, override, xf) => {
    if (meshId === null || meshId === undefined) return;
    if (instances >= MAX_INSTANCES) { skip("over the instance limit"); return; }
    const gk = `${meshId}|${slots.join(",")}|${override ?? ""}`;
    let g = groups.get(gk);
    if (!g) groups.set(gk, g = { mesh: meshId, slots, override, xf: [] });
    g.xf.push(...xf);
    instances++;
  };
  const slotsOf = async (from, doc, props, re) => {
    const out = [];
    for (const [k, v] of Object.entries(props)) { const m = k.match(re); if (m) out[+m[1]] = await material(from, doc, v); }
    return Array.from(out, (x) => x ?? null);
  };
  const lights = [], cameras = [], spawns = [];
  let env = null;

  // One scene file, placed under `base`; `over` = the outer scene's property overrides by node path.
  const scene = async (file, base, over, depth, stack) => {
    const doc = await load(file);
    if (!doc || doc.binary || doc.scan) { skip("binary scene"); return new Map(); }
    if (depth > MAX_DEPTH || stack.includes(file)) { skip("recursive scene"); return new Map(); }
    const glob = new Map(), hidden = new Set();
    const keyOf = (n) => n.parent === undefined ? "." : n.parent === "." ? String(n.name) : `${n.parent}/${n.name}`;
    for (const node of doc.nodes) {
      nodesSeen++;
      const k = keyOf(node);
      const pk = node.parent === undefined ? null : String(node.parent);
      if (pk !== null && hidden.has(pk)) { hidden.add(k); continue; }
      const inst = refOf(node.instance);
      // An untyped, uninstanced node below the root overrides a node of an instanced sub-scene.
      if (pk !== null && !node.type && !inst) continue;
      const props = { ...node.props, ...(over.get(k) || {}) };
      if (props.visible === false) { hidden.add(k); continue; }
      const parentXf = pk === null ? base : glob.get(pk);
      if (!parentXf) { skip("node under a part that wasn't loaded"); continue; }
      const local = xfOf(props.transform) || XF_I;
      if (inst) {
        const p = extPath(file, doc, inst);
        if (!p) { skip("missing instanced scene"); continue; }
        if (/\.(tscn|escn)$/i.test(p)) {
          // Overrides for the sub-scene: this node's own props (its root) + untyped nodes below it.
          const sub = new Map([[".", { ...props, ...(xfOf(props.transform) ? {} : {}) }]]);
          for (const n of doc.nodes) {
            if (n.type || refOf(n.instance) || n.parent === undefined) continue;
            const par = String(n.parent);
            const rel = par === k ? String(n.name) : par.startsWith(`${k}/`) ? `${par.slice(k.length + 1)}/${n.name}` : null;
            if (rel) sub.set(rel, { ...(sub.get(rel) || {}), ...n.props });
          }
          if (!props.transform) delete sub.get(".").transform;
          const g = await scene(p, parentXf, sub, depth + 1, [...stack, file]);
          for (const [sk, xf] of g) glob.set(sk === "." ? k : `${k}/${sk}`, xf);
          if (!g.size) glob.set(k, mul(parentXf, local));
        } else {
          const g = mul(parentXf, local);
          glob.set(k, g);
          if (MODEL_FILE.test(p)) draw(await mesh(file, doc, { $: "ExtPath", a: [p.startsWith("res://") ? p : `res://${p.slice(ix.resolveRes(file, "res://").length)}`] }), [], await material(file, doc, props.material_override), g);
          else skip(`${path.extname(p) || "unknown"} model`);
        }
        continue;
      }
      const g = mul(parentXf, local);
      glob.set(k, g);
      const type = String(node.type || "");
      if (/^MeshInstance(3D)?$/.test(type) || type === "SoftBody" || type === "SoftBody3D") {
        draw(await mesh(file, doc, props.mesh), await slotsOf(file, doc, props, /^(?:material|surface_material_override)\/(\d+)$/), await material(file, doc, props.material_override), g);
      } else if (/^MultiMeshInstance(3D)?$/.test(type)) {
        const ref = refOf(props.multimesh);
        let mm = null, mdoc = doc, mfrom = file;
        if (ref?.$ === "SubResource") mm = doc.subs.get(String(ref.a[0]));
        else if (ref) { const p = extPath(file, doc, ref); mdoc = p && await load(p); mfrom = p; mm = mdoc?.resource; }
        const mp = mm?.props || {};
        const meshId = mm && await mesh(mfrom, mdoc, mp.mesh);
        const over = await material(file, doc, props.material_override);
        const count = num(mp.instance_count, 0), vis = num(mp.visible_instance_count, -1), n = vis >= 0 ? Math.min(vis, count) : count;
        if (num(mp.transform_format, 1) !== 1 && mdoc?.format < 3) { skip("2D MultiMesh"); continue; }
        if (Array.isArray(mp.transform_array?.a)) {
          const a = mp.transform_array.a;
          for (let i = 0; i < n && i * 12 + 11 < a.length; i++) {
            const c = a.slice(i * 12, i * 12 + 12);
            draw(meshId, [], over, mul(g, [c[0], c[3], c[6], c[1], c[4], c[7], c[2], c[5], c[8], c[9], c[10], c[11]]));
          }
        } else if (Array.isArray(mp.buffer?.a)) {
          const a = mp.buffer.a, stride = 12 + (mp.use_colors ? 4 : 0) + (mp.use_custom_data ? 4 : 0);
          for (let i = 0; i < n && i * stride + 11 < a.length; i++) {
            const c = a.slice(i * stride, i * stride + 12);
            draw(meshId, [], over, mul(g, [c[0], c[1], c[2], c[4], c[5], c[6], c[8], c[9], c[10], c[3], c[7], c[11]]));
          }
        } else if (count) skip("MultiMesh without transforms");
      } else if (/^CSG/.test(type)) {
        if (num(props.operation, 0) === 2) { skip("CSG subtraction"); continue; }
        const g4 = doc.format >= 3;
        let prim = null;
        if (/^CSGBox/.test(type)) prim = { type: "box", size: g4 ? (vec(props.size, 3) || [1, 1, 1]) : [num(props.width, 2), num(props.height, 2), num(props.depth, 2)] };
        else if (/^CSGCylinder/.test(type)) prim = { type: "cylinder", top: num(props.radius, g4 ? 0.5 : 1), bottom: props.cone === true ? 0 : num(props.radius, g4 ? 0.5 : 1), height: num(props.height, g4 ? 2 : 1) };
        else if (/^CSGSphere/.test(type)) prim = { type: "sphere", radius: num(props.radius, g4 ? 0.5 : 1), height: 2 * num(props.radius, g4 ? 0.5 : 1) };
        else if (/^CSGMesh/.test(type)) { draw(await mesh(file, doc, props.mesh), [], await material(file, doc, props.material), g); continue; }
        else if (/^CSG(Combiner|Polygon|Torus)/.test(type)) { if (!/Combiner/.test(type)) skip(`${type}`); continue; }
        if (prim) {
          const id = meshes.push({ kind: "prim", prim, surfaces: [{ mat: null }] }) - 1;
          draw(id, [], await material(file, doc, props.material), g);
        }
      } else if (/^DirectionalLight(3D)?$/.test(type)) {
        lights.push({ dir: [-g[2], -g[5], -g[8]], color: color(props.light_color) || [1, 1, 1, 1], energy: num(props.light_energy, 1) });
      } else if (/^WorldEnvironment$/.test(type) && !env) {
        const ref = refOf(props.environment);
        let res = null;
        if (ref?.$ === "SubResource") res = doc.subs.get(String(ref.a[0]));
        else if (ref) { const p = extPath(file, doc, ref); res = p && (await load(p))?.resource; }
        const pr = res?.props || {};
        env = { background: color(pr.background_color) || color(pr.fog_color), ambient: color(pr.ambient_light_color), fog: pr.fog_enabled === true ? color(pr.fog_color) : null };
      } else if (/^(Camera|Camera3D|InterpolatedCamera|ClippedCamera)$/.test(type)) {
        cameras.push({ xf: g, current: props.current === true, depth });
      } else if (/^(Position3D|Marker3D|Spatial|Node3D)$/.test(type) && /^(player_?)?spawn(_?point)?\d*$|player.*(spawn|start)|^start(_?pos(ition)?)?$/i.test(String(node.name))) {
        spawns.push({ xf: g, name: String(node.name), depth });
      } else if (type === "GridMap") skip("GridMap");
      else if (/^(Sprite3D|AnimatedSprite3D|Label3D)$/.test(type)) skip(type);
      else if (/^(C?P|GPU)articles(3D)?$/.test(type) || /^CPUParticles(3D)?$/.test(type) || /^GPUParticles3D$/.test(type)) skip("particles");
    }
    return glob;
  };

  await scene(scenePath, XF_I, new Map(), 0, []);

  // Looks REA already knows for model files (saved / scene / .mtl / same-name guess).
  const looks = {};
  for (const m of meshes) {
    if (m.kind !== "file" || looks[m.path]) continue;
    try {
      const found = await textures.candidates(key, root, getTree, m.path);
      const c = found.candidates || [];
      looks[m.path] = { look: c.find((x) => x.source === "saved") || c.find((x) => x.source === "scene") || c.find((x) => x.source === "mtl") || null, guess: c.find((x) => x.source === "name") || null };
    } catch { looks[m.path] = { look: null, guess: null }; }
  }
  const pickCam = cameras.find((c) => c.current && c.depth === 0) || cameras.find((c) => c.depth === 0);
  const pickSpawn = spawns.sort((a, b) => a.depth - b.depth)[0];
  const result = {
    scene: scenePath, format: (await load(scenePath))?.format || 2,
    meshes, materials, looks,
    draws: [...groups.values()].map((g) => ({ ...g, xf: b64(Float32Array.from(g.xf)), count: g.xf.length / 12 })),
    lights, env, spawn: pickCam ? { kind: "camera", xf: pickCam.xf } : pickSpawn ? { kind: "marker", name: pickSpawn.name, xf: pickSpawn.xf } : null,
    stats: { nodes: nodesSeen, instances, meshes: meshes.length, materials: materials.length, vertices: verts, files: docs.size },
    skipped,
  };
  cache.set(ck, { at: Date.now(), mtime: stat.mtimeMs, result });
  for (const [k, v] of cache) if (Date.now() - v.at > CACHE_MS) cache.delete(k);
  return result;
}
