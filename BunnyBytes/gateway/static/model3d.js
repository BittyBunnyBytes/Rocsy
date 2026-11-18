// 3D model preview for the RAG library: orbit view, animation clips, expression presets + morph sliders,
// eye look, and a bone viewer (skeleton overlay, bone tree, pick a joint, rotate it).
// Loaded on demand by cache.js with import('/static/model3d.js'). glTF/GLB/VRM, FBX, OBJ, STL, PLY.
import * as THREE from './vendor/three/core/three.module.min.js';
import { OrbitControls } from './vendor/three/addons/controls/OrbitControls.js';
import { GLTFLoader } from './vendor/three/addons/loaders/GLTFLoader.js';

export const MODEL_EXT = /\.(glb|gltf|vrm|fbx|obj|stl|ply)$/i;
export const isModel = (name) => MODEL_EXT.test(name || '');

const esc = (v) => String(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ext = (name) => (name.match(MODEL_EXT)?.[1] || 'glb').toLowerCase();
const DEG = Math.PI / 180;

async function loadModel(url, name) {
  const kind = ext(name);
  if (kind === 'glb' || kind === 'gltf' || kind === 'vrm') {
    const gltf = await new GLTFLoader().loadAsync(url);
    return { root: gltf.scene, clips: gltf.animations || [] };
  }
  if (kind === 'fbx') {
    const { FBXLoader } = await import('./vendor/three/addons/loaders/FBXLoader.js');
    const root = await new FBXLoader().loadAsync(url);
    return { root, clips: root.animations || [] };
  }
  if (kind === 'obj') {
    const { OBJLoader } = await import('./vendor/three/addons/loaders/OBJLoader.js');
    return { root: await new OBJLoader().loadAsync(url), clips: [] };
  }
  const mod = kind === 'stl' ? await import('./vendor/three/addons/loaders/STLLoader.js') : await import('./vendor/three/addons/loaders/PLYLoader.js');
  const geo = await new (kind === 'stl' ? mod.STLLoader : mod.PLYLoader)().loadAsync(url);
  geo.computeVertexNormals();
  const material = new THREE.MeshStandardMaterial({ color: 0xb8c4cc, roughness: 0.6, vertexColors: !!geo.attributes.color });
  const root = new THREE.Group(); root.add(new THREE.Mesh(geo, material));
  return { root, clips: [] };
}

function stage(canvas, { alpha = false } = {}) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha, preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(32, 1, 0.01, 1000);
  scene.add(new THREE.HemisphereLight(0xdfe8ef, 0x1a2228, 1.6));
  const key = new THREE.DirectionalLight(0xffffff, 2.2); key.position.set(2, 4, 3); scene.add(key);
  const rim = new THREE.DirectionalLight(0x9fd8ff, 1.4); rim.position.set(-3, 2, -3); scene.add(rim);
  return { renderer, scene, camera };
}

function frame(camera, root, controls, view = 'front') {
  const box = new THREE.Box3().setFromObject(root);
  const size = box.getSize(new THREE.Vector3()), center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(size.length() / 2, 1e-3);
  const dist = radius / Math.sin(camera.fov * DEG / 2) * 0.92;
  const dir = { front: [0.32, 0.12, 1], side: [1, 0.1, 0], back: [-0.2, 0.15, -1], top: [0, 1, 0.01], face: [0, 0, 1] }[view] || [0.32, 0.12, 1];
  let target = center, d = dist;
  if (view === 'face') { target = new THREE.Vector3(center.x, box.max.y - size.y * 0.12, center.z); d = dist * 0.28; }
  camera.position.copy(target).add(new THREE.Vector3(...dir).normalize().multiplyScalar(d));
  camera.near = d / 100; camera.far = d * 100; camera.updateProjectionMatrix();
  camera.lookAt(target);
  if (controls) { controls.target.copy(target); controls.update(); }
  return { box, size, center, radius };
}

