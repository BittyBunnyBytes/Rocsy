(() => {
  const base = window.TRAINER_BASE || "/Trainer";
  const $ = (selector) => document.querySelector(selector);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const stateLabel = {waiting_for_gpu:"Queued", preparing:"Preparing", running:"Training", cancelling:"Stopping", complete:"Complete", failed:"Failed", cancelled:"Cancelled"};
  const ACTIVE = new Set(["waiting_for_gpu", "preparing", "running", "cancelling"]);
  // Families come from /api/families: header-detected base models, per-slot file candidates and
  // what's missing. Fields are shown per family through data-knob attributes.
  const SETTING_FIELDS = ["rank", "epochs", "resolution", "frames", "steps", "max_pixels", "repeats", "learning_rate"];
  const NUMERIC = new Set(["rank", "epochs", "resolution", "frames", "steps", "max_pixels", "repeats"]);
  const STATE_GROUPS = [["ready", "Ready to train"], ["needs_files", "Installed, needs a training file"], ["needs_trainer", "Needs its trainer installed"], ["not_installed", "Not installed yet (download the base model)"]];
  let familyData = {families: [], unrecognised: []}, presets = {builtin:{}, user:{}};
  const family = (id = $("#model").value) => familyData.families.find((f) => f.id === id);
  const slotKeys = () => (family()?.slots || []).map((s) => s.key);
  const presetFields = () => [...SETTING_FIELDS, ...slotKeys()];
  const openState = new Map();     // job id -> {progress, details, console}
  const consoleScroll = new Map(); // job id -> {top, atBottom}

  function message(text, bad=false) {
    const m = $("#form-message"); m.textContent = text || ""; m.style.color = bad ? "var(--red)" : "var(--amber)";
  }
  async function api(path, options={}) {
    const response = await fetch(base + path, {credentials:"same-origin", headers:{"Content-Type":"application/json", ...(options.headers||{})}, ...options});
    let body = {};
    try { body = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(body.detail || body.message || body.error || `Request failed (${response.status})`);
    return body;
  }
  const field = (name) => $("#job-form").elements.namedItem(name);
  let lastChecked = "";

  // ---- model file selectors (one per slot of the chosen family) ----
  const VERDICT = {ready:"", untested:" · untested", unsupported:" · can't train"};
  function renderSlots() {
    const host = $("#model-files"), fam = family(), keep = {};
    host.querySelectorAll("select").forEach((s) => { keep[s.name] = s.value; });
    host.replaceChildren();
    if (!fam) return;
    for (const slot of fam.slots) {
      const label = el("label", "file-field");
      label.append(document.createTextNode(slot.label + " "));
      if (!slot.required) label.append(el("span", "muted", "(optional)"));
      const select = el("select"); select.name = slot.key; select.dataset.preset = ""; select.dataset.slot = slot.key;
      if (!slot.required) select.dataset.optional = "";
      const usable = slot.candidates.filter((c) => c.verdict !== "unsupported");
      select.append(new Option(!slot.required ? "None" : usable.length ? "Choose a file…" : "No usable file installed", ""));
      const group = (title, list, disabled) => {
        if (!list.length) return;
        const g = document.createElement("optgroup"); g.label = title;
        for (const c of list) {
          const o = new Option(c.file + (VERDICT[c.verdict] || ""), c.file);
          o.disabled = disabled; o.title = c.reason || "";
          g.append(o);
        }
        select.append(g);
      };
      group(slot.base ? `Detected ${fam.title} models` : "Suggested", slot.candidates.filter((c) => c.verdict === "ready"), false);
      group("Untested", slot.candidates.filter((c) => c.verdict === "untested"), false);
      group("Can't train with these", slot.candidates.filter((c) => c.verdict === "unsupported"), true);
      const prior = keep[slot.key];
      const firstReady = slot.candidates.find((c) => c.verdict === "ready") || (slot.required ? usable[0] : null);
      select.value = prior && [...select.options].some((o) => o.value === prior && !o.disabled) ? prior : (firstReady?.file || "");
      const why = el("small", "slot-why");
      const explain = () => { const c = slot.candidates.find((x) => x.file === select.value); why.textContent = c?.reason || slot.note || ""; why.hidden = !why.textContent; };
      select.addEventListener("change", () => { explain(); presetEdited(); });
      explain();
      label.append(select, why);
      host.append(label);
    }
  }

  // ---- presets ----
  const presetKey = (p) => (p.builtin ? "b:" : "u:") + p.name;
  function presetList(model) {
    const out = [];
    for (const [name, settings] of Object.entries(presets.builtin?.[model] || {})) out.push({name, builtin:true, settings});
    for (const [name, settings] of Object.entries(presets.user?.[model] || {})) out.push({name, builtin:false, settings});
    return out;
  }
  function presetSettings(value) {
    if (!value) return null;
    const store = value.startsWith("b:") ? presets.builtin : presets.user;
    return store?.[$("#model").value]?.[value.slice(2)] || null;
  }
  function fillPresetSelect(keep) {
    const select = $("#preset"), current = keep ?? select.value;
    select.replaceChildren(new Option("Custom", ""));
    for (const p of presetList($("#model").value)) select.append(new Option((p.builtin ? "★ " : "") + p.name, presetKey(p)));
    select.value = [...select.options].some((o) => o.value === current) ? current : "";
    $("#preset-delete").disabled = !select.value.startsWith("u:");
  }
  function applyValues(settings) {
    for (const key of presetFields()) {
      const input = field(key);
      if (!input || !(key in settings)) continue;
      const v = String(settings[key] ?? "");
      if (input.tagName === "SELECT" && v && ![...input.options].some((o) => o.value === v)) input.append(new Option(`${v} (missing)`, v));
      input.value = v;
    }
  }
  function applyPreset(value) {
    const settings = presetSettings(value);
    if (!settings) return;
    applyValues(settings);
    try { localStorage.setItem(`trainer-preset-${$("#model").value}`, value); } catch (_) {}
  }
  function currentSettings() {
    const out = {};
    for (const key of presetFields()) {
      const input = field(key); if (!input) continue;
      if (!slotKeys().includes(key) && !(family()?.knobs || []).includes(key)) continue;
      out[key] = NUMERIC.has(key) ? Number(input.value) : input.value;
    }
    return out;
  }
  function selectDefaultPreset() {
    const model = $("#model").value;
    let saved = ""; try { saved = localStorage.getItem(`trainer-preset-${model}`) || ""; } catch (_) {}
    const keys = presetList(model).map(presetKey);
    const pick = keys.includes(saved) ? saved : (keys[0] || "");
    applyValues(family()?.defaults || {});  // recipe defaults first; a preset then overrides them
    fillPresetSelect(pick);
    applyPreset(pick);
  }

  function setModelFields() {
    const fam = family();
    const knobs = new Set(fam?.knobs || []);
    document.querySelectorAll("[data-knob]").forEach((n) => { n.hidden = !knobs.has(n.dataset.knob); });
    const lr = field("learning_rate"); if (lr) lr.placeholder = fam?.defaults?.learning_rate ? `recipe: ${fam.defaults.learning_rate}` : "recipe";
    $("#model-hint").textContent = fam ? fam.hint : "";
    renderSlots();
    renderFamilyStatus();
    selectDefaultPreset();
    $("#submit-job").disabled = !fam || fam.state !== "ready";
  }

  // What the chosen family still needs, with one-click downloads through the Models hub.
  function renderFamilyStatus() {
    const box = $("#family-status"), fam = family();
    box.replaceChildren();
    if (!fam) { box.hidden = true; return; }
    box.className = `family-status ${fam.state}`;
    const caveat = fam.state === "ready" && fam.untested_only?.length;
    if (caveat) box.classList.add("caveat");
    const head = caveat ? "⚠ Can train, but only with untested files" : {ready:"✓ Ready to train", needs_files:"Needs a training file", needs_trainer:`Needs ${fam.backend_title} installed`, not_installed:"Base model not installed"}[fam.state];
    box.append(el("strong", null, head), el("span", "muted", ` · trains with ${fam.backend_title}`));
    if (fam.state === "needs_trainer") box.append(el("p", null, "Install it in Trainer environments above, then refresh."));
    for (const slot of fam.slots.filter((s) => s.missing || s.untested_only)) {
      const row = el("div", "missing-row");
      const bad = slot.candidates.filter((c) => c.verdict === "unsupported");
      row.append(el("span", null, slot.missing ? `Missing: ${slot.label}` : `Recommended: the tested ${slot.label}`));
      if (bad.length) row.append(el("small", "muted", ` (installed ${bad.map((c) => c.file.split("/").pop()).join(", ")} can't be trained on: ${bad[0].reason})`));
      if (slot.get) {
        const page = `https://huggingface.co/${slot.get.repo}` + (slot.get.path ? `/blob/main/${slot.get.path}` : "");
        if (!slot.get.manual) {
          const b = el("button", "button secondary small", `⬇ Download${slot.get.gb ? ` (~${slot.get.gb} GB)` : ""}`); b.type = "button";
          b.addEventListener("click", async () => {
            b.disabled = true; b.textContent = "Starting…";
            try { const r = await api(`/api/families/${encodeURIComponent(fam.id)}/get/${slot.key}`, {method:"POST", body:"{}"}); b.textContent = `Downloading to ${r.dest}/ — see Models → Downloads`; }
            catch (error) { b.disabled = false; b.textContent = "⬇ Download"; message(error.message, true); }
          });
          row.append(b);
        }
        const a = el("a", "small-link", slot.get.manual ? "Get it on Hugging Face ↗" : "Hugging Face ↗"); a.href = page; a.target = "_blank"; a.rel = "noopener";
        row.append(a);
        if (slot.get.save_as) row.append(el("small", "muted", ` save as models/${slot.get.dest}/${slot.get.save_as}`));
      } else {
        row.append(el("small", "muted", ` put it in models/${slot.folders[0]}/ and press Refresh`));
      }
      box.append(row);
    }
    box.hidden = false;
  }

  function fillFamilySelect() {
    const select = $("#model"), current = select.value || (() => { try { return localStorage.getItem("trainer-model") || ""; } catch (_) { return ""; } })();
    select.replaceChildren();
    const fams = familyData.families;
    for (const [state, label] of STATE_GROUPS) {
      const list = fams.filter((f) => f.state === state && !f.recipe);
      if (!list.length) continue;
      const g = document.createElement("optgroup"); g.label = label;
      for (const f of list) g.append(new Option(`${f.title}${f.group === "Video" ? " (video)" : ""}`, f.id));
      select.append(g);
    }
    const recipes = fams.filter((f) => f.recipe);
    if (recipes.length) {
      const g = document.createElement("optgroup"); g.label = familyData.ai_toolkit ? "AI Toolkit recipes (base model from Hugging Face)" : "AI Toolkit recipes (install AI Toolkit first)";
      for (const f of recipes) g.append(new Option(f.title.replace(" (AI Toolkit)", ""), f.id));
      select.append(g);
    }
    select.value = fams.some((f) => f.id === current) ? current : (fams.find((f) => f.state === "ready")?.id || fams[0]?.id || "");
  }
  async function refreshFamilies() {
    try { familyData = await api("/api/families"); }
    catch (error) { message(`Could not check installed models: ${error.message}`, true); return; }
    fillFamilySelect();
    setModelFields();
    const others = familyData.unrecognised || [], box = $("#other-models");
    box.hidden = !others.length;
    box.querySelector("summary").textContent = `${others.length} other model file${others.length === 1 ? "" : "s"} found that nothing here can train`;
    box.querySelector("ul").replaceChildren(...others.map((o) => { const li = el("li"); li.append(el("code", null, o.file), document.createTextNode(` ${o.variant ? o.variant + ": " : ""}${o.reason}`)); return li; }));
  }

  function renderBackends(status) {
    const host = $("#backend-status"); host.replaceChildren();
    for (const [key, backend] of Object.entries(status.backends || {})) {
      const card = el("div", "backend-card");
      card.append(el("strong", null, backend.title), el("span", `state-${backend.state}`, backend.state.replaceAll("_", " ")), el("small", "muted", backend.covers));
      if (backend.state === "not_installed" && backend.install) {
        const b = el("button", "button secondary small setup-button", `Install ${backend.title}`); b.type = "button"; b.dataset.backend = backend.install;
        b.addEventListener("click", () => runSetup(b));
        card.append(b);
      } else if (backend.note) card.append(el("small", "muted", backend.note));
      host.append(card);
    }
    const select = $("#dataset"), current = select.value; select.replaceChildren();
    select.append(new Option(status.datasets?.length ? "Select a dataset folder" : "No dataset folders found", ""));
    for (const dataset of status.datasets || []) select.append(new Option(dataset, dataset));
    if ([...select.options].some((option) => option.value === current)) select.value = current;
    if (select.value && select.value !== lastChecked) { lastChecked = select.value; scheduleCheck(0); }
    for (const [key, item] of Object.entries(status.setups || {})) {
      if (item.state === "installing") message(`${key} environment installation is running. This can take a while.`);
    }
  }
  async function refreshStatus() {
    try { renderBackends(await api("/api/status")); }
    catch (error) { $("#backend-status").textContent = error.message; }
  }

  // ---- job cards ----
  const pct = (f) => `${Math.round((f || 0) * 100)}%`;
  function duration(sec) {
    if (sec == null) return "";
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
    return h ? `${h} h ${m} min` : m ? `${m} min ${s} s` : `${s} s`;
  }
  function bar(fraction, text, cls="") {
    const wrap = el("div", `pbar ${cls}`);
    const fill = el("div", "pbar-fill"); fill.style.width = pct(fraction);
    wrap.append(fill, el("span", "pbar-text", text));
    return wrap;
  }
  function section(job, key, title, summary, fill, defaultOpen) {
    const d = el("details", `job-box box-${key}`);
    const st = openState.get(job.id) || {};
    d.open = key in st ? st[key] : defaultOpen;
    const s = el("summary"); s.append(el("span", "box-title", title));
    if (summary) s.append(el("span", "box-summary", summary));
    d.append(s);
    d.addEventListener("toggle", () => { openState.set(job.id, {...(openState.get(job.id) || {}), [key]: d.open}); });
    fill(d);
    return d;
  }
  function progressBox(job, box) {
    const p = job.progress || {};
    const body = el("div", "box-body");
    if (p.phase === "caching_latents" && p.cache_total) {
      body.append(bar(p.cache_done / p.cache_total, `Encoding clips · ${p.cache_done}/${p.cache_total}`, "phase"));
    }
    const stageNo = {starting:1, caching_latents:1, caching_text:2, loading:3, training:4}[p.phase];
    let overallText = `Overall ${pct(p.overall)} · ${stageNo ? `stage ${stageNo} of 4: ` : ""}${p.phase_label || job.stage}`;
    if (p.steps_total) {
      overallText = `Overall ${pct(p.overall)} · step ${p.step}/${p.steps_total} · epoch ${p.epoch || 0}/${p.epochs_total || "?"}`;
      if (p.sec_per_step) overallText += ` · ${p.sec_per_step.toFixed(1)} s/step`;
      if (p.eta_seconds != null && ACTIVE.has(job.state)) overallText += ` · ${duration(p.eta_seconds)} left`;
    }
    body.append(bar(p.overall, overallText, `overall ${job.state}`));
    const facts = [];
    if (p.step_mix) facts.push(p.steps_per_epoch ? `Epoch type: ${p.steps_per_epoch} steps = ${p.step_mix} steps` : `Dataset: ${p.step_mix} item(s)`);
    if (p.loss != null) facts.push(`avg loss ${p.loss.toFixed(4)}`);
    if (p.elapsed_seconds != null) facts.push(`training time ${duration(p.elapsed_seconds)}`);
    if (facts.length) body.append(el("p", "box-note", facts.join(" · ")));
    if (p.epochs?.length) {
      const list = el("div", "epoch-list");
      for (const e of p.epochs) {
        const row = el("div", `epoch-row ${e.status}`);
        row.append(el("span", "epoch-label", `Epoch ${e.epoch}`));
        let text = e.status === "running" ? `${p.epoch_step}/${p.steps_per_epoch} steps`
          : e.status === "done" ? `${p.steps_per_epoch || ""} steps` : "waiting";
        if (e.loss != null) text += ` · loss ${e.loss.toFixed(4)}`;
        if (e.checkpoint) text += " · saved";
        row.append(bar(e.fraction, text, e.status));
        list.append(row);
      }
      body.append(list);
    }
    box.append(body);
  }
  function detailsBox(job, box) {
    const body = el("div", "box-body");
    const r = job.request || {};
    const rows = [["Dataset", job.dataset], ["Rank", r.rank], ["Epochs", r.epochs]];
    const knobs = family(job.model)?.knobs || [];
    if (knobs.includes("resolution") || job.model === "minimax_h3" || job.model === "ltx25") rows.push(["Resolution", r.resolution]);
    if (r.learning_rate) rows.push(["Learning rate", r.learning_rate]);
    if (r.trigger) rows.push(["Trigger", r.trigger]);
    const shown = new Set();
    for (const k of ["dit", "vae", "text_encoder", "audio_vae", "training_adapter"]) if (r[k]) { rows.push([k.replaceAll("_", " "), r[k]]); shown.add(k); }
    for (const [k, v] of Object.entries(r.files || {})) if (v && !shown.has(k)) rows.push([k.replaceAll("_", " "), v]);
    rows.push(["Created", new Date(job.created_at).toLocaleString()]);
    if (job.completed_at) rows.push(["Finished", new Date(job.completed_at).toLocaleString()]);
    if (job.error) rows.push(["Error", job.error]);
    const dl = el("dl", "kv");
    for (const [k, v] of rows) dl.append(el("dt", null, k), el("dd", null, String(v)));
    body.append(dl);
    if (job.outputs?.length) {
      body.append(el("p", "box-note", `LoRA files (${job.outputs.length}), on the host in ${job.outputs[0].host_path.replace(/[^/]+$/, "")}`));
      const ul = el("ul", "outputs");
      for (const o of job.outputs) ul.append(el("li", null, `${o.name} · ${o.size_mb} MB`));
      body.append(ul);
    }
    box.append(body);
  }
  function consoleBox(job, box) {
    // Carriage-return progress updates overwrite their line, like a terminal does.
    const text = (job.log || "").split("\n").map((line) => { const parts = line.split("\r").filter((x) => x.trim()); return parts.length ? parts[parts.length - 1] : ""; }).join("\n");
    const log = el("pre", "log"); log.textContent = text || "Waiting for trainer output…";
    box.append(log);
    const saved = consoleScroll.get(job.id);
    requestAnimationFrame(() => { log.scrollTop = !saved || saved.atBottom ? log.scrollHeight : saved.top; });
    log.addEventListener("scroll", () => consoleScroll.set(job.id, {top:log.scrollTop, atBottom:log.scrollTop + log.clientHeight >= log.scrollHeight - 8}));
  }
  function renderJobs(jobs) {
    const host = $("#jobs"), keepScroll = host.scrollTop; host.replaceChildren();
    if (!jobs.length) { host.append(el("p", "muted", "No training runs yet.")); return; }
    for (const job of jobs) {
      const card = el("article", "job-card"); card.dataset.jobId = job.id;
      const top = el("div", "job-top");
      top.append(el("h3", null, `${job.model_title}: ${job.name}`), el("span", `badge ${job.state}`, stateLabel[job.state] || job.state));
      const p = job.progress || {};
      card.append(top, el("p", "job-meta", `Dataset: ${job.dataset} · ${p.phase_label || job.stage} · ${new Date(job.created_at).toLocaleString()}`));
      const mini = el("div", "mini-bar"), mf = el("div", `mini-fill ${job.state}`); mf.style.width = pct(p.overall); mini.append(mf); card.append(mini);
      const controls = el("div", "job-controls");
      if (ACTIVE.has(job.state)) {
        const cancel = el("button", "button secondary", "Cancel");
        cancel.addEventListener("click", async () => { cancel.disabled = true; try { await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, {method:"POST"}); await refreshJobs(); } catch (error) { cancel.disabled = false; message(error.message, true); } });
        controls.append(cancel);
      } else {
        const video = ["minimax_h3", "ltx25"].includes(job.model);
        const image = ["krea2", "qwen_image_21", "moss_image", "anima"].includes(job.model);
        if (job.state === "complete" && (video || image) && job.outputs?.length) {
          const published = job.stage === "Complete · In image gallery";
          const publish = el("button", "button secondary", image ? (published ? "Publish again" : "Publish to Image studio") : "Publish to gallery");
          if (image) publish.title = "Add the final LoRA to the Image studio's LoRA gallery (Trainer made)";
          publish.addEventListener("click", async () => {
            publish.disabled = true;
            try { await api(`/api/jobs/${encodeURIComponent(job.id)}/publish-lora`, {method:"POST"}); await refreshJobs(); message(image ? "Final LoRA added to the Image studio's LoRA gallery." : "Final LoRA added to the video gallery."); }
            catch (error) { publish.disabled = false; message(error.message, true); }
          });
          controls.append(publish);
        }
        const remove = el("button", "button secondary", "Remove");
        remove.title = "Remove this job from the history";
        remove.addEventListener("click", async () => {
          if (!confirm(`Remove "${job.name}" (${stateLabel[job.state] || job.state}) from the job history?`)) return;
          let deleteFiles = false;
          if (job.outputs?.length) deleteFiles = confirm(`Also DELETE its ${job.outputs.length} LoRA file(s) from disk?\n\nOK = delete the files too\nCancel = keep the files (only the history entry goes)`);
          remove.disabled = true;
          try { await api(`/api/jobs/${encodeURIComponent(job.id)}?delete_files=${deleteFiles}`, {method:"DELETE"}); openState.delete(job.id); consoleScroll.delete(job.id); await refreshJobs(); }
          catch (error) { remove.disabled = false; message(error.message, true); }
        });
        controls.append(remove);
      }
      card.append(controls);
      const progSummary = p.steps_total ? `${pct(p.overall)} · epoch ${p.epoch}/${p.epochs_total}` : pct(p.overall);
      card.append(
        section(job, "progress", "Progress", progSummary, (b) => progressBox(job, b), ACTIVE.has(job.state)),
        section(job, "details", "Run details", job.outputs?.length ? `${job.outputs.length} LoRA file(s)` : "", (b) => detailsBox(job, b), false),
        section(job, "console", "Console log", "", (b) => consoleBox(job, b), false),
      );
      host.append(card);
    }
    host.scrollTop = keepScroll;
  }
  async function refreshJobs() {
    try { const jobs = (await api("/api/jobs")).jobs || []; renderJobs(jobs); window.trainerAlerts?.jobs(jobs); }
    catch (error) { $("#jobs").textContent = error.message; }
  }

  // ---- events ----
  $("#model").addEventListener("change", () => { try { localStorage.setItem("trainer-model", $("#model").value); } catch (_) {} setModelFields(); });
  $("#preset").addEventListener("change", (e) => { applyPreset(e.target.value); $("#preset-delete").disabled = !e.target.value.startsWith("u:"); });
  // Editing any preset-controlled field after picking a preset flips the selector to "Custom".
  function presetEdited() {
    const sel = $("#preset"), settings = presetSettings(sel.value);
    if (!settings) return;
    const cur = currentSettings();
    if (presetFields().some((k) => k in settings && k in cur && String(settings[k] ?? "") !== String(cur[k] ?? ""))) { sel.value = ""; $("#preset-delete").disabled = true; }
  }
  document.querySelectorAll("#job-form [data-preset]").forEach((n) => n.addEventListener("change", presetEdited));
  $("#preset-save").addEventListener("click", async () => {
    const model = $("#model").value, sel = $("#preset");
    const name = (prompt("Save these settings as a preset named:", sel.value.startsWith("u:") ? sel.value.slice(2) : "") || "").trim();
    if (!name) return;
    try {
      presets = await api("/api/presets", {method:"PUT", body:JSON.stringify({model, name, settings:currentSettings()})});
      fillPresetSelect("u:" + name); message(`Preset "${name}" saved.`);
      try { localStorage.setItem(`trainer-preset-${model}`, "u:" + name); } catch (_) {}
    } catch (error) { message(error.message, true); }
  });
  $("#preset-delete").addEventListener("click", async () => {
    const model = $("#model").value, value = $("#preset").value;
    if (!value.startsWith("u:") || !confirm(`Delete preset "${value.slice(2)}"?`)) return;
    try { presets = await api(`/api/presets?model=${encodeURIComponent(model)}&name=${encodeURIComponent(value.slice(2))}`, {method:"DELETE"}); fillPresetSelect(""); message("Preset deleted."); }
    catch (error) { message(error.message, true); }
  });
  // What a run would actually train on: uncaptioned / off-spec files are left out, not fatal.
  let checkTimer = 0, checkSeq = 0, autoTrigger = "";
  function scheduleCheck(delay = 350) { clearTimeout(checkTimer); checkTimer = setTimeout(runCheck, delay); }
  async function runCheck() {
    const box = $("#train-check"), ds = $("#dataset").value, model = $("#model").value;
    if (!ds || !model) { box.hidden = true; return; }
    const seq = ++checkSeq;
    let r;
    try { r = await api(`/api/datasets/${encodeURIComponent(ds)}/training-check?model=${model}&trigger=${encodeURIComponent(field("trigger").value || "")}`); }
    catch (error) { if (seq === checkSeq) { box.hidden = false; box.className = "train-check bad"; box.textContent = error.message; } return; }
    if (seq !== checkSeq) return;
    // Fill the trigger word detected from the captions unless the user typed their own.
    const trig = field("trigger");
    if (r.trigger_guess && (!trig.value.trim() || trig.value === autoTrigger) && trig.value !== r.trigger_guess) {
      trig.value = autoTrigger = r.trigger_guess; scheduleCheck(0); return;
    }
    const c = r.counts, parts = [];
    if (c.video) parts.push(`${c.video} clip${c.video === 1 ? "" : "s"}`);
    if (c.image) parts.push(`${c.image} image${c.image === 1 ? "" : "s"}`);
    if (c.audio) parts.push(`${c.audio} audio`);
    box.replaceChildren();
    box.className = "train-check " + (!r.included ? "bad" : r.problems.length || r.warnings.length ? "warn" : "ok");
    box.append(el("strong", null, r.included ? `✓ Will train on ${parts.join(", ")}` : "✗ Nothing here is ready to train yet"));
    const skipped = [];
    if (r.uncaptioned) skipped.push(`${r.uncaptioned} without a caption`);
    if (r.problems.length) skipped.push(`${r.problems.length} with a problem`);
    if (skipped.length) box.append(el("span", null, ` · left out: ${skipped.join(", ")} (they stay in the dataset)`));
    for (const w of r.warnings) box.append(el("p", null, "⚠ " + w));
    if (r.excluded.length) {
      const d = el("details"); d.append(el("summary", null, `Show the ${r.excluded.length} left-out file${r.excluded.length === 1 ? "" : "s"}`));
      const ul = el("ul");
      for (const e of r.excluded) { const li = el("li"); li.append(el("code", null, e.file), document.createTextNode(` ${e.reason}`)); ul.append(li); }
      d.append(ul); box.append(d);
    }
    box.hidden = false;
  }
  $("#dataset").addEventListener("change", () => {
    lastChecked = $("#dataset").value;
    if (field("trigger").value === autoTrigger) field("trigger").value = autoTrigger = "";
    scheduleCheck(0);
  });
  $("#model").addEventListener("change", () => scheduleCheck(0));
  field("trigger").addEventListener("input", () => scheduleCheck());
  document.addEventListener("datasets-changed", () => scheduleCheck());
  document.addEventListener("train-tab-shown", () => scheduleCheck(0));
  $("#refresh-status").addEventListener("click", () => { refreshStatus(); refreshFamilies(); });
  document.addEventListener("datasets-changed", refreshStatus);
  $("#refresh-jobs").addEventListener("click", refreshJobs);
  async function runSetup(button) {
    button.disabled = true; $("#setup-log").hidden = false; $("#setup-log").textContent = `Starting ${button.textContent}…`;
    try { await api(`/api/setup/${button.dataset.backend}`, {method:"POST", body:"{}"}); $("#setup-log").textContent = "Installation started. Keep this page open; status will update as it runs."; }
    catch (error) { $("#setup-log").textContent = error.message; }
    finally { setTimeout(() => { button.disabled = false; refreshStatus(); refreshFamilies(); }, 5000); }
  }
  $("#job-form").addEventListener("submit", async (event) => {
    event.preventDefault(); message("");
    const form = new FormData(event.currentTarget), model = form.get("model"), fam = family(model);
    const files = {};
    for (const key of slotKeys()) files[key] = form.get(key) || "";
    const payload = {
      model, dataset:form.get("dataset"), name:form.get("name"), rank:Number(form.get("rank")), epochs:Number(form.get("epochs")),
      max_pixels:Number(form.get("max_pixels") || 655360), steps:Number(form.get("steps") || 2000),
      resolution:Number(form.get("resolution") || 512), frames:Number(form.get("frames") || 25),
      repeats:Number(form.get("repeats") || 1), learning_rate:(fam?.knobs.includes("learning_rate") ? form.get("learning_rate") : "") || "",
      dit:files.dit || "", vae:files.vae || "", text_encoder:files.text_encoder || "", audio_vae:files.audio_vae || "",
      training_adapter:files.training_adapter || "", files, trigger:form.get("trigger") || ""
    };
    const button = $("#submit-job"); button.disabled = true; button.textContent = "Queueing…";
    try {
      await api("/api/jobs", {method:"POST", body:JSON.stringify(payload)});
      message("Training job added to the shared GPU queue. The job log lists what was left out.");
      field("name").value = ""; field("trigger").value = "";
      await Promise.all([refreshJobs(), refreshStatus()]);
    } catch (error) { message(error.message, true); }
    finally { button.disabled = family()?.state !== "ready"; button.textContent = "Queue training run"; }
  });

  (async () => {
    try { presets = await api("/api/presets"); }
    catch (error) { message(`Could not load presets: ${error.message}`, true); }
    await refreshFamilies(); refreshStatus(); refreshJobs();
    // /api/jobs carries every job's full log (~90 KB): poll it every 5 s on the Train tab,
    // every 15 s elsewhere (alerts still fire), and not at all while the tab is hidden.
    let tick = 0;
    setInterval(() => {
      if (document.hidden) return;
      tick += 1;
      if (document.body.classList.contains("on-datasets") && tick % 3) return;
      refreshJobs(); refreshStatus();
    }, 5000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) { refreshJobs(); refreshStatus(); } });
  })();
})();
