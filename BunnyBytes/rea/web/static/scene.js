// "Explore in 3D" for Godot scenes (.tscn/.escn) in the Decompiled tab: scene.mjs flattens the scene
// (nodes, instanced sub-scenes, meshes, materials) and this draws it as a world you can fly through.
// Every mesh is uploaded once and drawn instanced. Mouse: click to look around (Esc frees the mouse),
// WASD/arrows move, Space/E up, Q/C down, Shift faster, wheel = speed, F = full screen, R = back to start.
// Touch: left half of the view moves, right half looks, ▲/▼ buttons for up and down.
// Uses app.js (el, api, dx, dxRaw, dxResolver, size) and models.js (dxParseModel).

function sceneExplore(f, body) {
  const box = el("div", "sw-box");
  const bar = el("div", "sw-bar");
  const go = el("button", "button primary", "Explore in 3D");
  go.type = "button";
  bar.append(go, el("span", "hint", "Rebuilds the scene's 3D nodes (meshes, models, materials, sub-scenes) as a world you can walk through. 2D and UI scenes have nothing to show."));
  box.append(bar);
  body.append(box);
  go.addEventListener("click", () => sceneOpen(f, box, go));
}

const SW_B64 = (s, T) => {
  if (!s) return null;
  const bin = atob(s), u8 = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  return new T(u8.buffer);
};

async function sceneOpen(f, box, go) {
  dxRelease();
  go.disabled = true;
  box.querySelector(".sw-wrap")?.remove();
  box.querySelector(".sw-status")?.remove();
  const status = el("p", "hint sw-status", "Reading the scene…");
  box.append(status);
  let data;
  try { data = await api(`/kb/${dx.key}/decompiled/scene?path=${encodeURIComponent(f.p)}`); }
  catch (error) { status.textContent = `Couldn't build the scene: ${error.message}`; status.className = "error sw-status"; go.disabled = false; return; }
  const skippedText = Object.entries(data.skipped || {}).sort((a, b) => b[1] - a[1]).map(([k, n]) => `${n} ${k}`).join(", ");
  if (!data.draws.length) {
    status.textContent = `Nothing 3D to draw in this scene (${data.stats.nodes.toLocaleString()} nodes${skippedText ? `; not drawn: ${skippedText}` : ""}). It's probably a 2D or UI scene.`;
    go.disabled = false;
    return;
  }

  // Model files the scene uses (glTF/OBJ/FBX/…): parsed here, like the model viewer does.
  const files = data.meshes.map((m, i) => m.kind === "file" ? i : -1).filter((i) => i >= 0);
  const parsed = new Map();
  const failed = [];
  const treeFile = (p) => {
    for (const bin of dx.data?.binaries || []) { const x = bin.files.find((y) => y.p === p); if (x) return { name: x.p.split("/").pop(), ...x, bin: x.bin || bin }; }
    return null;
  };
  let done = 0;
  const queue = [...files];
  const worker = async () => {
    while (queue.length) {
      const i = queue.shift();
      const p = data.meshes[i].path;
      status.textContent = `Loading models ${++done} of ${files.length}: ${p.split("/").pop()}`;
      try {
        const fo = treeFile(p);
        if (!fo) throw new Error("not in the tree");
        if (fo.s > 96 * 1024 * 1024) throw new Error("too big");
        const r = await fetch(dxRaw(p));
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const resolver = dxResolver(fo);
        const mesh = await dxParseModel(dxExt(p), await r.arrayBuffer(), resolver);
        for (const s of mesh.surfaces) {
          if (s.texBlob) { s.texUrl = URL.createObjectURL(s.texBlob); dx.blobs.push(s.texUrl); delete s.texBlob; }
          if (!s.texUrl && !s.texCanvas && s.texRef) { const tp = resolver.image(s.texRef); if (tp) s.texUrl = dxRaw(tp); }
        }
        parsed.set(i, mesh);
      } catch (error) { failed.push(`${p.split("/").pop()} (${error.message})`); }
    }
  };
  await Promise.all([worker(), worker(), worker(), worker()]);

  const wrap = el("div", "sw-wrap");
  wrap.tabIndex = -1;
  const canvas = el("canvas", "sw-canvas");
  canvas.tabIndex = 0;
  const top = el("div", "sw-top");
  const fsBtn = el("button", "button sw-fs-btn", "⛶ Full screen");
  fsBtn.type = "button";
  fsBtn.title = "Full screen (F)";
  const texBtn = el("button", "button", "Textures: on");
  texBtn.type = "button";
  const resetBtn = el("button", "button", "Back to start");
  resetBtn.type = "button";
  resetBtn.title = "R";
  top.append(texBtn, resetBtn, fsBtn);
  const exitBtn = el("button", "button sw-exit", "✕ Exit full screen");
  exitBtn.type = "button";
  exitBtn.title = "Esc";
  const help = el("div", "sw-help");
  const speedNote = el("span", "sw-speed");
  help.append(el("span", "sw-help-desk", "Click to look · WASD move · Space/E up · Q/C down · Shift fast · wheel speed · Esc frees mouse"),
    el("span", "sw-help-touch", "Left side: move · right side: look"), speedNote);
  const touch = el("div", "sw-touch");
  const upBtn = el("button", "button", "▲"), downBtn = el("button", "button", "▼");
  upBtn.type = downBtn.type = "button";
  upBtn.setAttribute("aria-label", "Up"); downBtn.setAttribute("aria-label", "Down");
  touch.append(upBtn, downBtn);
  const stick = el("div", "sw-stick");
  stick.hidden = true;
  wrap.append(canvas, top, exitBtn, help, touch, stick);
  status.textContent = `${data.stats.nodes.toLocaleString()} nodes · ${data.stats.instances.toLocaleString()} objects drawn · ${data.meshes.length} meshes · ${data.materials.length} materials` +
    `${data.spawn ? ` · starts at ${data.spawn.kind === "camera" ? "the scene camera" : `"${data.spawn.name}"`}` : ""}` +
    `${skippedText ? ` · not drawn: ${skippedText}` : ""}${failed.length ? ` · models that failed: ${failed.slice(0, 5).join(", ")}${failed.length > 5 ? "…" : ""}` : ""}`;
  box.append(wrap);
  go.disabled = false;
  go.textContent = "Rebuild 3D view";

  const world = sceneWorld(canvas, data, parsed, { wrap, fsBtn, exitBtn, texBtn, resetBtn, upBtn, downBtn, stick, speedNote });
  if (!world) { wrap.remove(); status.textContent = "This browser has no WebGL2, so the scene can't be drawn."; return; }
  dx.viewer = world;
  canvas.focus({ preventScroll: true });
}