function stats(root) {
  let tris = 0, verts = 0, meshes = 0, morphs = 0; const bones = new Set(), materials = new Set();
  root.traverse(o => {
    if (o.isBone) bones.add(o);
    if (!o.isMesh) return;
    meshes++; const g = o.geometry; const n = g.attributes.position?.count || 0; verts += n;
    tris += (g.index ? g.index.count : n) / 3;
    morphs += Object.keys(o.morphTargetDictionary || {}).length;
    (Array.isArray(o.material) ? o.material : [o.material]).forEach(m => materials.add(m));
  });
  return { tris: Math.round(tris), verts, meshes, bones: bones.size, morphs, materials: materials.size };
}

function extrasOf(root) {
  // Expression presets saved by the exporter as node extras (Blender custom properties): *_emotions / emotions.
  const out = {};
  root.traverse(o => {
    for (const [k, v] of Object.entries(o.userData || {})) {
      if (!/(^|_)(emotions|expressions)$/i.test(k)) continue;
      try { Object.assign(out, typeof v === 'string' ? JSON.parse(v) : v); } catch { /* not JSON */ }
    }
  });
  return out;
}

// ── thumbnails: one shared offscreen renderer, one model at a time ───────────────────────────────
const thumbs = new Map(); let thumbStage = null, thumbQueue = Promise.resolve();
export function thumbnail(url, name, size = 320) {
  if (thumbs.has(url)) return thumbs.get(url);
  const job = thumbQueue.then(async () => {
    if (!thumbStage) { const c = document.createElement('canvas'); thumbStage = stage(c, { alpha: true }); }
    const { renderer, scene, camera } = thumbStage;
    renderer.setSize(size, size, false); camera.aspect = 1; camera.updateProjectionMatrix();
    const { root, clips } = await loadModel(url, name);
    scene.add(root);
    try {
      const idle = clips.find(c => /idle/i.test(c.name));
      if (idle) { const mixer = new THREE.AnimationMixer(root); mixer.clipAction(idle).play(); mixer.update(0.3); }
      frame(camera, root, null, 'front');
      renderer.render(scene, camera);
      return renderer.domElement.toDataURL('image/png');
    } finally {
      scene.remove(root);
      root.traverse(o => { o.geometry?.dispose?.(); (Array.isArray(o.material) ? o.material : [o.material]).forEach(m => { if (!m) return; for (const v of Object.values(m)) v?.isTexture && v.dispose(); m.dispose?.(); }); });
    }
  });
  thumbQueue = job.catch(() => {});
  thumbs.set(url, job); job.catch(() => thumbs.delete(url));
  return job;
}

// ── viewer dialog ──────────────────────────────────────────────────────────────────────────────
let V = null;   // the single viewer instance

