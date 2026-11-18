"use strict";
(() => {
  const P = document.getElementById("m3d-script").dataset.prefix;
  const $ = (id) => document.getElementById(id);
  const VIEWS = ["front", "left", "back", "right"];
  const files = {};
  let models = [];
  let rigMethods = { builtin: "Built-in humanoid" };
  let selected = null;
  let lastJobs = [];
  let source = "picture";
  const touch = matchMedia("(pointer: coarse)").matches;
  const narrow = () => matchMedia("(max-width: 860px)").matches;
  // Rocsy's mobile data saver (gateway data-saver.js): don't download 3D models until asked.
  const saveData = () => !!(window.RocsyDataSaver && window.RocsyDataSaver.active);
  const loaded = new Set();   // jobs whose model was opened this visit

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function api(path, opts = {}) {
    const r = await fetch(P + path, { credentials: "same-origin", ...opts });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || body.error || `HTTP ${r.status}`);
    return body;
  }

  function currentModel() { return models.find((m) => m.name === $("model").value); }

  function renderViews() {
    const m = currentModel();
    const views = m && m.multiview && source !== "repaint" ? VIEWS : ["front"];
    const box = $("views");
    box.className = "views" + (views.length === 1 ? " single" : "");
    box.innerHTML = "";
    for (const v of views) {
      const label = document.createElement("label");
      label.className = "drop";
      label.innerHTML = `<span class="tag">${views.length === 1 ? (source === "repaint" ? "front picture (optional)" : "picture") : v}${v === "front" ? "" : " (optional)"}</span>` +
        `<span>${touch ? "Tap to choose a picture or take a photo" : "Drop a picture or click"}</span><input type="file" accept="image/*">`;
      const input = label.querySelector("input");
      const show = () => {
        label.querySelector("img")?.remove();
        label.querySelector(".clear")?.remove();
        if (!files[v]) return;
        const img = document.createElement("img");
        img.src = URL.createObjectURL(files[v]);
        img.alt = v;
        const clear = document.createElement("button");
        clear.type = "button"; clear.className = "clear"; clear.textContent = "×"; clear.title = "Remove";
        clear.addEventListener("click", (e) => { e.preventDefault(); delete files[v]; show(); });
        label.append(img, clear);
      };
      input.addEventListener("change", () => { if (input.files[0]) { files[v] = input.files[0]; show(); } });
      label.addEventListener("dragover", (e) => { e.preventDefault(); label.classList.add("over"); });
      label.addEventListener("dragleave", () => label.classList.remove("over"));
      label.addEventListener("drop", (e) => {
        e.preventDefault(); label.classList.remove("over");
        const f = [...e.dataTransfer.files].find((x) => x.type.startsWith("image/"));
        if (f) { files[v] = f; show(); }
      });
      box.append(label);
      show();
    }
    $("model-note").textContent = m && m.textured
      ? "Makes a fully textured model by itself (colour, metal, roughness, normal and AO maps) and removes the background automatically. Takes a few minutes."
      : m && m.multiview
        ? "Front is required. Add left, back and right views of the same object for a more accurate shape."
        : "One clear picture of a single object on a plain background works best.";
    // TRELLIS.2 / Pixal3D texture the model themselves: the studio's own painting options don't apply.
    const own = !!(m && m.textured) && source !== "repaint";
    for (const id of ["paint-row", "sides-row", "rembg-row"]) $(id).classList.toggle("engine-off", own);
  }

  async function loadModels() {
    const info = await api("/api/models").catch(() => ({ models: [], error: "Couldn't reach the plugin." }));
    models = info.models || [];
    const sel = $("model");
    sel.innerHTML = models.map((m) => `<option value="${esc(m.name)}">${esc(m.label)}</option>`).join("");
    $("rembg-row").hidden = !info.rembg;
    rigMethods = info.rig_methods || rigMethods;
    $("rig-method").innerHTML = Object.entries(rigMethods).map(([k, label]) => `<option value="${esc(k)}">${esc(label)}</option>`).join("");
    $("rig-method-row").hidden = Object.keys(rigMethods).length < 2 || !$("character").checked;
    $("paint-row").hidden = !info.paint;
    $("paint").checked = !!info.paint;
    $("image-model").innerHTML = (info.image_models || []).map((m) => `<option value="${esc(m.key)}">${esc(m.label)}</option>`).join("");
    $("go").disabled = !models.length;
    if (!models.length) setMsg(info.error || "No Hunyuan3D checkpoints found in ComfyUI's models/checkpoints.", true);
    renderViews();
  }

  function setMsg(text, err = false) { const m = $("form-msg"); m.textContent = text; m.className = "msg" + (err ? " err" : ""); }

  $("model").addEventListener("change", renderViews);
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    source = tab.dataset.source;
    document.querySelectorAll(".tab").forEach((t) => { t.classList.toggle("on", t === tab); t.setAttribute("aria-selected", String(t === tab)); });
    $("views").hidden = source === "prompt";
    $("model-note").hidden = source !== "picture";
    $("prompt-box").hidden = source === "picture";
    $("mesh-box").hidden = source !== "repaint";
    $("image-model-row").hidden = source === "repaint";
    $("prompt-label").textContent = source === "repaint" ? "Or describe how it should look" : "Describe the object";
    $("paint-row").classList.toggle("dim", source === "repaint");
    $("rembg-row").classList.toggle("dim", source !== "picture");
    $("model").disabled = source === "repaint";
    $("character-row").hidden = source === "repaint";
    $("sides-row").hidden = source === "repaint";
    renderViews();
  }));
  $("form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const m = currentModel();
    const fd = new FormData($("form"));
    fd.set("remove_background", $("rembg").checked ? "true" : "false");
    fd.set("paint", $("paint").checked && !(m && m.textured) ? "true" : "false");
    fd.set("character", $("character").checked && source !== "repaint" ? "true" : "false");
    if (!$("character").checked) fd.delete("rig_method");
    fd.set("all_sides", $("all-sides").checked && source !== "repaint" && $("paint").checked ? "true" : "false");
    if (source === "repaint") {
      const mesh = $("mesh").files[0];
      if (!mesh) { setMsg("Choose your model file (.glb, .obj or .stl).", true); return; }
      if (!files.front && !$("prompt").value.trim()) { setMsg("Add a front picture or describe how it should look.", true); return; }
      fd.set("model", m ? m.name : "");
      fd.append("mesh", mesh);
      if (files.front) { fd.delete("prompt"); fd.append("front", files.front); }
    } else if (source === "prompt") {
      if (!$("prompt").value.trim()) { setMsg("Type what the object should be.", true); return; }
    } else {
      fd.delete("prompt");
      if (!files.front) { setMsg("Add a front picture first.", true); return; }
      for (const v of (m.multiview ? VIEWS : ["front"])) if (files[v]) fd.append(v, files[v]);
    }
    $("go").disabled = true; setMsg("Uploading…");
    try {
      await api("/api/jobs", { method: "POST", body: fd });
      setMsg("Queued. It takes a GPU turn like Image and Video jobs.");
      await refresh();
    } catch (err) { setMsg(err.message, true); }
    finally { $("go").disabled = !models.length; }
  });

  function stateText(j) {
    if (j.status === "done") return `Done${j.size ? " · " + (j.size / 1048576).toFixed(1) + " MB" : ""}`;
    if (j.status === "error") return j.error || "Failed";
    if (j.status === "cancelled") return "Cancelled";
    return `${j.stage || j.status}${j.status === "running" ? " · " + Math.round((j.progress || 0)) + "%" : ""}`;
  }

  function renderJobs(list) {
    lastJobs = list;
    $("jobs-empty").hidden = list.length > 0;
    $("jobs").innerHTML = list.map((j) => {
      const busy = j.status === "queued" || j.status === "running";
      return `<li class="job${selected === j.id ? " selected" : ""}" data-id="${esc(j.id)}">
        ${j.inputs.front ? `<button class="thumb" data-act="view" type="button" title="Show"><img class="thumb" src="${esc(j.inputs.front)}" alt=""></button>`
                         : `<div class="noimg">Picture on its way</div>`}
        <div class="body">
          <span class="name" title="${esc(j.title)}">${esc(j.title)}</span>
          <span class="state ${esc(j.status)}">${esc(stateText(j))}</span>
          ${busy ? `<div class="bar"><span style="width:${Math.round((j.progress || 0))}%"></span></div>` : ""}
          ${j.kind === "edit" ? `<span class="kind">Edit${j.keep_shape ? " · colours only" : ""}</span><span class="prompt" title="${esc(j.instruction)}">“${esc(j.instruction)}”</span>`
            : j.kind === "repaint" ? `<span class="kind">Repainted upload</span>` : ""}
          ${j.prompt ? `<span class="prompt" title="${esc(j.prompt)}">“${esc(j.prompt)}”</span>` : ""}
          ${j.rig ? `<span class="rig">Rigged${j.rig.method === "magicarticulate" ? " (MagicArticulate AI)" : ""} · ${esc(j.rig.joints)} bones · ${esc((j.rig.animations || []).join(", "))}</span>` : ""}
          ${j.rig_error ? `<span class="rig bad">${esc(j.rig_error)}</span>` : ""}
          ${j.rig && j.rig.physics && j.rig.physics.length ? `<span class="physics">Physics · ${esc(j.rig.physics.map((p) => p.name).join(", "))}</span>` : ""}
          <span class="muted small">${esc(j.model_label)}${j.settings.paint ? (j.settings.all_sides ? " · painted all sides" : " · painted") : ""} · seed ${esc(j.settings.seed)}</span>
          <div class="actions">
            ${j.status === "done" ? `<button type="button" data-act="view">View</button><a class="button" href="${esc(j.glb)}" download>GLB</a>` : ""}
            ${j.status === "done" && j.inputs.front ? `<button type="button" data-act="edit">Edit</button>` : ""}
            ${j.status === "done" && j.rig && j.rig.method !== "magicarticulate" && j.inputs.front ? `<button type="button" data-act="physics" title="Make loose parts (chainmail, capes, hair) swing">Physics</button>` : ""}
            ${j.status === "done" ? `<button type="button" data-act="rig" title="Add a skeleton and preview animations (standing characters)">${j.rig ? "Re-rig" : "Rig"}</button>` : ""}
            ${!busy ? `<button type="button" data-act="redo">Redo</button>` : ""}
            ${j.status === "error" || j.status === "cancelled" ? `<button type="button" data-act="retry">Retry</button>` : ""}
            <button type="button" data-act="delete">${busy ? "Cancel" : "Delete"}</button>
          </div>
        </div></li>`;
    }).join("");
  }

  // Poly count: read only the GLB's JSON header (two small range requests) and add up the triangles.
  const polyCache = new Map();
  async function polyCount(url) {
    if (polyCache.has(url)) return polyCache.get(url);
    let whole = null;   // a server that ignores Range sends the whole file (200): slice it here instead
    const range = async (from, to) => {
      if (whole) return whole.slice(from, to + 1);
      const r = await fetch(url, { headers: { Range: `bytes=${from}-${to}` }, credentials: "same-origin" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const bytes = new Uint8Array(await r.arrayBuffer());
      if (r.status === 206) return bytes;
      whole = bytes;
      return whole.slice(from, to + 1);
    };
    const head = new DataView((await range(0, 19)).buffer);
    if (head.getUint32(0, true) !== 0x46546C67) throw new Error("not a GLB");
    const jsonLen = head.getUint32(12, true);
    const json = JSON.parse(new TextDecoder().decode(await range(20, 19 + jsonLen)));
    let tris = 0, verts = 0;
    for (const mesh of json.meshes || []) {
      for (const p of mesh.primitives || []) {
        const pos = json.accessors[p.attributes.POSITION];
        verts += pos ? pos.count : 0;
        const mode = p.mode ?? 4;           // 4 = triangles
        if (mode !== 4) continue;
        tris += p.indices != null ? Math.floor(json.accessors[p.indices].count / 3) : Math.floor((pos?.count || 0) / 3);
      }
    }
    const out = { tris, verts };
    polyCache.set(url, out);
    return out;
  }
  const fmt = (n) => n.toLocaleString();

  function show(j, opts = {}) {
    if (!j || j.status !== "done") return;
    selected = j.id;
    const v = $("viewer");
    $("viewer-empty").hidden = true; $("viewer-bar").hidden = false;
    const src = `${j.glb}?v=${Math.round(j.finished || 0)}`;
    $("anim-bar").hidden = true;
    $("paint-toggle").hidden = true;   // shown again once the model loads, if it has paint
    if (saveData() && !loaded.has(j.id) && !opts.load) {
      // Mobile data: the picture and a button instead of a multi-MB download.
      v.hidden = true;
      v.removeAttribute("src");
      $("viewer-tap").hidden = false;
      $("viewer-poster").src = j.inputs.front || "";
      $("viewer-size").textContent = j.size ? `${(j.size / 1048576).toFixed(1)} MB` : "";
      $("viewer-tap").onclick = () => show(j, { load: true });
    } else {
      loaded.add(j.id);
      $("viewer-tap").hidden = true;
      v.hidden = false;
      if (v.getAttribute("src") !== src) v.src = src;
    }
    if (opts.scroll && narrow()) $("viewer-bar").closest(".viewer-panel").scrollIntoView({ behavior: "smooth", block: "start" });
    $("viewer-title").textContent = j.title;
    const meta = j.settings.native_texture
      ? `${j.model_label} · detail ${{128: 1024, 256: 1536, 384: 2048, 512: 2048}[j.settings.octree] || 1536} · PBR textures`
      : `${j.model_label} · detail ${j.settings.octree} · ${j.settings.steps} steps${j.settings.paint ? " · painted" : ""}`;
    $("viewer-meta").textContent = meta;
    const showing = j.id;
    polyCount(src).then(({ tris, verts }) => {
      if (selected === showing) $("viewer-meta").textContent = `${meta} · ${fmt(tris)} triangles · ${fmt(verts)} vertices`;
    }).catch(() => { /* count unavailable: leave the line as it is */ });
    $("viewer-download").href = j.glb;
    renderJobs(lastJobs);
  }

  $("jobs").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const id = btn.closest(".job").dataset.id;
    const j = lastJobs.find((x) => x.id === id);
    const act = btn.dataset.act;
    try {
      if (act === "view") { show(j, { scroll: true }); return; }
      else if (act === "redo") { openRedo(j); return; }
      else if (act === "physics") { openPhysics(j); return; }
      else if (act === "rig") {
        if (Object.keys(rigMethods).length > 1) { openRig(j); return; }
        btn.disabled = true; btn.textContent = "Rigging…";
        await doRig(j, "builtin");
        return;
      }
      else if (act === "edit") { openEdit(j); return; }
      else if (act === "retry") await api(`/api/jobs/${id}/retry`, { method: "POST" });
      else if (act === "delete") {
        const busy = j.status === "queued" || j.status === "running";
        if (!busy && !confirm(`Delete "${j.title}"?`)) return;
        await api(`/api/jobs/${id}`, { method: "DELETE" });
        if (selected === id && !busy) { selected = null; $("viewer").hidden = true; $("viewer-tap").hidden = true; $("viewer-bar").hidden = true; $("viewer-empty").hidden = false; }
      }
      await refresh();
    } catch (err) { alert(err.message); }
  });

  // ---- Character option: a T-pose picture and an automatic skeleton; on by default with all sides painted.
  const CHARACTER_WORDS = /\b(player|character|hero|heroine|npc|villain|knight|warrior|soldier|wizard|mage|person|man|woman|girl|boy|robot|humanoid|elf|orc|goblin|zombie|avatar|mascot)\b/i;
  let characterTouched = false;
  $("character").addEventListener("change", () => {
    characterTouched = true;
    if ($("character").checked) $("all-sides").checked = true;
    $("rig-method-row").hidden = Object.keys(rigMethods).length < 2 || !$("character").checked;
  });
  $("prompt").addEventListener("input", () => {
    if (characterTouched) return;
    const on = CHARACTER_WORDS.test($("prompt").value);
    if (on !== $("character").checked) { $("character").checked = on; if (on) $("all-sides").checked = true; }
    $("rig-method-row").hidden = Object.keys(rigMethods).length < 2 || !$("character").checked;
  });

  // ---- Animation preview (rigged models): <model-viewer> plays the GLB's clips.
  const viewer = $("viewer");
  // Paint on/off: keep each material's colour texture so it can be taken off (bare clay shape) and put back.
  let paintOn = new URLSearchParams(location.search).get("paint") !== "off";   // ?paint=off opens with the bare shape
  let painted = [];   // [{material, texture}]
  function applyPaint() {
    for (const { material, texture } of painted) {
      material.pbrMetallicRoughness.baseColorTexture.setTexture(paintOn ? texture : null);
    }
    const b = $("paint-toggle");
    b.textContent = paintOn ? "Paint: on" : "Paint: off";
    b.setAttribute("aria-pressed", String(paintOn));
    b.title = paintOn ? "Show the bare shape without paint" : "Show the paint again";
  }
  $("paint-toggle").addEventListener("click", () => { paintOn = !paintOn; applyPaint(); });
  viewer.addEventListener("load", () => {
    const orbit = new URLSearchParams(location.search).get("orbit");   // ?orbit=180deg opens looking from behind
    if (orbit && /^-?\d{1,3}(deg)?$/.test(orbit)) {
      viewer.autoRotate = false;
      viewer.cameraOrbit = `${parseInt(orbit, 10)}deg 80deg auto`;
    }
    painted = (viewer.model?.materials || [])
      .map((material) => ({ material, texture: material.pbrMetallicRoughness?.baseColorTexture?.texture }))
      .filter((m) => m.texture);
    $("paint-toggle").hidden = !painted.length;
    if (painted.length && !paintOn) applyPaint();   // the choice carries over to the next model
    const names = viewer.availableAnimations || [];
    $("anim-bar").hidden = !names.length;
    if (!names.length) { viewer.autoRotate = true; return; }
    $("anim").innerHTML = names.map((n) => `<option>${esc(n)}</option>`).join("") + `<option value="">None (bind pose)</option>`;
    const asked = new URLSearchParams(location.search).get("anim");   // ?anim=Walk opens on that clip
    const start = names.includes(asked) ? asked : names.includes("Idle") ? "Idle" : names[0];
    $("anim").value = start;
    viewer.animationName = start;
    viewer.autoRotate = false;
    viewer.play();
    $("anim-play").textContent = "Pause";
  });
  $("anim").addEventListener("change", () => {
    const name = $("anim").value;
    if (!name) { viewer.pause(); viewer.animationName = null; viewer.currentTime = 0; $("anim-play").textContent = "Play"; return; }
    viewer.animationName = name;
    viewer.currentTime = 0;
    viewer.play();
    $("anim-play").textContent = "Pause";
  });
  $("anim-play").addEventListener("click", () => {
    if (viewer.paused) { if (!viewer.animationName && $("anim").options.length) { $("anim").value = $("anim").options[0].value; viewer.animationName = $("anim").value; } viewer.play(); $("anim-play").textContent = "Pause"; }
    else { viewer.pause(); $("anim-play").textContent = "Play"; }
  });

  // ---- Physics dialog: presets + a brush on the front picture
  let physicsOpts = null;
  const brush = { canvas: $("brush-canvas"), ctx: null, drawing: false, erase: false, dirty: false };
  brush.ctx = brush.canvas.getContext("2d");

  async function loadPhysicsOpts() {
    if (!physicsOpts) physicsOpts = await api("/api/physics/options");
    return physicsOpts;
  }
  const materialOptions = (sel) => Object.entries(physicsOpts.materials).map(([k, label]) =>
    `<option value="${esc(k)}"${k === sel ? " selected" : ""}>${esc(label)}</option>`).join("");
  const DEFAULT_MATERIAL = { skirt: "chainmail", cape: "cloth", hair: "hair" };

  function setSlider(id, value) {
    const el = $(id);
    el.dataset.set = value == null ? "" : "1";
    el.value = value == null ? 0.5 : value;
    $(`${id}-v`).textContent = value == null ? "material default" : Number(value).toFixed(2);
  }
  for (const id of ["ph-stiffness", "ph-gravity", "ph-drag"]) {
    $(id).addEventListener("input", () => { $(id).dataset.set = "1"; $(`${id}-v`).textContent = Number($(id).value).toFixed(2); });
  }
  $("ph-reset").addEventListener("click", () => ["ph-stiffness", "ph-gravity", "ph-drag"].forEach((id) => setSlider(id, null)));

  async function openPhysics(j) {
    dialogJob = j;
    await loadPhysicsOpts();
    $("physics-title").textContent = `“${j.title}”`;
    const saved = Object.fromEntries((j.physics || []).map((p) => [p.kind, p]));
    $("physics-presets").innerHTML = Object.entries(physicsOpts.presets).map(([kind, label]) => `
      <div class="ph-row" data-kind="${esc(kind)}">
        <label class="check"><input type="checkbox"${saved[kind] ? " checked" : ""}><span>${esc(label)}</span></label>
        <select aria-label="Material for ${esc(label)}">${materialOptions(saved[kind]?.material || DEFAULT_MATERIAL[kind])}</select>
      </div>`).join("");
    $("brush-material").innerHTML = materialOptions(saved.brush?.material || "chainmail");
    $("brush-on").checked = !!saved.brush;
    $("brush-on").dispatchEvent(new Event("change"));
    const first = (j.physics || [])[0] || {};
    setSlider("ph-stiffness", first.stiffness); setSlider("ph-gravity", first.gravity); setSlider("ph-drag", first.drag);
    $("physics-remove").hidden = !(j.physics && j.physics.length);
    $("physics-msg").textContent = "";
    // Brush canvas at the picture's own resolution; an existing mask is loaded back in.
    const img = $("brush-img");
    img.onload = () => {
      brush.canvas.width = img.naturalWidth; brush.canvas.height = img.naturalHeight;
      brush.ctx.clearRect(0, 0, brush.canvas.width, brush.canvas.height);
      brush.dirty = false;
      if (saved.brush) {
        const m = new Image();
        m.onload = () => {   // mask PNG is white-on-black: draw only the white as the brush colour
          const tmp = document.createElement("canvas");
          tmp.width = brush.canvas.width; tmp.height = brush.canvas.height;
          const t = tmp.getContext("2d");
          t.drawImage(m, 0, 0, tmp.width, tmp.height);
          const d = t.getImageData(0, 0, tmp.width, tmp.height);
          for (let i = 0; i < d.data.length; i += 4) {
            const on = d.data[i] > 127;
            d.data[i] = 231; d.data[i + 1] = 154; d.data[i + 2] = 79; d.data[i + 3] = on ? 255 : 0;
          }
          brush.ctx.putImageData(d, 0, 0);
        };
        m.src = `${P}/files/${j.id}/physics_mask.png?v=${Math.round(j.finished || 0)}`;
      }
    };
    img.src = j.inputs.front;
    $("physics-dialog").showModal();
  }

  $("brush-on").addEventListener("change", () => $("brush-on").closest(".brush-box").classList.toggle("off", !$("brush-on").checked));
  $("brush-erase").addEventListener("click", () => {
    brush.erase = !brush.erase; $("brush-erase").setAttribute("aria-pressed", String(brush.erase));
  });
  $("brush-clear").addEventListener("click", () => { brush.ctx.clearRect(0, 0, brush.canvas.width, brush.canvas.height); brush.dirty = true; });

  function brushAt(e) {
    const r = brush.canvas.getBoundingClientRect();
    const scale = brush.canvas.width / r.width;
    const x = (e.clientX - r.left) * scale, y = (e.clientY - r.top) * scale;
    const radius = Number($("brush-size").value) * scale / 2;
    brush.ctx.globalCompositeOperation = brush.erase ? "destination-out" : "source-over";
    brush.ctx.fillStyle = "rgb(231,154,79)";
    brush.ctx.beginPath(); brush.ctx.arc(x, y, radius, 0, Math.PI * 2); brush.ctx.fill();
    brush.dirty = true;
  }
  brush.canvas.addEventListener("pointerdown", (e) => { brush.drawing = true; brush.canvas.setPointerCapture(e.pointerId); brushAt(e); });
  brush.canvas.addEventListener("pointermove", (e) => { if (brush.drawing) brushAt(e); });
  for (const ev of ["pointerup", "pointercancel"]) brush.canvas.addEventListener(ev, () => { brush.drawing = false; });

  function maskBlob() {
    // White where painted, black elsewhere, at the picture's resolution.
    const out = document.createElement("canvas");
    out.width = brush.canvas.width; out.height = brush.canvas.height;
    const o = out.getContext("2d");
    const d = brush.ctx.getImageData(0, 0, out.width, out.height);
    const m = o.createImageData(out.width, out.height);
    let any = false;
    for (let i = 0; i < d.data.length; i += 4) {
      const v = d.data[i + 3] > 40 ? 255 : 0;
      if (v) any = true;
      m.data[i] = m.data[i + 1] = m.data[i + 2] = v; m.data[i + 3] = 255;
    }
    o.putImageData(m, 0, 0);
    return new Promise((resolve) => out.toBlob((b) => resolve(any ? b : null), "image/png"));
  }

  async function sendPhysics(parts, withMask) {
    const fd = new FormData();
    fd.set("parts", JSON.stringify(parts));
    if (withMask) {
      const blob = await maskBlob();
      if (!blob) throw new Error("Paint over the moving parts first (or untick the brush).");
      fd.set("mask", blob, "mask.png");
    }
    $("physics-msg").className = "msg"; $("physics-msg").textContent = "Simulating… (about 5–20 seconds)";
    const done = await api(`/api/jobs/${dialogJob.id}/physics`, { method: "POST", body: fd });
    $("physics-dialog").close();
    loaded.delete(done.id);
    await refresh();
    show(done, { load: true });
  }

  $("physics-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const tune = {};
    for (const [key, id] of [["stiffness", "ph-stiffness"], ["gravity", "ph-gravity"], ["drag", "ph-drag"]]) {
      if ($(id).dataset.set === "1") tune[key] = Number($(id).value);
    }
    const parts = [...document.querySelectorAll("#physics-presets .ph-row")]
      .filter((row) => row.querySelector("input").checked)
      .map((row) => ({ kind: row.dataset.kind, material: row.querySelector("select").value, ...tune }));
    const useBrush = $("brush-on").checked;
    if (useBrush) parts.push({ kind: "brush", material: $("brush-material").value, ...tune });
    if (!parts.length) { $("physics-msg").className = "msg err"; $("physics-msg").textContent = "Tick a part or use the brush."; return; }
    const hasSavedMask = (dialogJob.physics || []).some((p) => p.kind === "brush");
    try { await sendPhysics(parts, useBrush && (brush.dirty || !hasSavedMask)); }
    catch (err) { $("physics-msg").className = "msg err"; $("physics-msg").textContent = err.message; }
  });
  $("physics-remove").addEventListener("click", async () => {
    try { await sendPhysics([], false); }
    catch (err) { $("physics-msg").className = "msg err"; $("physics-msg").textContent = err.message; }
  });

  // ---- Rig dialog: built-in humanoid or MagicArticulate AI
  async function doRig(j, method) {
    const fd = new FormData();
    fd.set("method", method);
    const done = await api(`/api/jobs/${j.id}/rig`, { method: "POST", body: fd });
    loaded.delete(j.id);
    await refresh();
    if (done.status === "done") show(done, { load: true });
  }
  function openRig(j) {
    dialogJob = j;
    $("rig-title").textContent = `“${j.title}”`;
    const current = j.rig_method || "builtin";
    $("rig-choices").innerHTML = Object.entries(rigMethods).map(([k, label]) =>
      `<label class="check"><input type="radio" name="method" value="${esc(k)}"${k === current ? " checked" : ""}><span>${esc(label)}</span></label>`).join("");
    $("rig-msg").textContent = "";
    $("rig-dialog").showModal();
  }
  $("rig-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const method = new FormData(e.target).get("method") || "builtin";
    $("rig-msg").className = "msg"; $("rig-msg").textContent = method === "magicarticulate" ? "Queued for a GPU turn…" : "Rigging…";
    try { await doRig(dialogJob, method); $("rig-dialog").close(); }
    catch (err) { $("rig-msg").className = "msg err"; $("rig-msg").textContent = err.message; }
  });

  // ---- Redo / Edit dialogs
  let dialogJob = null;
  document.querySelectorAll("dialog [data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));

  function openRedo(j) {
    dialogJob = j;
    const f = $("redo-form");
    $("redo-title").textContent = `“${j.title}”`;
    f.elements.new_seed.checked = true;
    const drawn = !!j.prompt || j.kind === "edit";
    $("redo-picture-row").hidden = !drawn;
    f.elements.new_picture.checked = false;
    $("redo-paint-row").hidden = !!j.mesh;
    f.elements.paint.checked = !!j.settings.paint;
    f.elements.octree.value = String(j.settings.octree);
    f.elements.steps.value = j.settings.steps;
    f.elements.texture.value = String(j.settings.texture || 2048);
    $("redo-msg").textContent = "";
    $("redo-dialog").showModal();
  }

  $("redo-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, fd = new FormData();
    for (const name of ["new_seed", "new_picture", "paint"]) fd.set(name, f.elements[name].checked ? "true" : "false");
    for (const name of ["octree", "steps", "texture"]) fd.set(name, f.elements[name].value);
    if (dialogJob.mesh) fd.delete("paint");
    try {
      await api(`/api/jobs/${dialogJob.id}/redo`, { method: "POST", body: fd });
      $("redo-dialog").close();
      await refresh();
    } catch (err) { $("redo-msg").textContent = err.message; $("redo-msg").className = "msg err"; }
  });

  function openEdit(j) {
    dialogJob = j;
    $("edit-title").textContent = `“${j.title}”`;
    $("edit-picture").src = j.inputs.front;
    $("edit-form").reset();
    $("edit-msg").textContent = "";
    $("edit-dialog").showModal();
  }

  $("edit-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    if (!String(fd.get("instruction") || "").trim()) return;
    try {
      await api(`/api/jobs/${dialogJob.id}/edit`, { method: "POST", body: fd });
      $("edit-dialog").close();
      await refresh();
    } catch (err) { $("edit-msg").textContent = err.message; $("edit-msg").className = "msg err"; }
  });

  let timer = null;
  async function refresh() {
    clearTimeout(timer);
    try {
      const jobs = await api("/api/jobs");
      const before = new Map(lastJobs.map((j) => [j.id, j.status]));
      renderJobs(jobs);
      const fresh = jobs.find((j) => j.status === "done" && before.get(j.id) && before.get(j.id) !== "done");
      const wanted = !selected && new URLSearchParams(location.search).get("job");
      if (fresh) show(fresh);
      else if (!selected) show(jobs.find((j) => j.id === wanted && j.status === "done") || jobs.find((j) => j.status === "done"));
    } catch { /* keep polling */ }
    const busy = lastJobs.some((j) => j.status === "queued" || j.status === "running");
    if (!document.hidden) timer = setTimeout(refresh, busy ? 2000 : 10000);
  }
  // A phone with the page in the background stops polling; it catches up when it's back.
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); else clearTimeout(timer); });

  loadModels();
  refresh();
})();