// ------------------------------------------------------------------------- geometry --
function swPrim(prim) {
  const P = [], N = [], T = [], I = [];
  const quad = (o, u, v, n) => { // corner o, edges u and v
    const b = P.length / 3;
    for (const [a, c] of [[0, 0], [1, 0], [1, 1], [0, 1]]) { P.push(o[0] + u[0] * a + v[0] * c, o[1] + u[1] * a + v[1] * c, o[2] + u[2] * a + v[2] * c); N.push(...n); T.push(a, 1 - c); }
    I.push(b, b + 1, b + 2, b, b + 2, b + 3);
  };
  const lathe = (rings) => { // rings: [[y, r, v]] around Y, 24 segments
    const seg = 24, b = P.length / 3;
    for (const [y, r, v, ny] of rings) for (let s = 0; s <= seg; s++) {
      const a = (s / seg) * Math.PI * 2, x = Math.sin(a), z = Math.cos(a);
      P.push(x * r, y, z * r);
      const l = Math.hypot(x, ny ?? 0, z) || 1;
      N.push(x / l, (ny ?? 0) / l, z / l);
      T.push(s / seg, v);
    }
    for (let k = 0; k + 1 < rings.length; k++) for (let s = 0; s < seg; s++) {
      const a = b + k * (seg + 1) + s, c = a + seg + 1;
      I.push(a, c, a + 1, a + 1, c, c + 1);
    }
  };
  const cap = (y, r, up) => {
    if (r <= 0) return;
    const seg = 24, b = P.length / 3;
    P.push(0, y, 0); N.push(0, up ? 1 : -1, 0); T.push(0.5, 0.5);
    for (let s = 0; s <= seg; s++) { const a = (s / seg) * Math.PI * 2; P.push(Math.sin(a) * r, y, Math.cos(a) * r); N.push(0, up ? 1 : -1, 0); T.push(0.5 + Math.sin(a) / 2, 0.5 + Math.cos(a) / 2); }
    for (let s = 0; s < seg; s++) I.push(b, b + 1 + s, b + 2 + s);
  };
  if (prim.type === "box") {
    const [x, y, z] = prim.size.map((v) => v / 2);
    quad([-x, -y, z], [2 * x, 0, 0], [0, 2 * y, 0], [0, 0, 1]); quad([x, -y, -z], [-2 * x, 0, 0], [0, 2 * y, 0], [0, 0, -1]);
    quad([x, -y, z], [0, 0, -2 * z], [0, 2 * y, 0], [1, 0, 0]); quad([-x, -y, -z], [0, 0, 2 * z], [0, 2 * y, 0], [-1, 0, 0]);
    quad([-x, y, z], [2 * x, 0, 0], [0, 0, -2 * z], [0, 1, 0]); quad([-x, -y, -z], [2 * x, 0, 0], [0, 0, 2 * z], [0, -1, 0]);
  } else if (prim.type === "plane") {
    const [x, z] = prim.size.map((v) => v / 2), o = prim.offset || [0, 0, 0];
    quad([-x + o[0], o[1], -z + o[2]], [2 * x, 0, 0], [0, 0, 2 * z], [0, 1, 0]);
  } else if (prim.type === "quad") {
    const [x, y] = prim.size.map((v) => v / 2), o = prim.offset || [0, 0, 0];
    quad([-x + o[0], -y + o[1], o[2]], [2 * x, 0, 0], [0, 2 * y, 0], [0, 0, 1]);
  } else if (prim.type === "cylinder") {
    const h = prim.height / 2;
    lathe([[-h, prim.bottom, 1, 0], [h, prim.top, 0, 0]]);
    cap(h, prim.top, true); cap(-h, prim.bottom, false);
  } else if (prim.type === "sphere") {
    const rings = [], k = prim.height / 2 / (prim.radius || 1);
    for (let i = 0; i <= 16; i++) { const a = Math.PI * (i / 16 - 0.5); rings.push([Math.sin(a) * prim.radius * k, Math.cos(a) * prim.radius, 1 - i / 16, Math.sin(a)]); }
    lathe(rings);
  } else if (prim.type === "capsule") {
    const rings = [], r = prim.radius, h = prim.mid / 2;
    for (let i = 0; i <= 8; i++) { const a = Math.PI / 2 * (i / 8 - 1); rings.push([Math.sin(a) * r - h, Math.cos(a) * r, 1 - i / 18, Math.sin(a)]); }
    for (let i = 0; i <= 8; i++) { const a = Math.PI / 2 * (i / 8); rings.push([Math.sin(a) * r + h, Math.cos(a) * r, 0.5 - i / 18, Math.sin(a)]); }
    lathe(rings);
    if (prim.axis === "z") for (let i = 0; i < P.length; i += 3) { const y = P[i + 1]; P[i + 1] = -P[i + 2]; P[i + 2] = y; const ny = N[i + 1]; N[i + 1] = -N[i + 2]; N[i + 2] = ny; }
  }
  return { pos: Float32Array.from(P), nrm: Float32Array.from(N), uv: Float32Array.from(T), col: null, idx: Uint32Array.from(I) };
}