const ICONS = {
  play: '<path d="M7 5l12 7-12 7z" fill="currentColor" stroke="none"/>',
  pause: '<path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z" fill="currentColor" stroke="none"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  cube: '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5"/>',
  camera: '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>',
};
const icon = (d, s = 16) => `<svg viewBox="0 0 24 24" width="${s}" height="${s}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;

function buildDialog() {
  const dlg = document.createElement('dialog');
  dlg.className = 'dlg m3d-dlg'; dlg.setAttribute('aria-labelledby', 'm3d-title');
  dlg.innerHTML = `
  <header class="dlg-head"><span class="dlg-mark" aria-hidden="true">${icon(ICONS.cube, 17)}</span>
    <div><h2 id="m3d-title">3D model</h2><p id="m3d-sub"></p></div>
    <button type="button" class="icon-button" data-act="close" aria-label="Close">${icon(ICONS.close, 14)}</button></header>
  <div class="m3d-body">
    <div class="m3d-stage">
      <canvas id="m3d-canvas" aria-label="3D preview"></canvas>
      <div class="m3d-status" id="m3d-status">Loading…</div>
      <div class="m3d-views" role="group" aria-label="Camera">
        <button type="button" data-view="front">Front</button><button type="button" data-view="side">Side</button><button type="button" data-view="back">Back</button><button type="button" data-view="face">Face</button>
        <button type="button" data-act="shot" title="Save a PNG of this view">${icon(ICONS.camera, 14)}</button>
      </div>
      <div class="m3d-bone-tag" id="m3d-bone-tag" hidden></div>
      <div class="m3d-transport" id="m3d-transport" hidden>
        <button type="button" class="icon-button" data-act="play" aria-label="Play or pause">${icon(ICONS.pause)}</button>
        <input type="range" id="m3d-time" min="0" max="1000" value="0" aria-label="Timeline">
        <span class="m3d-time" id="m3d-clock">0.00 s</span>
        <select id="m3d-speed" aria-label="Speed"><option value="0.25">¼×</option><option value="0.5">½×</option><option value="1" selected>1×</option><option value="1.5">1.5×</option><option value="2">2×</option></select>
      </div>
    </div>
    <aside class="m3d-side">
      <div class="m3d-tabs" role="tablist">
        <button type="button" role="tab" data-tab="anim" aria-selected="true">Animate</button>
        <button type="button" role="tab" data-tab="face" aria-selected="false">Face</button>
        <button type="button" role="tab" data-tab="bones" aria-selected="false">Bones</button>
        <button type="button" role="tab" data-tab="info" aria-selected="false">Info</button>
      </div>
      <section class="m3d-pane" data-pane="anim">
        <div class="m3d-label">Animations</div><div class="m3d-chips" id="m3d-clips"></div>
        <div class="m3d-label">Display</div>
        <div class="m3d-toggles" id="m3d-display">
          <label><input type="checkbox" data-show="skeleton"> Skeleton</label>
          <label><input type="checkbox" data-show="wire"> Wireframe</label>
          <label><input type="checkbox" data-show="grid" checked> Floor</label>
          <label><input type="checkbox" data-show="spin"> Turntable</label>
        </div>
        <div class="m3d-label">Background</div>
        <div class="m3d-chips" id="m3d-bg">
          <button type="button" class="m3d-chip" data-bg="#4a4e52" aria-pressed="true">Studio grey</button>
          <button type="button" class="m3d-chip" data-bg="#0d1419">Dark</button>
          <button type="button" class="m3d-chip" data-bg="#f2f3f4">White</button>
          <button type="button" class="m3d-chip" data-bg="#00b140" title="Chroma key for video editing">Green screen</button>
        </div>
        <div class="m3d-label">Parts</div><div class="m3d-toggles" id="m3d-parts"></div>
      </section>
      <section class="m3d-pane" data-pane="face" hidden>
        <div class="m3d-label">Expression</div><div class="m3d-chips" id="m3d-emotions"></div>
        <div id="m3d-look-wrap"><div class="m3d-label">Eyes look</div>
          <div class="m3d-sliders"><label>Left / right <input type="range" id="m3d-look-x" min="-25" max="25" value="0"></label>
          <label>Up / down <input type="range" id="m3d-look-y" min="-15" max="15" value="0"></label></div></div>
        <div class="m3d-label">Morph targets</div><div class="m3d-sliders" id="m3d-morphs"></div>
      </section>
      <section class="m3d-pane" data-pane="bones" hidden>
        <p class="m3d-hint">Click a joint on the model (skeleton is shown on this tab) or pick a bone below, then rotate it. Posing pauses the animation.</p>
        <input type="search" id="m3d-bone-filter" placeholder="Filter bones" aria-label="Filter bones">
        <div class="m3d-bones" id="m3d-bones"></div>
        <div class="m3d-pose" id="m3d-pose" hidden>
          <div class="m3d-label" id="m3d-pose-name"></div>
          <div class="m3d-sliders">
            <label>X <input type="range" data-axis="x" min="-180" max="180" value="0"><output>0°</output></label>
            <label>Y <input type="range" data-axis="y" min="-180" max="180" value="0"><output>0°</output></label>
            <label>Z <input type="range" data-axis="z" min="-180" max="180" value="0"><output>0°</output></label>
          </div>
          <div class="m3d-row"><button type="button" class="button small" data-act="reset-bone">Reset bone</button><button type="button" class="button small" data-act="reset-pose">Reset pose</button></div>
        </div>
      </section>
      <section class="m3d-pane" data-pane="info" hidden><dl class="m3d-info" id="m3d-info"></dl></section>
    </aside>
  </div>`;
  document.body.append(dlg);
  return dlg;
}

class Viewer {
  constructor() {
    this.dlg = buildDialog();
    this.$ = (s) => this.dlg.querySelector(s);
    const canvas = this.$('#m3d-canvas');
    Object.assign(this, stage(canvas));
    this.scene.background = new THREE.Color(0x4a4e52);
    this.controls = new OrbitControls(this.camera, canvas); this.controls.enableDamping = true;
    this.grid = new THREE.GridHelper(4, 16, 0x6a7076, 0x5a5f64); this.scene.add(this.grid);
    this.clock = new THREE.Clock(); this.mixer = null; this.action = null; this.playing = true;
    this.root = null; this.skeleton = null; this.bones = []; this.morphMeshes = []; this.emotions = {};
    this.faceTarget = null; this.look = { x: 0, y: 0 }; this.selected = null; this.posed = new Map();
    this.marker = new THREE.Mesh(new THREE.SphereGeometry(1, 12, 8), new THREE.MeshBasicMaterial({ color: 0xffc04d, depthTest: false, transparent: true, opacity: 0.9 }));
    this.marker.renderOrder = 999; this.marker.visible = false; this.scene.add(this.marker);
    this.wire = false; this.spin = false;
    this.wireDrawn = new Map();
    this.wireUp();
    new ResizeObserver(() => this.resize()).observe(this.$('.m3d-stage'));
    const tick = () => { this.raf = requestAnimationFrame(tick); if (this.dlg.open) this.render(); };
    tick();
  }

  wireUp() {
    const $ = this.$;
    this.dlg.addEventListener('click', e => {
      if (e.target === this.dlg) return this.dlg.close();
      const act = e.target.closest('[data-act]')?.dataset.act;
      if (act === 'close') this.dlg.close();
      if (act === 'play') this.togglePlay();
      if (act === 'shot') this.screenshot();
      if (act === 'reset-bone' && this.selected) { this.selected.quaternion.copy(this.selected.userData.rest); this.posed.delete(this.selected); this.syncPose(); }
      if (act === 'reset-pose') { this.posed.forEach((_, b) => b.quaternion.copy(b.userData.rest)); this.posed.clear(); this.syncPose(); }
      const view = e.target.closest('[data-view]')?.dataset.view; if (view && this.root) frame(this.camera, this.root, this.controls, view);
      const tab = e.target.closest('[data-tab]')?.dataset.tab; if (tab) this.setTab(tab);
    });
    this.dlg.addEventListener('close', () => { this.mixer?.stopAllAction(); });
    $('#m3d-time').addEventListener('input', () => {
      if (!this.action) return;
      this.playing = false; this.syncPlay();
      this.action.paused = false; this.action.time = $('#m3d-time').value / 1000 * this.action.getClip().duration; this.mixer.update(0);
    });
    $('#m3d-speed').onchange = () => { if (this.mixer) this.mixer.timeScale = +$('#m3d-speed').value; };
    $('#m3d-display').addEventListener('change', e => {
      const what = e.target.dataset.show, on = e.target.checked;
      if (what === 'skeleton' && this.skelHelper) this.skelHelper.visible = on;
      if (what === 'grid') this.grid.visible = on;
      if (what === 'spin') this.spin = on;
      if (what === 'wire') { this.wire = on; this.root?.traverse(o => o.isMesh && (Array.isArray(o.material) ? o.material : [o.material]).forEach(m => { m.wireframe = on; })); }
    });
    $('#m3d-bg').addEventListener('click', e => {
      const b = e.target.closest('[data-bg]'); if (!b) return;
      this.scene.background = new THREE.Color(b.dataset.bg);
      $('#m3d-bg').querySelectorAll('[data-bg]').forEach(x => x.setAttribute('aria-pressed', String(x === b)));
      try { localStorage.setItem('m3d-bg', b.dataset.bg); } catch { /* private window */ }
    });
    try { const saved = localStorage.getItem('m3d-bg'); const b = saved && $('#m3d-bg').querySelector(`[data-bg="${saved}"]`); if (b) b.click(); } catch { /* none */ }
    $('#m3d-look-x').oninput = $('#m3d-look-y').oninput = () => { this.look = { x: +$('#m3d-look-x').value, y: +$('#m3d-look-y').value }; };
    $('#m3d-bone-filter').oninput = () => this.renderBones();
    $('#m3d-pose').addEventListener('input', e => {
      if (!this.selected || !e.target.dataset.axis) return;
      this.stopForPose();
      const r = {}; this.$('#m3d-pose').querySelectorAll('[data-axis]').forEach(i => { r[i.dataset.axis] = +i.value; i.nextElementSibling.value = `${i.value}°`; });
      this.posed.set(this.selected, r);
      this.selected.quaternion.copy(this.selected.userData.rest).multiply(new THREE.Quaternion().setFromEuler(new THREE.Euler(r.x * DEG, r.y * DEG, r.z * DEG)));
    });
    // pick a joint: nearest bone head on screen to the click (skeleton or Bones tab)
    let down = null;
    this.renderer.domElement.addEventListener('pointerdown', e => { down = [e.clientX, e.clientY]; });
    this.renderer.domElement.addEventListener('pointerup', e => {
      if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;
      if (!this.bones.length || !(this.skelHelper?.visible)) return;
      const rect = this.renderer.domElement.getBoundingClientRect(); let best = null, bestD = 14;
      const p = new THREE.Vector3();
      for (const b of this.bones) {
        b.getWorldPosition(p).project(this.camera);
        const x = (p.x + 1) / 2 * rect.width + rect.left, y = (1 - p.y) / 2 * rect.height + rect.top;
        const d = Math.hypot(x - e.clientX, y - e.clientY); if (d < bestD && p.z < 1) { bestD = d; best = b; }
      }
      if (best) this.selectBone(best, true);
    });
  }

  setTab(tab) {
    this.dlg.querySelectorAll('[data-tab]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.tab === tab)));
    this.dlg.querySelectorAll('[data-pane]').forEach(p => { p.hidden = p.dataset.pane !== tab; });
    this.tab = tab;
    if (this.skelHelper) this.skelHelper.visible = tab === 'bones' || this.$('[data-show=skeleton]').checked;
    this.marker.visible = tab === 'bones' && !!this.selected;
    this.$('#m3d-bone-tag').hidden = !(tab === 'bones' && this.selected);
    if (tab === 'face' && this.root) frame(this.camera, this.root, this.controls, 'face');
  }

  resize() {
    const el = this.$('.m3d-stage'); const w = el.clientWidth, h = el.clientHeight; if (!w || !h) return;
    this.renderer.setSize(w, h, false); this.camera.aspect = w / h; this.camera.updateProjectionMatrix();
  }

  async open(url, name, meta = {}) {
    if (!this.dlg.open) this.dlg.showModal();
    this.$('#m3d-title').textContent = meta.title || name;
    this.$('#m3d-sub').textContent = meta.title && meta.title !== name ? name : 'Drag to orbit · scroll to zoom · right-drag to pan';
    this.$('#m3d-status').hidden = false; this.$('#m3d-status').textContent = 'Loading…';
    this.unload(); this.resize();
    let loaded;
    try { loaded = await loadModel(url, name); }
    catch (err) { this.$('#m3d-status').textContent = `Could not load this model: ${err?.message || err}`; return; }
    this.$('#m3d-status').hidden = true;
    const { root, clips } = loaded; this.root = root; this.clips = clips;
    this.scene.add(root);
    const fit = frame(this.camera, root, this.controls, 'front');
    this.grid.position.y = fit.box.min.y; this.grid.scale.setScalar(Math.max(fit.size.x, fit.size.y, fit.size.z) / 2 || 1);
    this.marker.scale.setScalar(fit.radius * 0.012);
    // bones / skeleton
    this.bones = []; root.traverse(o => { if (o.isBone) { this.bones.push(o); o.userData.rest = o.quaternion.clone(); } });
    if (this.bones.length) {
      this.skelHelper = new THREE.SkeletonHelper(root); this.skelHelper.material.depthTest = false; this.skelHelper.material.transparent = true;
      this.skelHelper.renderOrder = 998; this.skelHelper.visible = this.$('[data-show=skeleton]').checked; this.scene.add(this.skelHelper);
    }
    this.eyes = this.bones.filter(b => /eye/i.test(b.name));
    // morphs / expressions
    this.morphMeshes = []; root.traverse(o => { if (o.isMesh && o.morphTargetDictionary && Object.keys(o.morphTargetDictionary).length) this.morphMeshes.push(o); });
    this.emotions = extrasOf(root);
    this.faceTarget = null;
    // animation
    this.mixer = new THREE.AnimationMixer(root); this.mixer.timeScale = +this.$('#m3d-speed').value;
    this.renderClips(); this.renderEmotions(); this.renderMorphs(); this.renderParts(); this.renderBones(); this.renderInfo(meta);
    this.$('#m3d-look-wrap').hidden = !this.eyes.length;
    const first = clips.find(c => /idle/i.test(c.name)) || clips[0];
    if (first) this.playClip(first); else this.$('#m3d-transport').hidden = true;
    this.setTab(this.tab || 'anim');
  }

  unload() {
    this.mixer?.stopAllAction(); this.action = null; this.selected = null; this.posed.clear(); this.look = { x: 0, y: 0 };
    this.$('#m3d-look-x').value = 0; this.$('#m3d-look-y').value = 0;
    if (this.skelHelper) { this.scene.remove(this.skelHelper); this.skelHelper.dispose?.(); this.skelHelper = null; }
    if (this.root) {
      this.scene.remove(this.root);
      this.root.traverse(o => { o.geometry?.dispose?.(); (Array.isArray(o.material) ? o.material : [o.material]).forEach(m => { if (!m) return; for (const v of Object.values(m)) v?.isTexture && v.dispose(); m.dispose?.(); }); });
      this.root = null;
    }
    this.marker.visible = false;
  }

  renderClips() {
    const host = this.$('#m3d-clips');
    if (!this.clips.length) { host.innerHTML = '<span class="m3d-none">No animations in this file</span>'; return; }
    host.innerHTML = this.clips.map((c, i) => `<button type="button" class="m3d-chip" data-clip="${i}">${esc(c.name || `Clip ${i + 1}`)}<small>${c.duration.toFixed(1)}s</small></button>`).join('');
    host.querySelectorAll('[data-clip]').forEach(b => b.onclick = () => this.playClip(this.clips[+b.dataset.clip]));
  }

  playClip(clip) {
    this.posed.forEach((_, b) => b.quaternion.copy(b.userData.rest)); this.posed.clear(); this.syncPose();
    this.mixer.stopAllAction();
    this.root.traverse(o => { if (o.isBone) o.quaternion.copy(o.userData.rest); });
    this.action = this.mixer.clipAction(clip); this.action.reset().setLoop(THREE.LoopRepeat).play();
    this.playing = true; this.syncPlay();
    this.$('#m3d-transport').hidden = false;
    this.$('#m3d-clips').querySelectorAll('[data-clip]').forEach(b => b.setAttribute('aria-pressed', String(this.clips[+b.dataset.clip] === clip)));
  }

  togglePlay() { if (!this.action) return; this.playing = !this.playing; if (this.playing && this.posed.size) this.playClip(this.action.getClip()); this.syncPlay(); }
  syncPlay() { this.$('[data-act=play]').innerHTML = icon(this.playing ? ICONS.pause : ICONS.play); }
  stopForPose() {
    if (!this.playing) return;
    this.playing = false; this.syncPlay();   // keeps the current frame; bone sliders add on top of it
  }

  renderEmotions() {
    const host = this.$('#m3d-emotions'); const names = Object.keys(this.emotions);
    if (!this.morphMeshes.length) { host.innerHTML = '<span class="m3d-none">No face morphs in this file</span>'; return; }
    const list = ['Clip', ...names];
    host.innerHTML = list.map(n => `<button type="button" class="m3d-chip" data-emotion="${esc(n)}" aria-pressed="${n === 'Clip'}">${n === 'Clip' ? 'From clip' : esc(n)}</button>`).join('')
      + (names.length ? '' : '<span class="m3d-none">No saved presets — use the sliders</span>');
    host.querySelectorAll('[data-emotion]').forEach(b => b.onclick = () => {
      const n = b.dataset.emotion; this.faceTarget = n === 'Clip' ? null : this.emotions[n] || {};
      host.querySelectorAll('[data-emotion]').forEach(x => x.setAttribute('aria-pressed', String(x === b)));
      this.$('#m3d-morphs').querySelectorAll('input').forEach(i => { i.value = this.faceTarget ? Math.round((this.faceTarget[i.dataset.morph] || 0) * 100) : 0; });
    });
  }

  renderMorphs() {
    const host = this.$('#m3d-morphs'); const seen = new Set(); const rows = [];
    for (const m of this.morphMeshes) for (const k of Object.keys(m.morphTargetDictionary)) if (!seen.has(k)) { seen.add(k); rows.push(k); }
    host.innerHTML = rows.length ? rows.map(k => `<label>${esc(k)} <input type="range" min="0" max="100" value="0" data-morph="${esc(k)}"></label>`).join('') : '<span class="m3d-none">None</span>';
    host.oninput = (e) => {
      const k = e.target.dataset.morph; if (!k) return;
      if (!this.faceTarget) { this.faceTarget = {}; this.$('#m3d-emotions').querySelectorAll('[data-emotion]').forEach(x => x.setAttribute('aria-pressed', 'false')); }
      this.faceTarget = { ...this.faceTarget, [k]: e.target.value / 100 };
    };
  }

  renderParts() {
    const host = this.$('#m3d-parts'); const meshes = [];
    this.root.traverse(o => { if (o.isMesh) meshes.push(o); });
    const groups = new Map();
    for (const m of meshes) { const key = (m.parent?.isGroup && m.parent.name && m.parent !== this.root) ? m.parent.name : m.name || 'Mesh'; (groups.get(key) || groups.set(key, []).get(key)).push(m); }
    if (groups.size < 2) { host.innerHTML = '<span class="m3d-none">One mesh</span>'; return; }
    host.innerHTML = [...groups.keys()].map((k, i) => `<label><input type="checkbox" checked data-part="${i}"> ${esc(k.replace(/_/g, ' '))}</label>`).join('');
    const arr = [...groups.values()];
    host.onchange = (e) => { const i = e.target.dataset.part; if (i != null) arr[+i].forEach(m => { m.visible = e.target.checked; }); };
  }

  renderBones() {
    const host = this.$('#m3d-bones');
    if (!this.bones.length) { host.innerHTML = '<span class="m3d-none">This model has no skeleton</span>'; return; }
    const q = this.$('#m3d-bone-filter').value.trim().toLowerCase();
    const depth = (b) => { let d = 0, p = b.parent; while (p && p.isBone) { d++; p = p.parent; } return d; };
    host.innerHTML = this.bones.filter(b => !q || b.name.toLowerCase().includes(q))
      .map(b => `<button type="button" class="m3d-bone ${b === this.selected ? 'on' : ''} ${this.posed.has(b) ? 'posed' : ''}" data-bone="${this.bones.indexOf(b)}" style="--d:${q ? 0 : depth(b)}">${esc(b.name || '(unnamed)')}</button>`).join('');
    host.querySelectorAll('[data-bone]').forEach(btn => btn.onclick = () => this.selectBone(this.bones[+btn.dataset.bone]));
  }

  selectBone(b, fromCanvas = false) {
    this.selected = b; if (fromCanvas && this.tab !== 'bones') this.setTab('bones');
    this.$('#m3d-pose').hidden = false; this.$('#m3d-pose-name').textContent = b.name;
    this.marker.visible = true; this.$('#m3d-bone-tag').hidden = false; this.$('#m3d-bone-tag').textContent = b.name;
    this.syncPose(); this.renderBones();
    this.$('#m3d-bones').querySelector('.on')?.scrollIntoView({ block: 'nearest' });
  }

  syncPose() {
    const r = this.selected && this.posed.get(this.selected) || { x: 0, y: 0, z: 0 };
    this.$('#m3d-pose').querySelectorAll('[data-axis]').forEach(i => { i.value = r[i.dataset.axis]; i.nextElementSibling.value = `${r[i.dataset.axis]}°`; });
    this.renderBones();
  }

  renderInfo(meta) {
    const s = stats(this.root); const kb = meta.size ? (meta.size > 1048576 ? `${(meta.size / 1048576).toFixed(1)} MB` : `${Math.round(meta.size / 1024)} KB`) : '';
    const fit = new THREE.Box3().setFromObject(this.root).getSize(new THREE.Vector3());
    const rows = [['Format', ext(meta.name || '').toUpperCase()], ['File size', kb], ['Triangles', s.tris.toLocaleString()], ['Vertices', s.verts.toLocaleString()],
      ['Meshes', s.meshes], ['Materials', s.materials], ['Bones', s.bones || 'none'], ['Morph targets', s.morphs || 'none'],
      ['Animations', this.clips.length ? this.clips.map(c => c.name).join(', ') : 'none'],
      ['Expressions', Object.keys(this.emotions).join(', ') || 'none'],
      ['Size', `${fit.x.toFixed(2)} × ${fit.y.toFixed(2)} × ${fit.z.toFixed(2)} (W × H × D)`]];
    this.$('#m3d-info').innerHTML = rows.filter(r => r[1] !== '').map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
  }

  applyFace(dt) {
    // Our own eased copy of the face: a clip with morph tracks rewrites the influences every frame.
    const k = 1 - Math.exp(-dt * 14); this.faceCur = this.faceCur || {};
    for (const m of this.morphMeshes) for (const [name, i] of Object.entries(m.morphTargetDictionary)) {
      if (!this.faceTarget) { this.faceCur[name] = m.morphTargetInfluences[i]; continue; }
      const cur = this.faceCur[name] ?? m.morphTargetInfluences[i];
      m.morphTargetInfluences[i] = this.faceCur[name] = cur + ((this.faceTarget[name] || 0) - cur) * k;
    }
  }

  applyLook() {
    if (!this.eyes?.length || (!this.look.x && !this.look.y)) return;
    // eye bones point out of the face with Z up (Blender export): yaw about local Z, pitch about local X
    const q = new THREE.Quaternion().setFromEuler(new THREE.Euler(this.look.y * DEG, 0, this.look.x * DEG));
    for (const b of this.eyes) b.quaternion.multiply(q);
  }

  render() {
    const dt = Math.min(this.clock.getDelta(), 0.1);
    if (this.mixer && this.playing) this.mixer.update(dt);
    if (this.mixer && !this.playing && this.posed.size) {
      // posed: re-apply the slider rotations on top of the paused frame
      this.posed.forEach((r, b) => b.quaternion.copy(b.userData.rest).multiply(new THREE.Quaternion().setFromEuler(new THREE.Euler(r.x * DEG, r.y * DEG, r.z * DEG))));
    }
    this.applyFace(dt);
    const eyesBefore = this.eyes?.map(b => b.quaternion.clone());
    this.applyLook();
    if (this.spin && this.root) this.root.rotation.y += dt * 0.6;
    if (this.selected && this.marker.visible) this.selected.getWorldPosition(this.marker.position);
    if (this.action) {
      const d = this.action.getClip().duration || 1;
      this.$('#m3d-time').value = Math.round((this.action.time % d) / d * 1000);
      this.$('#m3d-clock').textContent = `${(this.action.time % d).toFixed(2)} / ${d.toFixed(2)} s`;
    }
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
    this.eyes?.forEach((b, i) => b.quaternion.copy(eyesBefore[i]));   // look is an overlay, not a pose
  }

  screenshot() {
    const keepGrid = this.grid.visible, keepSkel = this.skelHelper?.visible, keepMark = this.marker.visible;
    this.grid.visible = false; if (this.skelHelper) this.skelHelper.visible = false; this.marker.visible = false;
    const bg = this.scene.background; this.scene.background = null; this.renderer.setClearAlpha(0);
    this.renderer.render(this.scene, this.camera);
    const a = document.createElement('a'); a.href = this.renderer.domElement.toDataURL('image/png');
    a.download = `${(this.$('#m3d-title').textContent || 'model').replace(/[^\w.-]+/g, '_')}.png`; a.click();
    this.scene.background = bg; this.renderer.setClearAlpha(1); this.grid.visible = keepGrid; if (this.skelHelper) this.skelHelper.visible = keepSkel; this.marker.visible = keepMark;
  }
}

export function openViewer(url, name, meta = {}) {
  V = V || new Viewer();
  window.__m3d = V;   // for poking at it from the console
  return V.open(url, name, { name, ...meta });
}