function swNormals(pos, idx) {
  const n = new Float32Array(pos.length);
  const count = idx ? idx.length : pos.length / 3;
  for (let k = 0; k + 2 < count; k += 3) {
    const a = idx ? idx[k] : k, b = idx ? idx[k + 1] : k + 1, c = idx ? idx[k + 2] : k + 2;
    const ux = pos[b * 3] - pos[a * 3], uy = pos[b * 3 + 1] - pos[a * 3 + 1], uz = pos[b * 3 + 2] - pos[a * 3 + 2];
    const vx = pos[c * 3] - pos[a * 3], vy = pos[c * 3 + 1] - pos[a * 3 + 1], vz = pos[c * 3 + 2] - pos[a * 3 + 2];
    const x = uy * vz - uz * vy, y = uz * vx - ux * vz, z = ux * vy - uy * vx;
    for (const v of [a, b, c]) { n[v * 3] += x; n[v * 3 + 1] += y; n[v * 3 + 2] += z; }
  }
  for (let i = 0; i < n.length; i += 3) { const l = Math.hypot(n[i], n[i + 1], n[i + 2]) || 1; n[i] /= l; n[i + 1] /= l; n[i + 2] /= l; }
  return n;
}

// --------------------------------------------------------------------------- the world --
function sceneWorld(canvas, data, parsed, ui) {
  const gl = canvas.getContext("webgl2", { antialias: true });
  if (!gl) return null;
  const sh = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
  const prog = gl.createProgram();
  gl.attachShader(prog, sh(gl.VERTEX_SHADER, `#version 300 es
in vec3 p; in vec2 t; in vec3 n; in vec4 c; in vec4 r0; in vec4 r1; in vec4 r2;
uniform mat4 VP; uniform vec3 eye; uniform vec3 uvS; uniform vec3 uvO; uniform int triW;
out vec2 vt; out vec3 vn; out vec4 vc; out float vd; out vec3 tp; out vec3 tn;
void main() {
  vec4 q = vec4(p, 1.0);
  vec3 w = vec3(dot(r0, q), dot(r1, q), dot(r2, q));
  vn = vec3(dot(r0.xyz, n), dot(r1.xyz, n), dot(r2.xyz, n));
  vt = t * uvS.xy + uvO.xy; vc = c; vd = distance(w, eye);
  // Triplanar (Godot SpatialMaterial uv1_triplanar): object space unless uv1_world_triplanar.
  tp = (triW == 1 ? w : p) * uvS + uvO; tp.y = -tp.y;
  tn = triW == 1 ? vn : n;
  gl_Position = VP * vec4(w, 1.0);
}`));
  gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, `#version 300 es
precision highp float;
in vec2 vt; in vec3 vn; in vec4 vc; in float vd; in vec3 tp; in vec3 tn;
uniform sampler2D tex; uniform vec4 col; uniform int tri; uniform int useTex; uniform int useVC; uniform int lit; uniform int cut; uniform int blend;
uniform vec3 L; uniform float amb; uniform vec3 fogC; uniform vec2 fog;
out vec4 o;
void main() {
  vec4 cc = col;
  if (useVC == 1) cc *= vc;
  if (useTex == 1) {
    if (tri == 1) {
      vec3 b = abs(normalize(tn)); b /= max(1e-4, b.x + b.y + b.z);
      cc *= texture(tex, tp.xy) * b.z + texture(tex, tp.xz) * b.y + texture(tex, tp.zy * vec2(-1.0, 1.0)) * b.x;
    } else cc *= texture(tex, vt);
  }
  if (cut == 1 && cc.a < 0.45) discard;
  if (blend == 1 && cc.a < 0.02) discard;
  vec3 rgb = cc.rgb;
  if (lit == 1) rgb *= amb + (1.0 - amb) * abs(dot(normalize(vn), L));
  rgb = mix(rgb, fogC, clamp((vd - fog.x) / max(1e-3, fog.y - fog.x), 0.0, 1.0));
  o = vec4(rgb, blend == 1 ? cc.a : 1.0);
}`));
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return null;
  const U = Object.fromEntries(["VP", "eye", "uvS", "uvO", "triW", "tri", "tex", "col", "useTex", "useVC", "lit", "cut", "blend", "L", "amb", "fogC", "fog"].map((k) => [k, gl.getUniformLocation(prog, k)]));
  const A = Object.fromEntries(["p", "t", "n", "c", "r0", "r1", "r2"].map((k) => [k, gl.getAttribLocation(prog, k)]));
  const buffers = [], vaos = [];
  const buf = (target, d) => { const b = gl.createBuffer(); gl.bindBuffer(target, b); gl.bufferData(target, d, gl.STATIC_DRAW); buffers.push(b); return b; };

  // Geometry per mesh surface: {pos, nrm, uv, col, idx, mat (server material id), file look}.
  const geoms = data.meshes.map((m, mi) => {
    let surfaces = [];
    if (m.kind === "array") surfaces = m.surfaces.map((s) => s && { pos: SW_B64(s.pos, Float32Array), nrm: SW_B64(s.nrm, Float32Array), uv: SW_B64(s.uv, Float32Array), col: SW_B64(s.col, Float32Array), idx: SW_B64(s.idx, Uint32Array), mat: s.mat });
    else if (m.kind === "prim") surfaces = [{ ...swPrim(m.prim), mat: m.surfaces?.[0]?.mat ?? null }];
    else if (m.kind === "file" && parsed.has(mi)) surfaces = parsed.get(mi).surfaces.map((s, i) => ({ ...s, idx: null, mat: null, file: m.path, fileIndex: s.index ?? i }));
    return surfaces.map((s) => {
      if (!s || !s.pos?.length) return null;
      const nrm = s.nrm && s.nrm.length === s.pos.length ? s.nrm : swNormals(s.pos, s.idx);
      const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
      for (let i = 0; i < s.pos.length; i += 3) for (let a = 0; a < 3; a++) { const v = s.pos[i + a]; if (v < min[a]) min[a] = v; if (v > max[a]) max[a] = v; }
      return { ...s, nrm, min, max, gpu: null };
    });
  });
  const upload = (g) => {
    if (g.gpu) return g.gpu;
    g.gpu = {
      p: buf(gl.ARRAY_BUFFER, g.pos), n: buf(gl.ARRAY_BUFFER, g.nrm),
      t: g.uv && g.uv.length / 2 === g.pos.length / 3 ? buf(gl.ARRAY_BUFFER, g.uv) : null,
      c: g.col && g.col.length / 4 === g.pos.length / 3 ? buf(gl.ARRAY_BUFFER, g.col) : null,
      i: g.idx ? buf(gl.ELEMENT_ARRAY_BUFFER, g.idx) : null,
      count: g.idx ? g.idx.length : g.pos.length / 3,
    };
    return g.gpu;
  };

  // Textures: URLs (decompiled files, blobs) or canvases (decoded TGA/DDS/… skins).
  let dead = false;
  const textures = new Map();
  const texture = (src) => {
    if (!src) return null;
    if (textures.has(src)) return textures.get(src);
    const entry = { tex: gl.createTexture(), ready: false };
    textures.set(src, entry);
    const put = (img, w) => {
      if (dead) return;
      gl.bindTexture(gl.TEXTURE_2D, entry.tex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
      gl.generateMipmap(gl.TEXTURE_2D);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, w <= 256 ? gl.NEAREST : gl.LINEAR);
      entry.ready = true;
      dirty();
    };
    if (typeof src !== "string") put(src, src.width);
    else { const img = new Image(); img.onload = () => put(img, img.naturalWidth); img.src = src; }
    return entry;
  };

  // Material for one surface of one draw group.
  const GREY = [0.8, 0.8, 0.8, 1];
  const fromServer = (id) => {
    const m = data.materials[id];
    return m && { color: m.color || [1, 1, 1, 1], tex: m.texture ? dxRaw(m.texture) : null, vc: Boolean(m.vc), blend: Boolean(m.blend), cut: Boolean(m.scissor || (m.blend && m.texture)), lit: !m.unshaded,
      uvS: m.uvScale || [1, 1, 1], uvO: m.uvOffset || [0, 0, 0], tri: Boolean(m.triplanar), triW: Boolean(m.triWorld) };
  };
  const materialFor = (group, g, i) => {
    const id = group.override ?? group.slots?.[i] ?? g.mat;
    const m = id !== null && id !== undefined ? fromServer(id) : null;
    if (m) return { ...m, vc: m.vc && Boolean(g.col) };
    if (g.file) {
      const known = data.looks?.[g.file] || {};
      for (const c of [known.look]) {
        if (!c) continue;
        const hit = c.surfaces?.find((x) => x.index === g.fileIndex) || c.surfaces?.find((x) => x.name && x.name === g.name);
        if (hit && (hit.texture || hit.color)) return { color: hit.color || [1, 1, 1, 1], tex: hit.texture ? dxRaw(hit.texture) : null, lit: true, cut: true };
        if (c.texture || c.color) return { color: c.color || [1, 1, 1, 1], tex: c.texture ? dxRaw(c.texture) : null, lit: true, cut: true };
      }
      if (g.texUrl || g.texCanvas || g.color || g.col) return { color: g.color || [1, 1, 1, 1], tex: g.texUrl || g.texCanvas || null, vc: Boolean(g.col), lit: true, cut: true };
      if (known.guess?.texture) return { color: [1, 1, 1, 1], tex: dxRaw(known.guess.texture), lit: true, cut: true };
    }
    return { color: GREY, tex: null, vc: false, lit: true };
  };

  // Draw parts: one VAO per (group, surface) sharing the surface's geometry and the group's instances.
  const centers = [];
  const bmin = [Infinity, Infinity, Infinity], bmax = [-Infinity, -Infinity, -Infinity];
  const parts = [];
  gl.useProgram(prog);
  for (const group of data.draws) {
    const xf = SW_B64(group.xf, Float32Array);
    const n = group.count;
    const rows = new Float32Array(n * 12);
    for (let k = 0; k < n; k++) {
      const s = xf.subarray(k * 12, k * 12 + 12), o = k * 12;
      rows.set([s[0], s[1], s[2], s[9], s[3], s[4], s[5], s[10], s[6], s[7], s[8], s[11]], o);
    }
    const surfaces = (geoms[group.mesh] || []).map((g, i) => [g, i]).filter(([g]) => g);
    if (!surfaces.length) continue;
    const inst = buf(gl.ARRAY_BUFFER, rows);
    // Bounds: each instance's box centre (robust overview) and corners (near/far planes).
    for (const [g] of surfaces) for (let k = 0; k < n; k++) {
      const r = rows.subarray(k * 12, k * 12 + 12);
      const c = [(g.min[0] + g.max[0]) / 2, (g.min[1] + g.max[1]) / 2, (g.min[2] + g.max[2]) / 2];
      const w = [0, 1, 2].map((a) => r[a * 4] * c[0] + r[a * 4 + 1] * c[1] + r[a * 4 + 2] * c[2] + r[a * 4 + 3]);
      if (w.every(Number.isFinite)) { if (centers.length < 200000) centers.push(w); for (let a = 0; a < 3; a++) { bmin[a] = Math.min(bmin[a], w[a]); bmax[a] = Math.max(bmax[a], w[a]); } }
    }
    for (const [g, i] of surfaces) {
      const gpu = upload(g);
      const vao = gl.createVertexArray();
      vaos.push(vao);
      gl.bindVertexArray(vao);
      const attr = (loc, b, size) => { if (loc < 0) return; if (!b) { gl.disableVertexAttribArray(loc); return; } gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, size, gl.FLOAT, false, 0, 0); gl.vertexAttribDivisor(loc, 0); };
      attr(A.p, gpu.p, 3); attr(A.n, gpu.n, 3); attr(A.t, gpu.t, 2); attr(A.c, gpu.c, 4);
      gl.bindBuffer(gl.ARRAY_BUFFER, inst);
      ["r0", "r1", "r2"].forEach((k, j) => { if (A[k] < 0) return; gl.enableVertexAttribArray(A[k]); gl.vertexAttribPointer(A[k], 4, gl.FLOAT, false, 48, j * 16); gl.vertexAttribDivisor(A[k], 1); });
      if (gpu.i) gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, gpu.i);
      gl.bindVertexArray(null);
      const mat = materialFor(group, g, i);
      parts.push({ vao, gpu, n, mat: { ...mat, vc: Boolean(mat.vc && gpu.c) } });
    }
  }
  parts.sort((a, b) => Boolean(a.mat.blend) - Boolean(b.mat.blend));
  // Defaults for attributes a surface doesn't have.
  if (A.t >= 0) gl.vertexAttrib2f(A.t, 0, 0);
  if (A.c >= 0) gl.vertexAttrib4f(A.c, 1, 1, 1, 1);

  // Size of the place: 5th–95th percentile of object centres (ignores far-away sky domes).
  const pct = (a, q) => { const s = centers.map((c) => c[a]).sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(q * s.length))] ?? 0; };
  const lo = [0, 1, 2].map((a) => pct(a, 0.05)), hi = [0, 1, 2].map((a) => pct(a, 0.95));
  const mid = lo.map((v, a) => (v + hi[a]) / 2);
  const extent = Math.max(2, ...hi.map((v, a) => v - lo[a]));
  const full = Math.max(extent, ...bmax.map((v, a) => Number.isFinite(v) ? v - bmin[a] : 0));
  const near = Math.min(0.5, Math.max(0.03, extent / 20000)), far = Math.max(200, full * 2.5);
  const baseSpeed = Math.max(2, Math.min(300, extent / 25));

  const light = data.lights?.[0]?.dir || [-0.35, -0.85, -0.4];
  const ll = Math.hypot(...light) || 1;
  const L = light.map((v) => -v / ll);
  const sky = (data.env?.background || [0.56, 0.73, 0.87, 1]).slice(0, 3);
  const fogC = (data.env?.fog || sky).slice(0, 3);

  // Camera.
  const cam = { pos: [0, 0, 0], yaw: 0, pitch: 0 };
  const start = () => {
    const s = data.spawn;
    if (s?.xf) {
      const x = s.xf;
      cam.pos = [x[9], x[10] + (s.kind === "marker" ? 1.7 : 0), x[11]];
      const f = [-x[2], -x[5], -x[8]], l = Math.hypot(...f) || 1;
      cam.pitch = s.kind === "marker" ? 0 : Math.asin(Math.max(-1, Math.min(1, f[1] / l)));
      cam.yaw = Math.atan2(-f[0], -f[2]);
    } else {
      cam.pos = [mid[0], mid[1] + extent * 0.28, mid[2] + extent * 0.55];
      cam.yaw = 0;
      cam.pitch = -Math.atan2(extent * 0.28, extent * 0.55);
    }
  };
  start();
  let speedMul = 1, textured = true;
  const showSpeed = () => { ui.speedNote.textContent = `speed ${(baseSpeed * speedMul).toFixed(baseSpeed * speedMul < 10 ? 1 : 0)} m/s`; };
  showSpeed();

  const mat4 = (a, b) => { const o = new Float32Array(16); for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) { let s = 0; for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k]; o[c * 4 + r] = s; } return o; };
  const fwd = () => [-Math.sin(cam.yaw) * Math.cos(cam.pitch), Math.sin(cam.pitch), -Math.cos(cam.yaw) * Math.cos(cam.pitch)];
  const draw = () => {
    const w = canvas.clientWidth, h = canvas.clientHeight, dpr = Math.min(2, devicePixelRatio || 1);
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.clearColor(sky[0], sky[1], sky[2], 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);
    gl.disable(gl.CULL_FACE);
    gl.useProgram(prog);
    const f = fwd(), r = [Math.cos(cam.yaw), 0, -Math.sin(cam.yaw)];
    const u = [r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0]];
    const e = cam.pos, dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
    const V = [r[0], u[0], -f[0], 0, r[1], u[1], -f[1], 0, r[2], u[2], -f[2], 0, -dot(r, e), -dot(u, e), dot(f, e), 1];
    const t = 1 / Math.tan((70 * Math.PI / 180) / 2), aspect = w / Math.max(1, h);
    const P = [t / aspect, 0, 0, 0, 0, t, 0, 0, 0, 0, (far + near) / (near - far), -1, 0, 0, 2 * far * near / (near - far), 0];
    gl.uniformMatrix4fv(U.VP, false, mat4(P, V));
    gl.uniform3fv(U.eye, e);
    gl.uniform3fv(U.L, L);
    gl.uniform1f(U.amb, 0.45);
    gl.uniform3fv(U.fogC, fogC);
    gl.uniform2f(U.fog, far * 0.35, far * 0.95);
    gl.uniform1i(U.tex, 0);
    gl.activeTexture(gl.TEXTURE0);
    let blending = false;
    for (const part of parts) {
      const m = part.mat;
      if (Boolean(m.blend) !== blending) {
        blending = Boolean(m.blend);
        if (blending) { gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA); } else gl.disable(gl.BLEND);
      }
      const tx = textured && texture(m.tex);
      gl.uniform1i(U.useTex, tx?.ready ? 1 : 0);
      if (tx?.ready) gl.bindTexture(gl.TEXTURE_2D, tx.tex);
      gl.uniform1i(U.useVC, m.vc ? 1 : 0);
      gl.uniform1i(U.lit, m.lit === false ? 0 : 1);
      gl.uniform1i(U.cut, tx?.ready && m.cut ? 1 : 0);
      gl.uniform1i(U.blend, blending ? 1 : 0);
      gl.uniform3fv(U.uvS, m.uvS || [1, 1, 1]);
      gl.uniform3fv(U.uvO, m.uvO || [0, 0, 0]);
      gl.uniform1i(U.tri, m.tri ? 1 : 0);
      gl.uniform1i(U.triW, m.triW ? 1 : 0);
      const c = textured || !m.tex ? m.color : [0.8, 0.8, 0.8, m.color[3] ?? 1];
      gl.uniform4f(U.col, c[0], c[1], c[2], c[3] ?? 1);
      gl.bindVertexArray(part.vao);
      if (part.gpu.i) gl.drawElementsInstanced(gl.TRIANGLES, part.gpu.count, gl.UNSIGNED_INT, 0, part.n);
      else gl.drawArraysInstanced(gl.TRIANGLES, 0, part.gpu.count, part.n);
    }
    gl.disable(gl.BLEND);
    gl.bindVertexArray(null);
  };

  // Input: keys, mouse look (pointer lock or drag), touch stick + look, buttons.
  const keys = new Set();
  const move = { x: 0, z: 0, y: 0 };
  let raf = 0, last = 0, needs = true;
  const moving = () => keys.size || move.x || move.z || move.y;
  const loop = (now) => {
    raf = 0;
    if (dead) return;
    const dt = last ? Math.min(0.1, (now - last) / 1000) : 0;
    last = now;
    if (moving()) {
      const sp = baseSpeed * speedMul * (keys.has("ShiftLeft") || keys.has("ShiftRight") ? 4 : 1) * dt;
      let fz = move.z, sx = move.x, uy = move.y;
      if (keys.has("KeyW") || keys.has("ArrowUp")) fz += 1;
      if (keys.has("KeyS") || keys.has("ArrowDown")) fz -= 1;
      if (keys.has("KeyD") || keys.has("ArrowRight")) sx += 1;
      if (keys.has("KeyA") || keys.has("ArrowLeft")) sx -= 1;
      if (keys.has("Space") || keys.has("KeyE")) uy += 1;
      if (keys.has("KeyQ") || keys.has("KeyC")) uy -= 1;
      const f = fwd(), r = [Math.cos(cam.yaw), 0, -Math.sin(cam.yaw)];
      for (let a = 0; a < 3; a++) cam.pos[a] += (f[a] * fz + r[a] * sx + (a === 1 ? uy : 0)) * sp;
      needs = true;
    }
    if (needs) { needs = false; draw(); }
    if (moving()) raf = requestAnimationFrame(loop);
    else last = 0;
  };
  const dirty = () => { needs = true; if (!raf && !dead) raf = requestAnimationFrame(loop); };
  const look = (dx_, dy_, k = 0.0025) => {
    cam.yaw -= dx_ * k;
    cam.pitch = Math.max(-1.55, Math.min(1.55, cam.pitch - dy_ * k));
    dirty();
  };
  const isFs = () => ui.wrap.classList.contains("sw-fs");
  const active = () => document.pointerLockElement === canvas || ui.wrap.contains(document.activeElement) || isFs();
  const MOVE_KEYS = new Set(["KeyW", "KeyA", "KeyS", "KeyD", "KeyQ", "KeyE", "KeyC", "Space", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "ShiftLeft", "ShiftRight"]);
  const onKeyDown = (ev) => {
    if (!active() || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (ev.target.closest?.("input, textarea, select")) return;
    if (MOVE_KEYS.has(ev.code)) { ev.preventDefault(); keys.add(ev.code); dirty(); }
    else if (ev.code === "KeyF") { ev.preventDefault(); toggleFs(); }
    else if (ev.code === "KeyR") { ev.preventDefault(); start(); dirty(); }
    else if (ev.code === "Escape" && ui.wrap.classList.contains("sw-pseudo") && document.pointerLockElement !== canvas) { ev.preventDefault(); exitFs(); }
  };
  const onKeyUp = (ev) => { keys.delete(ev.code); };
  const onBlur = () => { keys.clear(); };
  addEventListener("keydown", onKeyDown);
  addEventListener("keyup", onKeyUp);
  addEventListener("blur", onBlur);

  const touches = new Map();
  let mouse = null;
  canvas.addEventListener("pointerdown", (ev) => {
    canvas.focus({ preventScroll: true });
    if (ev.pointerType === "touch") {
      const rect = canvas.getBoundingClientRect();
      const side = ev.clientX - rect.left < rect.width / 2 ? "move" : "look";
      touches.set(ev.pointerId, { side, x0: ev.clientX, y0: ev.clientY, x: ev.clientX, y: ev.clientY });
      if (side === "move") { ui.stick.hidden = false; ui.stick.style.left = `${ev.clientX - rect.left}px`; ui.stick.style.top = `${ev.clientY - rect.top}px`; }
      canvas.setPointerCapture(ev.pointerId);
      return;
    }
    if (document.pointerLockElement === canvas) return;
    mouse = { x: ev.clientX, y: ev.clientY, moved: 0 };
    canvas.setPointerCapture(ev.pointerId);
  });
  canvas.addEventListener("pointermove", (ev) => {
    if (document.pointerLockElement === canvas) { look(ev.movementX, ev.movementY); return; }
    const t = touches.get(ev.pointerId);
    if (t) {
      if (t.side === "look") look(ev.clientX - t.x, ev.clientY - t.y, 0.005);
      else { const r = 60; move.x = Math.max(-1, Math.min(1, (ev.clientX - t.x0) / r)); move.z = Math.max(-1, Math.min(1, -(ev.clientY - t.y0) / r)); dirty(); }
      t.x = ev.clientX; t.y = ev.clientY;
      return;
    }
    if (mouse) { mouse.moved += Math.abs(ev.clientX - mouse.x) + Math.abs(ev.clientY - mouse.y); look(ev.clientX - mouse.x, ev.clientY - mouse.y, 0.004); mouse.x = ev.clientX; mouse.y = ev.clientY; }
  });
  const endPointer = (ev) => {
    const t = touches.get(ev.pointerId);
    if (t) { touches.delete(ev.pointerId); if (t.side === "move") { move.x = move.z = 0; ui.stick.hidden = true; } return; }
    // A click (not a drag) captures the mouse for mouse-look.
    if (mouse && mouse.moved < 4 && ev.type === "pointerup" && canvas.requestPointerLock) {
      try { const p = canvas.requestPointerLock(); p?.catch?.(() => {}); } catch { /* drag-to-look still works */ }
    }
    mouse = null;
  };
  canvas.addEventListener("pointerup", endPointer);
  canvas.addEventListener("pointercancel", endPointer);
  canvas.addEventListener("wheel", (ev) => { ev.preventDefault(); speedMul = Math.max(0.05, Math.min(40, speedMul * (ev.deltaY < 0 ? 1.25 : 0.8))); showSpeed(); }, { passive: false });
  canvas.addEventListener("contextmenu", (ev) => ev.preventDefault());
  const hold = (btn, dir) => {
    const on = (ev) => { ev.preventDefault(); move.y = dir; dirty(); };
    const off = () => { if (move.y === dir) move.y = 0; };
    btn.addEventListener("pointerdown", on);
    for (const e of ["pointerup", "pointercancel", "pointerleave"]) btn.addEventListener(e, off);
  };
  hold(ui.upBtn, 1); hold(ui.downBtn, -1);
  ui.texBtn.addEventListener("click", () => { textured = !textured; ui.texBtn.textContent = `Textures: ${textured ? "on" : "off"}`; dirty(); });
  ui.resetBtn.addEventListener("click", () => { start(); dirty(); canvas.focus({ preventScroll: true }); });

  // Full screen: the real Fullscreen API where there is one, otherwise (iPhone) the view fills the
  // window. Either way the ✕ button and Esc leave it.
  const fsEl = () => document.fullscreenElement || document.webkitFullscreenElement;
  const syncFs = () => {
    const real = fsEl() === ui.wrap, pseudo = ui.wrap.classList.contains("sw-pseudo");
    ui.wrap.classList.toggle("sw-fs", real || pseudo);
    ui.fsBtn.textContent = real || pseudo ? "Exit full screen" : "⛶ Full screen";
    document.documentElement.classList.toggle("sw-noscroll", pseudo);
    dirty();
  };
  const enterFs = async () => {
    try {
      if (ui.wrap.requestFullscreen) { await ui.wrap.requestFullscreen({ navigationUI: "hide" }); return; }
      if (ui.wrap.webkitRequestFullscreen) { ui.wrap.webkitRequestFullscreen(); return; }
    } catch { /* fall back below */ }
    ui.wrap.classList.add("sw-pseudo");
    syncFs();
  };
  const exitFs = () => {
    if (document.pointerLockElement === canvas) document.exitPointerLock?.();
    if (ui.wrap.classList.contains("sw-pseudo")) { ui.wrap.classList.remove("sw-pseudo"); syncFs(); }
    if (fsEl() === ui.wrap) (document.exitFullscreen || document.webkitExitFullscreen)?.call(document)?.catch?.(() => {});
  };
  const toggleFs = () => isFs() ? exitFs() : enterFs();
  ui.fsBtn.addEventListener("click", toggleFs);
  ui.exitBtn.addEventListener("click", exitFs);
  document.addEventListener("fullscreenchange", syncFs);
  document.addEventListener("webkitfullscreenchange", syncFs);
  const onLock = () => { ui.wrap.classList.toggle("sw-locked", document.pointerLockElement === canvas); };
  document.addEventListener("pointerlockchange", onLock);

  const resize = new ResizeObserver(dirty);
  resize.observe(canvas);
  dirty();

  return {
    destroy() {
      dead = true;
      if (raf) cancelAnimationFrame(raf);
      resize.disconnect();
      removeEventListener("keydown", onKeyDown);
      removeEventListener("keyup", onKeyUp);
      removeEventListener("blur", onBlur);
      document.removeEventListener("fullscreenchange", syncFs);
      document.removeEventListener("webkitfullscreenchange", syncFs);
      document.removeEventListener("pointerlockchange", onLock);
      if (document.pointerLockElement === canvas) document.exitPointerLock?.();
      if (fsEl() === ui.wrap) (document.exitFullscreen || document.webkitExitFullscreen)?.call(document)?.catch?.(() => {});
      document.documentElement.classList.remove("sw-noscroll");
      for (const v of vaos) gl.deleteVertexArray(v);
      for (const b of buffers) gl.deleteBuffer(b);
      for (const t of textures.values()) gl.deleteTexture(t.tex);
      gl.getExtension("WEBGL_lose_context")?.loseContext();
    },
  };
}
