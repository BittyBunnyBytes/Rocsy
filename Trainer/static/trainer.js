(() => {
  const base = window.TRAINER_BASE || document.body?.dataset.base || "/Trainer";
  const $ = (selector) => document.querySelector(selector);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const stateLabel = {waiting_for_gpu:"Queued", preparing:"Preparing", running:"Training", cancelling:"Stopping", complete:"Complete", failed:"Failed", cancelled:"Cancelled"};
  const ACTIVE = new Set(["waiting_for_gpu", "preparing", "running", "cancelling"]);
  // Families come from /api/families: header-detected base models, per-slot file candidates and
  // what's missing. Fields are shown per family through data-knob attributes.
  const SETTING_FIELDS = ["kind", "rotations", "save_every", "rank", "epochs", "resolution", "frames", "steps", "max_pixels", "repeats", "learning_rate", "lr_schedule", "base_model", "max_length", "quant", "max_seconds"];
  const NUMERIC = new Set(["rank", "epochs", "resolution", "frames", "steps", "max_pixels", "repeats", "max_length", "rotations", "save_every", "max_seconds"]);
  const STATE_GROUPS = [["ready", "Ready to train"], ["needs_files", "Installed, needs a training file"], ["needs_trainer", "Needs its trainer installed"], ["not_installed", "Not installed yet (download the base model)"]];
  let familyData = {families: [], unrecognised: []}, presets = {builtin:{}, user:{}};
  const family = (id = $("#model").value) => familyData.families.find((f) => f.id === id);
  const slotKeys = () => (family()?.slots || []).map((s) => s.key);
  const presetFields = () => [...SETTING_FIELDS, ...slotKeys()];
  const openState = new Map();     // job id -> {progress, details, console}
  const consoleScroll = new Map(); // job id -> {top, atBottom}
  const llamaPicks = new Map();    // job id -> "Use in Llama" result {file, fits, models}

  function message(text, bad=false) {
    const m = $("#form-message"); m.textContent = text || ""; m.style.color = bad ? "var(--red)" : "var(--amber)";
  }
  async function api(path, options={}) {
    // `timeout` (ms) aborts a request that hangs, e.g. behind a browser connection pool full of image downloads.
    const {timeout, ...rest} = options, ctl = timeout ? new AbortController() : null, timer = ctl && setTimeout(() => ctl.abort(), timeout);
    let response;
    try { response = await fetch(base + path, {credentials:"same-origin", ...(ctl ? {signal: ctl.signal} : {}), ...rest, headers:{"Content-Type":"application/json", ...(rest.headers||{})}}); }
    catch (error) { throw error.name === "AbortError" ? Object.assign(new Error("The server took too long to answer."), {timedOut: true}) : error; }
    finally { clearTimeout(timer); }
    let body = {};
    try { body = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(body.detail || body.message || body.error || `Request failed (${response.status})`);
    return body;
  }
  const field = (name) => $("#job-form").elements.namedItem(name);
  let lastChecked = "";

  // ---- model file selectors (one per slot of the chosen family) ----
  const VERDICT = {ready:"", untested:" · untested", unsupported:" · can't train"};
  const explainers = [];
  function renderSlots() {
    explainers.length = 0;
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
      const explain = () => {
        const c = slot.candidates.find((x) => x.file === select.value);
        why.textContent = (fineTune() && c?.ft_reason ? `Can't fine-tune: ${c.ft_reason}` : "") || c?.reason || slot.note || "";
        why.hidden = !why.textContent;
      };
      explainers.push(explain);
      select.addEventListener("change", () => { explain(); presetEdited(); applyKind(); });
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
    if ("base_model" in settings) renderBases();
  }
  function applyPreset(value) {
    const settings = presetSettings(value);
    if (!settings) return;
    if (family()?.finetune && !("kind" in settings)) field("kind").value = "lora";   // older presets are LoRA presets
    applyValues(settings);
    applyKind();
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

  // Full fine-tune (Fizgig families): rotations + save cadence replace rank/epochs; each save is a full checkpoint.
  const fineTune = () => Boolean(family()?.finetune) && field("kind")?.value === "finetune";
  function applyKind() {
    const fam = family(), ft = fineTune(), knobs = new Set(fam?.knobs || []);
    document.querySelectorAll("[data-knob]").forEach((n) => {
      const k = n.dataset.knob;
      n.hidden = !knobs.has(k) || (ft && (k === "rank" || k === "epochs" || k === "lr_schedule")) || (!ft && n.hasAttribute("data-ft"))
        || (!ft && k === "learning_rate" && fam?.finetune?.lr_ft_only);   // H3 LoRAs set their own rate (Automagic)
    });
    $("#name-label").textContent = ft ? "Checkpoint name" : "LoRA name";
    const lr = field("learning_rate");
    if (lr) lr.placeholder = ft ? `fine-tune: ${fam.finetune.lr}` : fam?.defaults?.learning_rate ? `recipe: ${fam.defaults.learning_rate}` : "recipe";
    explainers.forEach((f) => f());
    const note = $("#ft-note");
    note.replaceChildren(); note.hidden = !ft;
    if (ft) {
      const base = (fam.slots.find((x) => x.base)?.candidates || []).find((c) => c.file === field("dit")?.value);
      const saves = Math.floor((Number(field("rotations").value || 1) - 1) / Number(field("save_every").value || 1)) + 1;
      note.className = "train-check " + (base?.ft_reason ? "bad" : "ok");
      note.append(el("strong", null, base?.ft_reason ? "✗ This base can't be fine-tuned" : `${saves} full checkpoint${saves === 1 ? "" : "s"} · ~${Math.round(saves * fam.finetune.gb)} GB on disk`));
      note.append(el("span", null, base?.ft_reason ? ` · ${fam.finetune.base || "pick the bf16 release"}` : " · saved to models/diffusion_models/Trainer/, ready for ComfyUI"));
      note.title = fam.finetune.note;
    }
    $("#submit-job").textContent = ft ? "Queue fine-tune" : "Queue training run";
  }
  field("kind").addEventListener("change", () => {
    const fam = family(), lr = field("learning_rate");
    if (lr && fam?.finetune) lr.value = fineTune() ? fam.finetune.lr : (fam.defaults?.learning_rate || "");
    applyKind();
  });
  for (const n of ["rotations", "save_every"]) field(n).addEventListener("input", applyKind);

  function setModelFields() {
    const fam = family();
    const knobs = new Set(fam?.knobs || []);
    document.querySelectorAll("[data-knob]").forEach((n) => { n.hidden = !knobs.has(n.dataset.knob); });
    const lr = field("learning_rate"); if (lr) lr.placeholder = fam?.defaults?.learning_rate ? `recipe: ${fam.defaults.learning_rate}` : "recipe";
    $("#model-hint").textContent = fam ? fam.hint : "";
    renderBases();
    renderSlots();
    renderFamilyStatus();
    if (!fam?.finetune) field("kind").value = "lora";
    selectDefaultPreset();
    applyKind();
    $("#submit-job").disabled = !fam || fam.state !== "ready";
  }

  // LLM family: base model suggestions (local HF folders, cached repos, the source repo of each installed GGUF).
  function baseNote() {
    const value = field("base_model").value.trim(), note = $("#base-note");
    const hit = (family()?.bases || []).find((b) => b.value === value);
    note.textContent = hit ? hit.note : /gguf|\.gguf$/i.test(value) ? "GGUF files can't be trained: use the original safetensors repo." : "";
    note.hidden = !note.textContent;
  }
  // Base-model picker (LLM LoRA + Uncensor): the safetensors bases behind your installed GGUFs, then
  // "Other repo or folder…", which reveals the free-text input. The input stays the named form field.
  const OTHER_BASE = "\u0000other";
  function fillBasePicker(select, input, bases, fallback) {
    const other = select.value === OTHER_BASE, want = input.value.trim();
    select.replaceChildren();
    if (bases.length) {
      const group = el("optgroup"); group.label = "From your installed chat models";
      for (const b of bases) { const o = new Option(b.label || b.value, b.value); o.title = b.note; group.append(o); }
      select.append(group);
    } else select.append(new Option("No installed chat model with a known base", ""));
    select.append(new Option("Other repo or folder…", OTHER_BASE));
    const pick = want || (other ? "" : fallback || "");
    select.value = bases.some((b) => b.value === pick) ? pick : pick || other || !bases.length ? OTHER_BASE : bases[0].value;
    if (select.value !== OTHER_BASE) input.value = select.value; else if (pick) input.value = pick;
    input.hidden = select.value !== OTHER_BASE;
  }
  function wireBasePicker(select, input, changed) {
    select.addEventListener("change", () => {
      const other = select.value === OTHER_BASE;
      input.hidden = !other; input.value = other ? "" : select.value;
      if (other) input.focus();
      input.dispatchEvent(new Event("input")); input.dispatchEvent(new Event("change"));   // notes, saved choice, preset "edited"
    });
    input.addEventListener("input", changed);
  }
  const savedBase = (key) => { try { return localStorage.getItem(key) || ""; } catch (_) { return ""; } };
  const saveBase = (key, v) => { try { localStorage.setItem(key, v); } catch (_) {} };
  function renderBases() {
    const fam = family(), bases = fam?.bases;
    if (!bases) { baseNote(); return; }
    fillBasePicker($("#base-select"), field("base_model"), bases,
      savedBase("trainer-llm-base") || bases.find((b) => /fits it exactly/.test(b.note))?.value);
    baseNote();
  }
  wireBasePicker($("#base-select"), field("base_model"), () => { baseNote(); saveBase("trainer-llm-base", field("base_model").value.trim()); });

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
      for (const f of list) g.append(new Option(`${f.title}${f.group === "Video" ? " (video)" : f.group === "Music" ? " (music)" : ""}`, f.id));
      select.append(g);
    }
    const recipes = fams.filter((f) => f.recipe);
    if (recipes.length) {
      const g = document.createElement("optgroup"); g.label = familyData.ai_toolkit ? "AI Toolkit recipes (base model from Hugging Face)" : "AI Toolkit recipes (install AI Toolkit first)";
      for (const f of recipes) g.append(new Option(f.title.replace(" (AI Toolkit)", ""), f.id));
      select.append(g);
    }
    // First visit: a family that is fully ready (tested files) beats one that only has untested files.
    select.value = fams.some((f) => f.id === current) ? current
      : (fams.find((f) => f.state === "ready" && !f.untested_only?.length && !f.recipe)?.id || fams.find((f) => f.state === "ready")?.id || fams[0]?.id || "");
  }
  async function refreshFamilies() {
    try { familyData = await api("/api/families"); }
    catch (error) { message(`Could not check installed models: ${error.message}`, true); return; }
    fillFamilySelect();
    setModelFields();
    if (!$("#uncensor-form").hidden) renderUncensorBases();
    const others = familyData.unrecognised || [], box = $("#other-models");
    box.hidden = !others.length;
    box.querySelector("summary").textContent = `${others.length} other model file${others.length === 1 ? "" : "s"} found that nothing here can train`;
    box.querySelector("ul").replaceChildren(...others.map((o) => { const li = el("li"); li.append(el("code", null, o.file), document.createTextNode(` ${o.variant ? o.variant + ": " : ""}${o.reason}`)); return li; }));
  }

  // Compact bar: one chip per environment; the full cards sit behind "Details" (remembered per browser).
  function setEnvOpen(open) {
    $("#env-details").hidden = !open; $("#env-toggle").setAttribute("aria-expanded", String(open));
    $("#env-toggle").textContent = open ? "Hide details" : "Details";
    try { localStorage.setItem("trainer-env-open", open ? "1" : ""); } catch (_) {}
  }
  $("#env-toggle").addEventListener("click", () => setEnvOpen($("#env-details").hidden));
  try { setEnvOpen(Boolean(localStorage.getItem("trainer-env-open"))); } catch (_) {}
  function renderEnvChips(status) {
    const chips = $("#env-chips"); chips.replaceChildren();
    for (const backend of Object.values(status.backends || {})) {
      const installing = status.setups?.[backend.install]?.state === "installing";
      const state = installing ? "installing" : backend.state;
      const canInstall = state === "not_installed" && backend.install;
      const chip = el(canInstall ? "button" : "span", `env-chip state-${state}`);
      chip.append(el("i", "env-dot"), document.createTextNode(canInstall ? `Install ${backend.title}` : backend.title));
      chip.title = `${backend.title}: ${state.replaceAll("_", " ")}\n${backend.covers}`;
      if (canInstall) { chip.type = "button"; chip.dataset.backend = backend.install; chip.addEventListener("click", () => runSetup(chip)); }
      chips.append(chip);
    }
  }
  function renderBackends(status) {
    renderEnvChips(status);
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
    renderUncensorEnv(status);
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
      const left = job.timing?.remaining_seconds ?? p.eta_seconds;
      if (left != null && ACTIVE.has(job.state)) overallText += ` · ${duration(left)} left`;
    }
    body.append(bar(p.overall, overallText, `overall ${job.state}`));
    // Session clock, start to end: wall time so far + the epoch-average estimate of what's left.
    const t = job.timing || {};
    if (t.session_elapsed_seconds != null) {
      const clock = [`${ACTIVE.has(job.state) ? "Session" : "Session took"} ${duration(t.session_elapsed_seconds)}`];
      if (t.avg_epoch_seconds) clock.push(`avg ${duration(t.avg_epoch_seconds)}/epoch`);
      if (t.remaining_seconds != null && ACTIVE.has(job.state)) {
        clock.push(`~${duration(t.remaining_seconds)} left`,
          `total ~${duration(t.total_seconds)}`,
          `done ≈ ${new Date(t.finish_at).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"})}`);
      }
      body.append(el("p", "box-note", clock.join(" · ")));
    }
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
        if (e.seconds != null && e.status === "done") text += ` · ${duration(e.seconds)}`;
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
    const rows = r.kind === "finetune" ? [["Dataset", job.dataset], ["Kind", "Full fine-tune"], ["Rotations", r.rotations], ["Save every", `${r.save_every} rotation(s)`]]
      : r.kind === "extract" ? [["Kind", "LoRA extracted from a fine-tune"], ["Checkpoint", r.checkpoint], ["Ranks", r.ranks]]
      : r.kind === "uncensor" ? [["Kind", `Uncensor · ${METHOD[r.method] || r.method}`], ["Chat model", r.base_model], ["Saved to", `~/AI/Trainer/data/output/${job.id}/model/`]]
      : r.kind === "refusal_test" ? [["Kind", "Refusal test"], ["Answer length", `${r.max_tokens || 256} tokens`], ["Report", `~/AI/Trainer/data/output/${job.id}/refusals.json`]]
      : [["Dataset", job.dataset], ["Rank", r.rank]];
    const knobs = family(job.model)?.knobs || [];
    if (!r.kind || r.kind === "lora") rows.push(knobs.includes("steps") && !knobs.includes("epochs") ? ["Steps", r.steps] : ["Epochs", r.epochs]);
    if (knobs.includes("max_seconds")) rows.push(["Max seconds", r.max_seconds]);
    if (knobs.includes("resolution") || job.model === "minimax_h3" || job.model === "ltx25") rows.push(["Resolution", r.resolution]);
    if (r.learning_rate) rows.push(["Learning rate", r.learning_rate]);
    if (r.lr_schedule) rows.push(["LR schedule", {flat:"Flat", x2:"Adaptive LR…2×", x4:"Adaptive LR…4×"}[r.lr_schedule] || r.lr_schedule]);
    if (r.trigger) rows.push(["Trigger", r.trigger]);
    if (job.model === "llm" && !["uncensor", "refusal_test"].includes(r.kind)) rows.push(["Base model", r.base_model], ["Context", `${r.max_length} tokens`], ["Base precision", r.quant]);
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
      body.append(el("p", "box-note", `${r.kind === "finetune" ? "Checkpoints" : r.kind === "uncensor" ? "Model files" : "LoRA files"} (${job.outputs.length}), in ${job.outputs[0].host_path.replace(/[^/]+$/, "")}`
        + (job.model === "llm" && !["uncensor", "refusal_test"].includes(r.kind) ? " · the .gguf loads in llama.cpp with --lora on the same model's GGUF; peft/ holds the PEFT adapter" : "")));
      const ul = el("ul", "outputs");
      const deletable = !ACTIVE.has(job.state);
      for (const o of job.outputs) {
        const li = el("li", null, `${o.name} · ${o.size_mb} MB${o.final ? " · final" : ""}`);
        if (deletable && o.epoch != null) {
          const del = el("button", "button secondary small", "Delete"); del.type = "button";
          del.title = o.resume_state ? `Delete epoch ${o.epoch}'s resume state` : `Delete epoch ${o.epoch}`;
          del.addEventListener("click", () => deleteEpochFile(job, o));
          li.append(" ", del);
        }
        ul.append(li);
      }
      body.append(ul);
      const epochCount = new Set(job.outputs.filter((o) => o.epoch != null).map((o) => o.epoch)).size;
      if (deletable && epochCount > 1) {
        const clean = el("button", "button secondary", `Clean up epochs (${epochCount})…`); clean.type = "button";
        clean.addEventListener("click", () => openEpochCleanup(job));
        body.append(clean);
      }
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
  // After "Use in Llama": open the chosen model's Parameters tab in Llama with this LoRA preselected
  // (llama-params.js reads the hand-off from localStorage; same origin) - the user presses Save there.
  function llamaPicker(r) {
    const box = el("div", "llama-pick");
    const open = (id) => {
      try { localStorage.setItem("rocsy-pending-lora", JSON.stringify({model:id, file:r.file, at:Date.now()})); } catch (_) {}
      window.open(`/Llama/ui/#/models/${encodeURIComponent(id)}`, "_blank", "noopener");
    };
    box.append(el("p", "box-note", r.fits.length ? `Copied to models/llm/loras/${r.file}. Turn it on for:` : `Copied to models/llm/loras/${r.file}. No installed chat model was made from this base model; pick one to try it on:`));
    const row = el("div", "llama-pick-row");
    for (const m of r.fits) {
      const b = el("button", "button secondary small", `${m.name} · ${m.fit === "exact" ? "exact fit" : "same base"}`); b.type = "button";
      b.addEventListener("click", () => open(m.id));
      row.append(b);
    }
    const others = r.models.filter((m) => !r.fits.some((f) => f.id === m.id));
    if (others.length) {
      const sel = el("select"); sel.append(new Option(r.fits.length ? "Another model…" : "Choose a model…", ""));
      for (const m of others) sel.append(new Option(m.name, m.id));
      sel.addEventListener("change", () => { if (sel.value) open(sel.value); });
      row.append(sel);
    }
    box.append(row);
    return box;
  }
  const IMAGE_FAMILIES = ["krea2", "qwen_image_21", "moss_image", "anima"];

  // ---- epoch tests: a start form, then pages of renders (one page per prompt, or per epoch), each prompt + seed
  // rendered on every saved epoch. Optional agent review: a vision model scores each render live (Hermes terminal
  // beside the panel only while review mode is on) and the best epoch is highlighted.
  const EPOCH_ACTIVE = new Set(["preparing", "writing_prompt", "rendering"]);
  const REVIEW_ACTIVE = new Set(["waiting_for_gpu", "preparing", "running"]);
  const epochUrl = (job, rest = "") => `/api/jobs/${encodeURIComponent(job.id)}/epoch-test${rest}`;
  let epochDlg = null, epochPoll = null, ev = null, epochOpenToken = null;
  function epochDialog() {
    if (epochDlg) return epochDlg;
    epochDlg = el("dialog", "epoch-dialog");
    epochDlg.addEventListener("close", () => { clearTimeout(epochPoll); epochDlg.replaceChildren(); ev = null; });
    epochDlg.addEventListener("click", (e) => { if (e.target === epochDlg) epochDlg.close(); });   // backdrop
    document.body.append(epochDlg);
    return epochDlg;
  }
  function epochHead(title) {
    const head = el("div", "epoch-head");
    const x = el("button", "button secondary icon", "✕"); x.type = "button"; x.title = "Close";
    x.addEventListener("click", () => epochDlg.close());
    head.append(el("h3", null, title), x);
    return head;
  }
  async function openEpochs(job, start) {
    const dlg = epochDialog();
    clearTimeout(epochPoll); ev = null;
    const note = el("p", "muted", "Loading…"), token = epochOpenToken = {};
    dlg.replaceChildren(epochHead(`${start ? "Test epochs" : "Epochs"} · ${job.name}`), note);
    if (!dlg.open) dlg.showModal();
    // A hung request retries instead of leaving "Loading…" up for good; a newer open or a close drops this one.
    for (let tryNo = 1; ; tryNo++) {
      try {
        const data = await api(epochUrl(job, start ? "/options" : ""), {timeout: 12000});
        if (token !== epochOpenToken || !dlg.open) return;
        return start ? epochStartForm(job, data) : epochGallery(job, data);
      } catch (error) {
        if (token !== epochOpenToken || !dlg.open) return;
        if (!error.timedOut && !(error instanceof TypeError) || tryNo >= 3) {
          const again = el("button", "button secondary", "Try again"); again.type = "button";
          again.addEventListener("click", () => openEpochs(job, start));
          return dlg.replaceChildren(epochHead(job.name), el("p", "bad", error.message), again);
        }
        note.textContent = `Still loading… (try ${tryNo + 1} of 3)`;
      }
    }
  }
  // Small cached WebPs for grid tiles and the viewer strip; the viewer itself shows the full render.
  const thumbUrl = (url, w = 480) => `${url}${url.includes("?") ? "&" : "?"}thumb=${w}`;
  // What deleting/replacing a test throws away, spelled out for the confirm dialogs.
  function epochLoss(test) {
    const renders = test.items.filter((i) => i.output).length, reviewed = test.items.filter((i) => i.review).length;
    return `${test.runs.length} prompt${test.runs.length === 1 ? "" : "s"} and ${renders} render${renders === 1 ? "" : "s"}`
      + (reviewed ? `, plus the agent review (${reviewed} score${reviewed === 1 ? "" : "s"}${test.review?.best ? `, best: ${test.review.best}` : ""})` : "");
  }
  // Guides steer the prompts the agent writes: one line per agent-written prompt in order; a single line guides all of them.
  const splitGuides = (text) => text.split("\n").map((t) => t.trim()).filter(Boolean);
  function guideBox(lines = []) {
    const box = el("details", "epoch-own"); box.append(el("summary", null, "Guide what the agent's prompts test"));
    const area = el("textarea"); area.rows = 3; area.value = lines.join("\n");
    area.placeholder = "One line per agent-written prompt, in order. A single line guides them all.\ne.g. close-up of the face, neutral light\nfull body, walking outdoors at night\nunusual outfit the dataset never shows";
    box.append(area); box.open = !!area.value;
    return {box, area};
  }
  // Agent prompts without the training captions: the look must come from the trigger word alone.
  function noTagsBox(on = false) {
    const box = el("input"); box.type = "checkbox"; box.checked = on;
    const label = el("label", "epoch-check"); label.title = "The agent gets no training captions and describes only the scene (setting, pose, framing, light), so the renders show what the trigger word brings on its own";
    label.append(box, document.createTextNode(" Leave out the training tags (trigger word only)"));
    return {label, box};
  }
  const splitPrompts = (text) => text.split(/^\s*---+\s*$/m).map((t) => t.trim()).filter(Boolean);
  function modelSelect(opt, value) {
    const sel = el("select");
    for (const m of opt.vision_models || []) sel.append(new Option(m.name || m.id, m.id));
    if (!sel.options.length) sel.append(new Option("No vision model in llama-swap", ""));
    sel.value = value || opt.review_model || sel.options[0].value;
    return sel;
  }
  function epochStartForm(job, opt, previous = null) {
    const dlg = epochDlg, form = el("form", "epoch-form");
    clearTimeout(epochPoll); ev = null;
    const studio = opt.studio === "video" ? "Video" : "Image";
    form.append(el("p", "muted", `${opt.epochs.length} saved epoch${opt.epochs.length === 1 ? "" : "s"} (${opt.epochs.join(", ")}). `
      + `The ${studio} studio's agent writes the prompts from the dataset's captions${job.request?.trigger ? ` and the trigger “${job.request.trigger}”` : ""}; `
      + `every prompt renders on every epoch with its own fixed seed, queued in the ${studio} studio. Each prompt is a page.`));
    const row = el("div", "epoch-fields");
    let preset = null;
    if (opt.studio === "video") {
      preset = el("select"); preset.name = "preset";
      preset.append(new Option("Short test (5 s, low res)", ""));
      for (const n of opt.presets) preset.append(new Option(`Preset: ${n}`, n));
      preset.value = previous?.preset ?? opt.default;
      const l = el("label", null, "Render settings"); l.append(preset); row.append(l);
    }
    const runs = el("input"); runs.type = "number"; runs.min = "1"; runs.max = String(opt.max_runs || 12); runs.step = "1";
    runs.value = previous?.runs?.length || (opt.studio === "video" ? 2 : 3);
    const lr = el("label", null, "Prompts (pages)"); lr.append(runs); row.append(lr);
    const strength = el("input"); strength.type = "number"; strength.min = "0"; strength.max = "2"; strength.step = "0.05"; strength.value = previous?.strength ?? 1;
    const ls = el("label", null, "LoRA strength"); ls.append(strength); row.append(ls);
    let every = null;
    if (opt.epochs.length > 6) {
      every = el("select");
      for (const n of [1, 2, 3, 4, 5, 10]) if (n === 1 || n < opt.epochs.length) every.append(new Option(n === 1 ? "Every epoch" : `Every ${n}th (last always)`, String(n)));
      const le = el("label", null, "Which epochs"); le.append(every); row.append(le);
    }
    form.append(row);
    const count = el("p", "muted epoch-count");
    const recount = () => {
      const n = Number(every?.value || 1), e = n > 1 ? Math.ceil(opt.epochs.length / n) : opt.epochs.length;
      const r = Math.max(Number(runs.value) || 1, splitPrompts(prompt.value).length + splitGuides(guide.area.value).length);
      count.textContent = `${r} prompt${r === 1 ? "" : "s"} × ${e} epoch${e === 1 ? "" : "s"} = ${r * e} render${r * e === 1 ? "" : "s"}`;
    };
    const own = el("details", "epoch-own"); own.append(el("summary", null, "Write prompts myself"));
    const prompt = el("textarea"); prompt.rows = 4; prompt.placeholder = "Leave empty and the agent writes them. Several prompts: separate them with a line of ---. Yours fill the first pages, the agent writes the rest.";
    const mine = (previous?.runs || []).filter((r) => r.prompt_by === "you").map((r) => r.prompt);
    prompt.value = mine.join("\n---\n");
    own.append(prompt); own.open = !!prompt.value; form.append(own);
    const guide = guideBox((previous?.runs || []).map((r) => r.guide).filter(Boolean));
    const noTags = noTagsBox(!!previous?.runs?.some((r) => r.no_tags));
    form.append(guide.box, noTags.label);
    // Review mode: the agent scores every render for what the LoRA should teach once they have all rendered.
    const rv = el("div", "epoch-review-opts");
    const auto = el("input"); auto.type = "checkbox"; auto.checked = !!previous?.auto_review;
    const al = el("label", "epoch-check"); al.append(auto, document.createTextNode(" Agent review when the renders finish (live, picks the best epoch)"));
    const model = modelSelect(opt, previous?.review_model);
    const lm = el("label", null, "Vision model"); lm.append(model);
    const teach = el("input"); teach.type = "text"; teach.maxLength = 500; teach.value = previous?.teach || "";
    teach.placeholder = "e.g. a character's face and outfit, or a watercolour style";
    const lt = el("label", "wide", "What should the LoRA teach? (optional, also guides the prompts)"); lt.append(teach);
    const rf = el("div", "epoch-fields"); rf.append(lm, lt);
    const sync = () => { rf.hidden = !auto.checked; };
    auto.addEventListener("change", sync); sync();
    rv.append(al, rf); form.append(rv);
    if (previous) form.append(el("p", "bad epoch-replace", `Starting replaces the old test: its ${epochLoss(previous)} are deleted for good.`));
    const go = el("button", "button primary", previous ? "Delete old test and start" : "Start epoch test"); go.type = "submit";
    const msg = el("span", "muted");
    const acts = el("div", "epoch-acts"); acts.append(go);
    if (previous) {
      const back = el("button", "button secondary", "Back to the old test"); back.type = "button";
      back.addEventListener("click", async () => { try { epochGallery(job, await api(epochUrl(job)), true); } catch (error) { msg.textContent = error.message; } });
      acts.append(back);
    }
    acts.append(count, msg); form.append(acts);
    for (const n of [runs, prompt, guide.area, every].filter(Boolean)) n.addEventListener("input", recount);
    every?.addEventListener("change", recount); recount();
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (previous && !confirm(`This deletes the old epoch test completely: ${epochLoss(previous)}. It can't be undone.\n\nStart the new test?`)) return;
      go.disabled = true; msg.textContent = "Starting…";
      try {
        const list = splitPrompts(prompt.value), guides = splitGuides(guide.area.value);
        const test = await api(epochUrl(job), {method:"POST", body:JSON.stringify({preset: preset?.value || "", strength: Number(strength.value) || 0,
          runs: Math.max(Number(runs.value) || 1, list.length + guides.length), prompts: list, guides, no_tags: noTags.box.checked, every: Number(every?.value || 1),
          review: auto.checked, review_model: auto.checked ? model.value : "", teach: teach.value})});
        refreshJobs(); epochGallery(job, test);
      } catch (error) { msg.textContent = error.message; go.disabled = false; }
    });
    dlg.replaceChildren(epochHead(`Test epochs · ${job.name}`), form);
  }

  // More pages on a finished test: new prompts or one prompt under new seeds, on every epoch or only the ones picked
  // (from the viewer: that epoch + its prompt). Earlier renders stay.
  const MAX_EPOCH_RUNS = 32;
  function epochMoreForm(job, test, pre = {}) {
    clearTimeout(epochPoll);
    const dlg = epochDlg, form = el("form", "epoch-form");
    const labels = [...new Set(test.items.map((i) => i.label))], stars = (test.starred || []).filter((l) => labels.includes(l));
    const room = MAX_EPOCH_RUNS - test.runs.length;
    // Epochs
    const pick = new Set(pre.labels || (stars.length ? stars : labels));
    const chips = el("div", "more-epochs");
    const paint = () => { for (const c of chips.children) c.classList.toggle("on", pick.has(c.dataset.label)); recount(); };
    for (const l of labels) {
      const c = el("button", `more-epoch${stars.includes(l) ? " starred" : ""}`, `${stars.includes(l) ? "★ " : ""}${l.replace(/^Epoch /, "E")}`); c.type = "button";
      c.dataset.label = l; c.title = l;
      c.addEventListener("click", () => { pick.has(l) ? pick.delete(l) : pick.add(l); paint(); });
      chips.append(c);
    }
    const quick = el("div", "more-quick");
    for (const [t, set] of [["All", labels], ["Starred", stars], ["None", []]]) {
      const b = el("button", "seg-btn", t); b.type = "button"; b.disabled = t === "Starred" && !stars.length;
      b.addEventListener("click", () => { pick.clear(); for (const l of set) pick.add(l); paint(); });
      quick.append(b);
    }
    const ep = el("div", "epoch-label", "Epochs"); ep.append(quick);
    // What to render
    let mode = pre.run != null ? "seeds" : "prompts";
    const seg = el("div", "seg more-mode");
    const modes = {prompts: "New prompts", seeds: "Same prompt, new seeds"};
    const row = el("div", "epoch-fields");
    const src = el("select");
    test.runs.forEach((r, k) => { if (r.prompt) src.append(new Option(`Prompt ${k + 1}${r.of != null ? ` (= ${r.of + 1})` : ""} · seed ${r.seed}: ${r.prompt.slice(0, 70)}`, String(k))); });
    src.value = String(pre.run ?? 0);
    const ls = el("label", "wide", "Prompt"); ls.append(src);
    const count = el("input"); count.type = "number"; count.min = "1"; count.max = String(Math.max(1, room)); count.step = "1"; count.value = String(Math.min(room, mode === "seeds" ? 3 : 1));
    const lc = el("label", null, "How many"); lc.append(count);
    const seed = el("input"); seed.type = "number"; seed.min = "0"; seed.placeholder = "random";
    const lsd = el("label", null, "First seed"); lsd.append(seed);
    row.append(ls, lc, lsd);
    const own = el("details", "epoch-own"); own.append(el("summary", null, "Write prompts myself"));
    const prompt = el("textarea"); prompt.rows = 4; prompt.placeholder = "Leave empty and the agent writes new ones (different scenes from the existing pages). Several prompts: separate them with a line of ---.";
    own.append(prompt);
    const guide = guideBox(), noTags = noTagsBox(!!test.runs.at(-1)?.no_tags);
    const sync = () => {
      for (const b of seg.children) b.classList.toggle("on", b.dataset.mode === mode);
      ls.hidden = mode !== "seeds"; own.hidden = guide.box.hidden = noTags.label.hidden = mode !== "prompts";
      lc.firstChild.textContent = mode === "seeds" ? "New seeds" : "New prompts";
      recount();
    };
    for (const [m, t] of Object.entries(modes)) {
      const b = el("button", "seg-btn", t); b.type = "button"; b.dataset.mode = m;
      b.addEventListener("click", () => { mode = m; sync(); });
      seg.append(b);
    }
    const note = el("p", "muted epoch-count"), msg = el("span", "muted");
    const go = el("button", "button primary", "Render"); go.type = "submit";
    function recount() {
      const n = Math.min(room, Math.max(Number(count.value) || 1, mode === "prompts" ? splitPrompts(prompt.value).length + splitGuides(guide.area.value).length : 0)), e = pick.size;
      note.textContent = `${n} new page${n === 1 ? "" : "s"} × ${e} epoch${e === 1 ? "" : "s"} = ${n * e} render${n * e === 1 ? "" : "s"}` + (room < 5 ? ` · room for ${room} more page${room === 1 ? "" : "s"}` : "");
      go.disabled = !e || room < 1;
    }
    form.append(el("p", "muted", "Each new page is one prompt + seed rendered on the epochs you pick. Existing renders are kept."), ep, chips,
      el("div", "epoch-label more-what", "Render"), seg, row, own, guide.box, noTags.label);
    const back = el("button", "button secondary", "Back"); back.type = "button";
    back.addEventListener("click", () => epochGallery(job, test, true));
    const acts = el("div", "epoch-acts"); acts.append(go, back, note, msg); form.append(acts);
    for (const n of [count, prompt, guide.area]) n.addEventListener("input", recount);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      go.disabled = true; msg.textContent = "Starting…";
      const list = mode === "prompts" ? splitPrompts(prompt.value) : [], guides = mode === "prompts" ? splitGuides(guide.area.value) : [];
      const body = {runs: Math.max(Number(count.value) || 1, list.length + guides.length), prompts: list, guides, no_tags: mode === "prompts" && noTags.box.checked,
        labels: pick.size === labels.length ? [] : labels.filter((l) => pick.has(l)),
        seed_from: mode === "seeds" ? Number(src.value) : null, seed: seed.value === "" ? null : Number(seed.value)};
      try {
        const fresh = await api(epochUrl(job, "/more"), {method:"POST", body:JSON.stringify(body)});
        if (ev) { ev.view = "prompt"; ev.page = test.runs.length; ev.starOnly = false; ev.follow = false; }
        epochGallery(job, fresh, true); refreshJobs();
      } catch (error) { msg.textContent = error.message; go.disabled = false; }
    });
    dlg.replaceChildren(epochHead(`More tests · ${job.name}`), form);
    paint(); sync();
  }

  const hhmm = (t) => new Date(t * 1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit", hour12:false});
  function epochTermLine(l) {
    const row = el("div", `hl hl-${l.kind}`);
    row.append(el("span", "hl-time", hhmm(l.t)));
    const body = el("span", "hl-body");
    if (l.kind === "user") body.append(el("span", "hl-prompt", "❯ "), document.createTextNode(l.text));
    else if (l.kind === "tool") {
      const [name, ...rest] = l.text.split("  ");
      body.append(el("span", "hl-bar", "┊ "), document.createTextNode(`${l.icon || "⚙"} `), el("span", "hl-tool", name), document.createTextNode("  " + rest.join("  ")));
      if (l.ms != null) body.append(el("span", "hl-dim", `  ${l.ms} ms`));
    } else if (l.kind === "assistant") {
      body.append(el("span", "hl-brand", "☤ "), document.createTextNode(l.text));
      for (const g of l.good || []) body.append(document.createTextNode(" "), el("span", "rv-chip good", g));
      for (const b of l.bad || []) body.append(document.createTextNode(" "), el("span", "rv-chip bad", b));
    } else if (l.kind === "ok") body.append(el("span", "hl-ok", "✓ "), document.createTextNode(l.text));
    else if (l.kind === "warn") body.append(el("span", "hl-warn", "⚠ "), document.createTextNode(l.text));
    else if (l.kind === "error") body.append(el("span", "hl-err", "✗ "), document.createTextNode(l.text));
    else body.append(el("span", "hl-dim", "· "), document.createTextNode(l.text));
    row.append(body);
    if (l.item != null) { row.classList.add("hl-link"); row.title = "Show this render"; row.addEventListener("click", () => showItem(l.item, true)); }
    return row;
  }
  function epochTerm() {
    const term = el("div", "hterm epoch-term");
    const head = el("div", "hterm-head");
    const follow = el("button", "hterm-follow", "⤓ follow"); follow.type = "button"; follow.title = "Jump to the render the agent is reviewing";
    follow.addEventListener("click", () => { ev.follow = true; follow.hidden = true; if (ev.test.review?.current != null) showItem(ev.test.review.current, false); });
    const close = el("button", "hterm-close", "✕"); close.type = "button"; close.title = "Hide the review log";
    close.addEventListener("click", () => { ev.showLog = false; epochGallery(ev.job, ev.test, true); });
    head.append(el("span", "hterm-brand", "☤ Hermes Agent"), el("span", "hterm-sub", " · epoch-review"), el("span", "hterm-state"), follow, close);
    term.append(head, el("div", "hterm-body"), el("div", "hterm-status"));
    return term;
  }
  function paintTerm() {
    const term = ev?.term, r = ev?.test.review;
    if (!term || !r) return;
    const body = term.querySelector(".hterm-body"), stick = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    body.querySelector(".hl-cursor")?.remove();
    for (const l of ev.newLines.splice(0)) body.append(epochTermLine(l));
    const active = REVIEW_ACTIVE.has(r.state);
    if (active) { const cur = el("div", "hl hl-cursor"); cur.append(el("span", "hl-time", ""), el("span", "hl-body")); cur.lastChild.append(el("span", "hl-spin", "⠋"), document.createTextNode(` ${r.stage}`)); body.append(cur); }
    if (stick) body.scrollTop = body.scrollHeight;
    const st = term.querySelector(".hterm-state"); st.textContent = active ? "● running" : r.state === "complete" ? "● done" : `● ${r.state}`; st.dataset.state = r.state;
    term.querySelector(".hterm-follow").hidden = ev.follow || !active;
    const reviewed = ev.test.items.filter((i) => i.review).length, total = ev.test.items.filter((i) => i.state === "complete").length;
    term.querySelector(".hterm-status").textContent = `⚕ ${r.model}  │  ${reviewed}/${total} reviewed${r.best ? `  │  ★ ${r.best}` : ""}`;
  }
  // Pages: "prompt" view = one page per prompt (every epoch on it); "epoch" view = one page per epoch (every prompt).
  function epochPages(test) {
    // "Starred" filter: only your starred epochs (ignored once nothing is starred).
    const only = ev.starOnly && test.starred?.length ? new Set(test.starred) : null;
    const items = test.items.map((i, n) => [i, n]).filter(([i]) => !only || only.has(i.label));
    const labels = [...new Set(items.map(([i]) => i.label))];
    return ev.view === "epoch" ? labels.map((l) => ({key: l, title: l, items: items.filter(([i]) => i.label === l)}))
      : test.runs.map((r, k) => ({key: k, title: `Prompt ${k + 1}`, run: r, items: items.filter(([i]) => (i.run || 0) === k)}));
  }
  async function deleteRender(n, ask = true) {
    const i = ev?.test.items[n];
    if (!i || (ask && !confirm(`Delete this render (${i.label}, prompt ${(i.run || 0) + 1})?`))) return;
    const list = viewer?.open ? viewList() : [], at = list.indexOf(n);
    try { ev.test = await api(epochUrl(ev.job, `/items/${n}`), {method:"DELETE"}); }
    catch (error) { message(error.message, true); return; }
    epochGallery(ev.job, ev.test, true); refreshJobs();
    if (viewer?.open) {
      // Indices after n shift down by one; carry on with the next picture.
      const next = list.slice(at + 1).concat(list.slice(0, at)).map((m) => m > n ? m - 1 : m), now = viewList();
      viewAt = next.find((m) => now.includes(m)) ?? -1;
      if (viewAt < 0) viewer.close(); else paintViewer();
    }
  }
  const isStarred = (label) => !!ev?.test.starred?.includes(label);
  async function toggleStar(label) {
    const list = viewer?.open ? viewList() : [], at = list.indexOf(viewAt);
    try {
      const res = await api(epochUrl(ev.job, "/star"), {method:"POST", body:JSON.stringify({label, starred: !isStarred(label)})});
      ev.test.starred = res.starred;
    } catch (error) { message(error.message, true); return; }
    epochGallery(ev.job, ev.test, true);
    if (viewer?.open) {
      // Unstarring under the Starred filter drops the picture from the list: move on to the next one still in it.
      const now = viewList();
      if (!now.length) viewer.close();
      else { if (!now.includes(viewAt)) viewAt = list.slice(at + 1).concat(list.slice(0, at)).find((n) => now.includes(n)) ?? now[0]; paintViewer(); }
    }
    refreshJobs();
  }
  const starBtn = (label, cls = "") => {
    const on = isStarred(label), b = el("button", `star-btn${on ? " on" : ""}${cls ? ` ${cls}` : ""}`, on ? "★" : "☆"); b.type = "button";
    b.title = on ? `Unstar ${label}` : `Star ${label}`;
    b.addEventListener("click", (e) => { e.stopPropagation(); toggleStar(label); });
    return b;
  };
  function showItem(n, manual) {
    const i = ev?.test.items[n];
    if (!i) return;
    if (manual) ev.follow = false;
    const pages = epochPages(ev.test), page = pages.findIndex((pg) => pg.items.some(([, m]) => m === n));
    if (page >= 0 && page !== ev.page) { ev.page = page; epochGallery(ev.job, ev.test, true); }
    const tile = epochDlg.querySelector(`.epoch-tile[data-idx="${n}"]`);
    if (tile && manual) { tile.scrollIntoView({block:"nearest", behavior:"smooth"}); tile.classList.remove("rv-flash"); void tile.offsetWidth; tile.classList.add("rv-flash"); }
  }
  // Image viewer over the epoch dialog: walks every finished render in the current view's order (all pages),
  // ←/→ step, ↑/↓ jump a page (prompt or epoch), Home/End, swipe on touch. Closing lands on that tile.
  let viewer = null, viewAt = -1;
  const delBtn = () => viewer?.querySelector(".ev-del");
  const viewList = () => epochPages(ev.test).flatMap((pg) => pg.items.filter(([i]) => i.media_url).map(([, n]) => n));
  function openViewer(n) {
    if (!viewer) {
      viewer = el("dialog", "epoch-viewer");
      viewer.addEventListener("click", (e) => { if (e.target === viewer || e.target.classList.contains("ev-stage")) viewer.close(); });
      viewer.addEventListener("close", () => { if (ev && viewAt >= 0) showItem(viewAt, true); viewAt = -1; });
      viewer.addEventListener("keydown", (e) => {
        if (e.target.closest?.("input,select,textarea")) return;
        if (e.key === "Delete" && !delBtn()?.hidden) { e.preventDefault(); deleteRender(viewAt); return; }
        if (/^[sf]$/i.test(e.key) && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); const i = ev?.test.items[viewAt]; if (i) toggleStar(i.label); return; }
        const step = {ArrowLeft: -1, ArrowRight: 1, ArrowUp: "up", ArrowDown: "down", Home: "home", End: "end"}[e.key];
        if (step == null) return;
        e.preventDefault(); viewStep(step);
      });
      let x0 = null;
      viewer.addEventListener("touchstart", (e) => { x0 = e.touches.length === 1 ? e.touches[0].clientX : null; }, {passive: true});
      viewer.addEventListener("touchend", (e) => { if (x0 == null) return; const dx = e.changedTouches[0].clientX - x0; x0 = null; if (Math.abs(dx) > 50) viewStep(dx < 0 ? 1 : -1); });
      document.body.append(viewer);
    }
    viewAt = n; paintViewer();
    if (!viewer.open) viewer.showModal();
  }
  function viewStep(step) {
    const list = viewList(), k = list.indexOf(viewAt);
    if (!list.length) return;
    if (typeof step === "number") viewAt = list[(k + step + list.length) % list.length];
    else if (step === "home") viewAt = list[0];
    else if (step === "end") viewAt = list[list.length - 1];
    else {
      // Same position on the previous/next page that has renders.
      const pages = epochPages(ev.test).map((pg) => pg.items.filter(([i]) => i.media_url).map(([, n]) => n)).filter((p) => p.length);
      const p = pages.findIndex((pg) => pg.includes(viewAt)), pos = pages[p].indexOf(viewAt);
      const to = pages[(p + (step === "down" ? 1 : -1) + pages.length) % pages.length];
      viewAt = to[Math.min(pos, to.length - 1)];
    }
    paintViewer();
  }
  function paintViewer() {
    const test = ev.test, list = viewList(), k = list.indexOf(viewAt), i = test.items[viewAt];
    if (!i) { viewer.close(); return; }
    const run = test.runs[i.run || 0], rv = i.review, best = test.review?.best && i.label === test.review.best;
    const head = el("div", "ev-head"), title = el("div", "ev-title");
    const sb = starBtn(i.label, "ev-star"); sb.title += " (S)";
    title.append(sb, el("strong", null, i.label), el("span", null, `Prompt ${(i.run || 0) + 1}`), el("span", null, `Seed ${run?.seed ?? "—"}`));
    if (best) title.append(el("span", "ev-best", "Agent's best"));
    if (rv) { const b = el("span", `rv-badge ${rv.score >= 7 ? "hi" : rv.score >= 4 ? "mid" : "lo"}`, `${rv.score}`); b.title = `Concept ${rv.concept ?? "?"} · prompt ${rv.prompt ?? "?"} · quality ${rv.quality ?? "?"}`; title.append(b); }
    const tools = el("div", "ev-tools");
    const open = el("a", "button secondary icon", "↗"); open.href = i.media_url; open.target = "_blank"; open.rel = "noopener"; open.title = "Open full size in a new tab";
    const x = el("button", "button secondary icon", "✕"); x.type = "button"; x.title = "Close (Esc)"; x.addEventListener("click", () => viewer.close());
    const more = el("button", "button secondary", "＋ More tests"); more.type = "button";
    more.title = `More renders of ${i.label}: new seeds for this prompt, or new prompts`;
    more.hidden = EPOCH_ACTIVE.has(test.state) || REVIEW_ACTIVE.has(test.review?.state) || test.runs.length >= MAX_EPOCH_RUNS;
    more.addEventListener("click", () => { const job = ev.job; viewAt = -1; viewer.close(); epochMoreForm(job, test, {labels: [i.label], run: i.run || 0}); });
    const del = el("button", "button secondary danger ev-del", "Delete"); del.type = "button"; del.title = "Delete this render (Del)";
    del.hidden = EPOCH_ACTIVE.has(test.state) || REVIEW_ACTIVE.has(test.review?.state) || test.items.length < 2;
    del.addEventListener("click", () => deleteRender(viewAt));
    tools.append(el("span", "ev-count", `${k + 1} / ${list.length}`), more, del, open, x);
    head.append(title, tools);
    const stage = el("div", "ev-stage");
    const img = el("img"); img.src = i.media_url; img.alt = `${i.label} · seed ${run?.seed ?? ""}`;
    const prev = el("button", "ev-nav prev", "‹"), next = el("button", "ev-nav next", "›");
    prev.type = next.type = "button"; prev.title = "Previous (←)"; next.title = "Next (→)";
    prev.addEventListener("click", () => viewStep(-1)); next.addEventListener("click", () => viewStep(1));
    stage.append(img, prev, next);
    const foot = el("div", "ev-foot");
    if (run?.guide) foot.append(el("p", "ev-guide", `Guide: ${run.guide}`));
    if (run?.prompt) { const p = el("p", "ev-prompt", run.prompt); p.title = run.prompt; foot.append(p); }
    if (rv?.notes) foot.append(el("p", "ev-notes", rv.notes));
    const strip = el("div", "ev-strip");
    for (const n of list) {
      const it = test.items[n], t = el("button", `ev-thumb${n === viewAt ? " on" : ""}${isStarred(it.label) ? " starred" : ""}`); t.type = "button";
      t.title = `${it.label} · prompt ${(it.run || 0) + 1} · seed ${test.runs[it.run || 0]?.seed ?? ""}`;
      const ti = el("img"); ti.src = thumbUrl(it.media_url, 320); ti.alt = ""; ti.loading = "lazy"; ti.decoding = "async";
      t.append(ti, el("span", null, `${isStarred(it.label) ? "★ " : ""}${it.label.replace(/^Epoch /, "E")}`));
      t.addEventListener("click", () => { viewAt = n; paintViewer(); });
      strip.append(t);
    }
    foot.append(strip);
    viewer.replaceChildren(head, stage, foot);
    strip.querySelector(".on")?.scrollIntoView({block: "nearest", inline: "center"});
    for (const d of [-1, 1]) { const m = test.items[list[(k + d + list.length) % list.length]]; if (m?.media_url) new Image().src = m.media_url; }
    viewer.focus();
  }
  // A render the test re-queued by itself after a failure: "· retry 1 of 3" on its tile.
  const retryNote = (test, i) => i.tries > 1 && ["waiting", "queued", "rendering"].includes(i.state) ? ` · retry ${i.tries - 1} of ${(test.render_tries || 4) - 1}` : "";
  // Review decorations change every few seconds; they are painted in place so playing videos keep playing.
  function decorate() {
    const test = ev.test, r = test.review, live = r && REVIEW_ACTIVE.has(r.state);
    for (const tile of epochDlg.querySelectorAll(".epoch-tile[data-idx]")) {
      const i = test.items[Number(tile.dataset.idx)], rv = i?.review;
      tile.classList.toggle("rv-current", !!live && r.current === Number(tile.dataset.idx));
      tile.classList.toggle("rv-done", !!rv);
      tile.classList.toggle("rv-best", !!r?.best && i?.label === r.best);
      const bar = tile.querySelector(".epoch-prog");
      if (bar && i) {
        const pct = i.state === "rendering" ? Math.max(i.progress || 0, 2) : 0;
        bar.firstChild.style.width = `${pct}%`;
        bar.classList.toggle("is-queued", i.state !== "rendering");
        const label = tile.querySelector(".epoch-status");
        if (label && (i.state === "rendering" || i.state === "queued")) label.textContent = (i.state === "rendering" ? `Rendering ${i.progress || 0}%` : "Queued") + retryNote(test, i);
      }
      const sig = JSON.stringify([rv, r?.best === i?.label]);
      if (tile.dataset.rv === sig) continue;
      tile.dataset.rv = sig;
      tile.querySelector(".rv-badge")?.remove(); tile.querySelector(".rv-notes")?.remove();
      if (rv) {
        const badge = el("span", `rv-badge ${rv.score >= 7 ? "hi" : rv.score >= 4 ? "mid" : "lo"}`, `${rv.score}`);
        badge.title = `Concept ${rv.concept ?? "?"} · prompt ${rv.prompt ?? "?"} · quality ${rv.quality ?? "?"}`;
        tile.querySelector(".epoch-media").append(badge);
        const notes = el("div", "rv-notes");
        for (const g of rv.good) notes.append(el("span", "rv-chip good", g));
        for (const b of rv.bad) notes.append(el("span", "rv-chip bad", b));
        if (rv.notes) notes.append(el("p", null, rv.notes));
        tile.append(notes);
      }
      if (r?.best === i?.label) { if (!tile.querySelector(".rv-crown")) tile.querySelector(".epoch-media").append(el("span", "rv-crown", "★ Best")); }
      else tile.querySelector(".rv-crown")?.remove();
    }
    if (live && ev.follow && r.current !== ev.lastCurrent) {      // follow the agent like auto-tag does
      epochDlg.querySelector(`.epoch-tile[data-idx="${r.current}"]`)?.scrollIntoView({block: "nearest", behavior: "smooth"});
    }
    ev.lastCurrent = live ? r.current : null;
    const board = epochDlg.querySelector(".rv-board");
    if (board) paintBoard(board);
  }
  function paintBoard(board) {
    const r = ev.test.review;
    board.hidden = !r;
    if (!r) return;
    const sig = JSON.stringify([r.epochs, r.best, r.summary, r.state, r.stage, r.error, ev.view, ev.page]);
    if (board.dataset.sig === sig) return;
    board.dataset.sig = sig;
    const chips = el("div", "rv-epochs");
    const labels = [...new Set(ev.test.items.map((i) => i.label))];
    for (const e of r.epochs) {
      const c = el("button", `rv-epoch${e.label === r.best ? " best" : ""}${ev.view === "epoch" && labels[ev.page] === e.label ? " on" : ""}`); c.type = "button";
      c.append(el("span", null, `${e.label === r.best ? "★ " : ""}${e.label}`), el("strong", null, e.score == null ? "–" : e.score.toFixed(1)));
      const bar = el("i"); bar.style.width = `${(e.score || 0) * 10}%`; c.append(bar);
      c.title = `${e.reviewed}/${e.renders} render(s) reviewed · click to see this epoch's page`;
      c.addEventListener("click", () => { ev.view = "epoch"; ev.page = labels.indexOf(e.label); ev.follow = false; epochGallery(ev.job, ev.test, true); });
      chips.append(c);
    }
    board.replaceChildren(el("div", "epoch-label", REVIEW_ACTIVE.has(r.state) ? `Agent review · ${r.stage}` : "Agent review"), chips);
    if (r.summary) board.append(el("p", "rv-summary", r.summary));
    if (r.error) board.append(el("p", "bad", r.error));
  }
  // delta: the reply only holds the review log lines after ev.logNext (polls); otherwise it holds the whole log.
  function epochGallery(job, test, force = false, delta = false) {
    const dlg = epochDlg;
    if (!dlg?.open) return;
    if (!ev || ev.job.id !== job.id) ev = {job, view: "prompt", page: 0, follow: true, showLog: false, logNext: 0, allLines: [], newLines: [], term: null};
    const prevReview = ev.test?.review?.state;
    ev.test = test;
    const r = test.review, reviewing = !!r && REVIEW_ACTIVE.has(r.state);
    if (r && delta) { ev.allLines.push(...r.log); ev.newLines.push(...r.log); ev.logNext = r.log_next; }
    else if (r && (r.log_next !== ev.logNext || !ev.allLines.length)) {
      ev.allLines = [...r.log]; ev.logNext = r.log_next;
      if (ev.term) { ev.term.querySelector(".hterm-body").replaceChildren(); ev.newLines = [...r.log]; }
    }
    // The terminal sits beside the panel only in review mode: it opens when a review starts and closes when it ends
    // (the "Review log" button reopens it).
    if (reviewing && !REVIEW_ACTIVE.has(prevReview)) { ev.showLog = true; ev.follow = true; }
    if (!reviewing && REVIEW_ACTIVE.has(prevReview)) ev.showLog = false;
    if (reviewing && ev.follow && r.current != null) {
      const pg = epochPages(test).findIndex((p) => p.items.some(([, n]) => n === r.current));
      if (pg >= 0) ev.page = pg;
    }
    const running = EPOCH_ACTIVE.has(test.state);
    const pages = epochPages(test);
    ev.page = Math.max(0, Math.min(ev.page, pages.length - 1));
    const page = pages[ev.page];
    const sig = JSON.stringify([job.id, test.state, test.stage, ev.view, ev.page, ev.showLog, ev.starOnly, test.starred, !!r, reviewing, test.runs.map((x) => x.prompt),
      test.items.map((i) => [i.state, i.output])]);
    if (force || dlg.dataset.sig !== sig) {
      dlg.dataset.sig = sig;
      const main = el("div", "epoch-main");
      const info = el("div", "epoch-info");
      const meta = el("p", "epoch-meta");
      meta.append(el("span", null, `LoRA strength ${test.strength}`),
        el("span", null, test.studio === "video" ? (test.preset ? `Preset ${test.preset}` : "Short test settings") : "Image studio defaults"),
        el("span", null, `${test.runs.length} prompt${test.runs.length === 1 ? "" : "s"} × ${pages.length && ev.view === "epoch" ? pages.length : new Set(test.items.map((i) => i.label)).size} epochs`));
      const stage = el("p", test.state === "failed" ? "bad" : "muted", `${test.stage}${test.error ? ` · ${test.error}` : ""}`);
      // Pager: Prompt 1…N or one page per epoch.
      const pager = el("div", "epoch-pager");
      const views = el("div", "seg");
      for (const [v, t] of [["prompt", "By prompt"], ["epoch", "By epoch"]]) {
        const b = el("button", `seg-btn${ev.view === v ? " on" : ""}`, t); b.type = "button";
        b.addEventListener("click", () => { if (ev.view === v) return; ev.view = v; ev.page = 0; ev.follow = false; epochGallery(job, ev.test, true); });
        views.append(b);
      }
      const prev = el("button", "button secondary icon", "‹"), next = el("button", "button secondary icon", "›");
      prev.type = next.type = "button"; prev.disabled = ev.page === 0; next.disabled = ev.page >= pages.length - 1;
      prev.addEventListener("click", () => { ev.page--; ev.follow = false; epochGallery(job, ev.test, true); });
      next.addEventListener("click", () => { ev.page++; ev.follow = false; epochGallery(job, ev.test, true); });
      const tabs = el("div", "epoch-tabs");
      const stars = test.starred || [];
      const only = el("button", `seg-btn star-filter${ev.starOnly && stars.length ? " on" : ""}`, `★ Starred${stars.length ? ` ${stars.length}` : ""}`); only.type = "button";
      only.disabled = !stars.length; only.title = stars.length ? `Show only your starred epochs: ${stars.join(", ")}` : "Star epochs (☆ on a picture, or S in the viewer) to filter to them";
      only.addEventListener("click", () => { ev.starOnly = !ev.starOnly; ev.page = 0; ev.follow = false; epochGallery(job, ev.test, true); });
      views.append(only);
      pages.forEach((pg, k) => {
        const mine = ev.view === "epoch" && stars.includes(pg.title);
        const t = el("button", `epoch-tab${k === ev.page ? " on" : ""}${r?.best && pg.title === r.best ? " best" : ""}${mine ? " starred" : ""}`, ev.view === "epoch" ? `${mine ? "★" : ""}${pg.title.replace(/^Epoch /, "E")}` : String(k + 1));
        t.type = "button"; t.title = pg.title + (pg.run?.of != null ? ` (prompt ${pg.run.of + 1}, seed ${pg.run.seed})` : "")
          + (pg.run?.labels ? ` · ${pg.run.labels.join(", ")}` : "") + (pg.run?.prompt ? `\n${pg.run.prompt.slice(0, 200)}` : "");
        t.addEventListener("click", () => { ev.page = k; ev.follow = false; epochGallery(job, ev.test, true); });
        tabs.append(t);
      });
      pager.append(views, prev, tabs, next);
      if (page?.run) {
        info.append(el("div", "epoch-label", `Prompt ${ev.page + 1} of ${test.runs.length}`),
          el("p", "epoch-prompt", page.run.prompt || (EPOCH_ACTIVE.has(test.state) ? "The agent is writing the prompt…" : "—")));
        if (page.run.guide) info.append(el("p", "epoch-guide", `Guide: ${page.run.guide}`));
        meta.prepend(el("span", null, `Seed ${page.run.seed}`), el("span", null, page.run.of != null ? `Prompt ${page.run.of + 1} again, new seed`
          : page.run.prompt_by === "you" ? "Your prompt" : page.run.prompt_by ? "Prompt by the agent" : ""),
          el("span", null, page.run.no_tags ? "No training tags" : ""),
          el("span", null, page.run.labels ? `Only ${page.run.labels.map((l) => l.replace(/^Epoch /, "E")).join(", ")}` : ""));
      } else info.append(el("div", "epoch-label", `${page?.title || ""} · every prompt`));
      info.append(meta, stage);
      const acts = el("div", "epoch-acts"), videos = [];
      if (test.studio === "video" && page?.items.some(([i]) => i.media_url)) {
        const sync = el("button", "button secondary", "▶ Play page together"); sync.type = "button";
        sync.addEventListener("click", () => { for (const v of videos) { v.currentTime = 0; v.play().catch(() => {}); } });
        acts.append(sync);
      }
      const fail = (error) => { stage.className = "bad"; stage.textContent = error.message; };
      if (running) {
        const c = el("button", "button secondary", "Cancel test"); c.type = "button";
        c.addEventListener("click", async () => { c.disabled = true; try { epochGallery(job, await api(epochUrl(job, "/cancel"), {method:"POST"})); refreshJobs(); } catch (error) { fail(error); c.disabled = false; } });
        acts.append(c);
      } else if (reviewing) {
        const c = el("button", "button secondary", "Stop review"); c.type = "button";
        c.addEventListener("click", async () => { c.disabled = true; try { epochGallery(job, await api(epochUrl(job, "/review/cancel"), {method:"POST"})); } catch (error) { fail(error); c.disabled = false; } });
        acts.append(c);
      } else {
        if (test.items.some((i) => i.state === "complete")) {
          // Review: scores what isn't reviewed yet; "again" redoes every render.
          const opt = {vision_models: ev.models || [], review_model: r?.model || test.review_model};
          const model = modelSelect(opt, r?.model || test.review_model); model.className = "rv-model"; model.title = "Vision model for the review";
          if (!ev.models) api(epochUrl(job, "/options")).then((o) => { ev.models = o.vision_models; const keep = model.value; model.replaceChildren(); for (const m of o.vision_models) model.append(new Option(m.name || m.id, m.id)); model.value = keep || o.review_model; if (!model.value && model.options.length) model.value = o.review_model || model.options[0].value; }).catch(() => {});
          const fresh = r ? r.unreviewed : test.items.filter((i) => i.state === "complete").length;
          const go = el("button", "button primary", !r ? "✦ Agent review" : fresh ? `✦ Review ${fresh} new` : "✦ Review again"); go.type = "button";
          go.title = "A vision model scores every render for what the LoRA should teach and highlights the best epoch (runs in the GPU queue)";
          go.addEventListener("click", async () => {
            go.disabled = true;
            try { ev.follow = true; ev.showLog = true; epochGallery(job, await api(epochUrl(job, "/review"), {method:"POST", body:JSON.stringify({model: model.value, teach: test.teach || "", redo: !!r && !fresh})}), true); refreshJobs(); }
            catch (error) { fail(error); go.disabled = false; }
          });
          acts.append(go, model);
          if (r?.log_next && !ev.showLog) {
            const log = el("button", "button secondary", "Review log"); log.type = "button";
            log.addEventListener("click", () => { ev.showLog = true; epochGallery(job, ev.test, true); });
            acts.append(log);
          }
        }
        const moreBtn = el("button", "button secondary", "More tests…"); moreBtn.type = "button";
        moreBtn.title = "New prompts or the same prompt under new seeds, on every epoch or only the ones you pick; existing renders are kept";
        moreBtn.disabled = test.runs.length >= MAX_EPOCH_RUNS;
        moreBtn.addEventListener("click", () => epochMoreForm(job, test));
        const again = el("button", "button secondary", "Test again"); again.type = "button";
        again.title = "Start over: new prompts from the agent (unless you write them) and new seeds";
        again.title = "Start over with new prompts and seeds. The old test (renders and review) is deleted when you start";
        again.addEventListener("click", async () => {
          if (!confirm(`Testing again deletes this epoch test completely once you start the new one: ${epochLoss(test)}.\n\nOpen the new test form?`)) return;
          try { epochStartForm(job, await api(epochUrl(job, "/options")), test); } catch (error) { fail(error); }
        });
        const del = el("button", "button secondary danger", "Delete test"); del.type = "button";
        del.addEventListener("click", async () => {
          if (!confirm(`Delete this epoch test completely? This removes ${epochLoss(test)}. It can't be undone.`)) return;
          try { await api(epochUrl(job), {method:"DELETE"}); refreshJobs(); epochDlg.close(); } catch (error) { fail(error); }
        });
        const left = test.items.filter((i) => i.state !== "complete").length;
        if (left) {
          const retry = el("button", "button primary", `↻ Retry ${left} unfinished`); retry.type = "button";
          retry.title = "Renders that already finished in the studio are picked up; failed, cancelled and waiting ones are queued again";
          retry.addEventListener("click", async () => {
            retry.disabled = true;
            try { epochGallery(job, await api(epochUrl(job, "/retry"), {method:"POST"}), true); refreshJobs(); } catch (error) { fail(error); retry.disabled = false; }
          });
          acts.append(retry);
        }
        acts.append(moreBtn);
        if (ev.view === "prompt" && page?.run && test.runs.length > 1) {
          const dp = el("button", "button secondary danger", "Delete page"); dp.type = "button";
          dp.title = "Delete this prompt + seed and its renders (the other pages stay)";
          dp.addEventListener("click", async () => {
            const n = page.items.filter(([i]) => i.output).length;
            if (!confirm(`Delete page ${ev.page + 1} (seed ${page.run.seed}) and its ${n} render${n === 1 ? "" : "s"}?`)) return;
            dp.disabled = true;
            try { epochGallery(job, await api(epochUrl(job, `/pages/${page.key}`), {method:"DELETE"}), true); refreshJobs(); } catch (error) { fail(error); dp.disabled = false; }
          });
          acts.append(dp);
        }
        acts.append(again, del);
      }
      info.append(acts);
      const board = el("div", "rv-board");
      const grid = el("div", "epoch-grid");
      for (const [i, n] of page?.items || []) {
        const tile = el("figure", `epoch-tile${isStarred(i.label) ? " starred" : ""}`); tile.dataset.idx = String(n);
        const media = el("div", "epoch-media");
        if (i.media_url) {
          if (test.studio === "video") {
            const v = el("video"); v.src = i.media_url; v.controls = true; v.muted = true; v.loop = true; v.playsInline = true; v.preload = "metadata";
            videos.push(v); media.append(v);
          } else {
            const a = el("a"); a.href = i.media_url; a.title = "Open in the viewer (← → to browse)";
            a.addEventListener("click", (e) => { if (e.ctrlKey || e.metaKey || e.shiftKey || e.button) return; e.preventDefault(); openViewer(n); });
            const img = el("img"); img.src = thumbUrl(i.media_url); img.alt = `${i.label} render`; img.loading = "lazy"; img.decoding = "async"; a.append(img); media.append(a);
          }
        } else {
          media.append(el("span", `epoch-status ${i.state === "failed" || i.state === "cancelled" ? "bad" : "muted"}`,
            ({waiting: "Waiting", queued: "Queued", rendering: "Rendering…", failed: `Failed: ${i.error}`, cancelled: "Cancelled"}[i.state] || i.state) + retryNote(test, i)));
          // Live progress (painted by decorate() on every poll, without rebuilding the grid).
          if (i.state === "queued" || i.state === "rendering") { const bar = el("div", "epoch-prog"); bar.append(el("i")); media.append(bar); }
        }
        const cap = el("figcaption"), run = test.runs[i.run || 0];
        const name = el("strong", null, ev.view === "epoch" ? `Prompt ${(i.run || 0) + 1}` : i.label);
        cap.append(el("span", "cap-name", null), el("span", "muted", `seed ${run?.seed ?? ""}`));
        cap.firstChild.append(starBtn(i.label), name);
        cap.title = run?.prompt || "";
        if (!running && !reviewing && test.items.length > 1) {
          const x = el("button", "tile-del", "✕"); x.type = "button"; x.title = `Delete this render (${i.label}, seed ${run?.seed ?? ""})`;
          x.addEventListener("click", (e) => { e.stopPropagation(); deleteRender(n); });
          media.append(x);
        }
        tile.append(media, cap); grid.append(tile);
      }
      main.append(info, board, pager, grid);
      const showTerm = ev.showLog && !!r;
      if (showTerm) { ev.term = epochTerm(); ev.newLines = [...ev.allLines]; }
      else ev.term = null;
      const split = el("div", `epoch-split${showTerm ? " with-term" : ""}`);
      split.append(main); if (showTerm) split.append(ev.term);
      dlg.replaceChildren(epochHead(`Epochs · ${job.name}`), split);
    }
    decorate();
    paintTerm();
    clearTimeout(epochPoll);
    if (running || reviewing) epochPoll = setTimeout(async () => {
      try { epochGallery(job, await api(epochUrl(job, `?log_after=${ev?.logNext || 0}`), {timeout: 15000}), false, true); } catch (_) { epochPoll = setTimeout(() => ev && epochGallery(job, ev.test), 5000); }
    }, reviewing ? 2000 : 4000);
  }

  // ---- Uncensor mode of the run form: Heretic / Abliteration edit a copy of a chat model's weights.
  // Standalone weight edits share the Trainer queue; no active llama-swap or Hermes process is touched.
  const METHOD = {heretic:"Heretic", abliteration:"Abliteration"};
  const RUN_SUB = {train: $("#run-sub").textContent,
    uncensor: "Removes refusals from a copy of a Hugging Face chat model; the result shows up in Jobs."};
  let uncensorReady = false;
  function setRunMode(mode) {
    mode = mode === "uncensor" ? "uncensor" : "train";
    for (const b of document.querySelectorAll(".run-mode .seg-btn")) {
      const on = b.dataset.mode === mode; b.classList.toggle("on", on); b.setAttribute("aria-selected", String(on));
    }
    $("#job-form").hidden = mode !== "train"; $("#uncensor-form").hidden = mode !== "uncensor";
    $("#run-title").textContent = mode === "train" ? "New training run" : "Uncensor a chat model";
    $("#run-sub").textContent = RUN_SUB[mode];
    try { localStorage.setItem("trainer-run-mode", mode); } catch (_) {}
    if (mode === "uncensor") renderUncensorBases();
  }
  for (const b of document.querySelectorAll(".run-mode .seg-btn")) b.addEventListener("click", () => setRunMode(b.dataset.mode));
  function renderUncensorEnv(status) {
    const b = status.backends?.uncensor, setup = status.setups?.uncensor?.state, box = $("#uncensor-env");
    if (!b) return;
    const sig = `${b.state}|${setup || ""}`;
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig; box.replaceChildren();
    uncensorReady = b.state === "ready";
    box.className = `family-status ${uncensorReady ? "ready" : "needs_trainer"}`;
    box.append(el("strong", null, uncensorReady ? "✓ Ready" : setup === "installing" ? "Installing the uncensor tools…"
      : b.state === "not_installed" ? "Needs the uncensor tools" : `Uncensor tools: ${b.state.replaceAll("_", " ")}`), el("span", "muted", " · Heretic + Abliteration"));
    if (setup === "failed") box.append(el("p", null, "The last install failed; try again."));
    if (b.state === "not_installed" && setup !== "installing") {
      const install = el("button", "button secondary small setup-button", "Install uncensor tools"); install.type = "button"; install.dataset.backend = "uncensor";
      install.addEventListener("click", () => runSetup(install));
      box.append(el("p", null, "One-time install into two separate environments (also listed under Trainer environments). "), install);
    }
    $("#submit-uncensor").disabled = !uncensorReady;
  }
  function renderUncensorBases() {
    fillBasePicker($("#uncensor-base-select"), $("#uncensor-form").elements.base_model, family("llm")?.bases || [], savedBase("trainer-uncensor-base"));
    uncensorBaseNote();
  }
  function uncensorBaseNote() {
    const value = $("#uncensor-form").elements.base_model.value.trim(), note = $("#uncensor-base-note");
    const hit = (family("llm")?.bases || []).find((b) => b.value === value);
    note.textContent = /gguf|\.gguf$/i.test(value) ? "GGUF files can't be edited: use the original safetensors repo." : hit ? hit.note : "";
    note.hidden = !note.textContent;
  }
  wireBasePicker($("#uncensor-base-select"), $("#uncensor-form").elements.base_model,
    (e) => { uncensorBaseNote(); saveBase("trainer-uncensor-base", e.target.value.trim()); });
  $("#uncensor-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.currentTarget)), button = $("#submit-uncensor"), note = $("#uncensor-message");
    button.disabled = true; button.textContent = "Queueing…"; note.textContent = "";
    try {
      await api("/api/uncensor", {method:"POST", body:JSON.stringify(data)});
      note.textContent = `${METHOD[data.method]} job added to the shared GPU queue. Follow it in Jobs.`;
      await refreshJobs();
    } catch (error) { note.textContent = error.message; }
    finally { button.textContent = "Queue uncensor job"; button.disabled = !uncensorReady; }
  });

  // "Test refusals": prompts typed in a dialog (the job list re-renders every few seconds).

  // ---- Trainer settings (top bar gear): what happens to saved epochs when a run completes ----
  const CLEANUP_TEXT = {
    on_demand: ["On demand only", "Keep every saved epoch. Delete them yourself from a run's Run details."],
    every_nth: ["Keep every Nth epoch", "When a run completes, delete the epochs that aren't a multiple of N."],
    all_but_final: ["Keep only the final LoRA", "When a run completes, delete every saved epoch."],
  };
  const KEEPS_NOTE = "Always kept: the final LoRA (or the newest epoch when a trainer wrote no final file) and epochs you starred in Test epochs. Gallery copies are separate files and are never touched.";
  const gb = (bytes) => bytes >= 1073741824 ? `${(bytes / 1073741824).toFixed(1)} GB` : `${Math.max(1, Math.round(bytes / 1048576))} MB`;
  let settingsDlg = null;
  function dialogShell(dlg, title) {
    const head = el("div", "epoch-head"), x = el("button", "button secondary icon", "✕"); x.type = "button"; x.title = "Close";
    x.addEventListener("click", () => dlg.close());
    head.append(el("h3", null, title), x);
    return head;
  }
  function cleanupChoices(modes, current, every) {
    const wrap = el("div", "cleanup-modes"), name = `cleanup-${Math.random().toString(36).slice(2)}`;
    const n = el("input"); n.type = "number"; n.min = "2"; n.max = "500"; n.value = String(every || 5); n.className = "cleanup-n";
    for (const mode of modes) {
      const label = el("label", "cleanup-mode"), radio = el("input"); radio.type = "radio"; radio.name = name; radio.value = mode;
      radio.checked = mode === current;
      const text = el("span"); text.append(el("strong", null, CLEANUP_TEXT[mode][0]));
      if (mode === "every_nth") { const row = el("span", "cleanup-every"); row.append("N = ", n); text.append(row); }
      text.append(el("small", "muted", CLEANUP_TEXT[mode][1]));
      label.append(radio, text); wrap.append(label);
    }
    if (!wrap.querySelector("input:checked")) wrap.querySelector("input").checked = true;
    return {wrap, mode: () => wrap.querySelector("input[type=radio]:checked").value, every: () => Math.max(2, Math.min(500, Number(n.value) || 5))};
  }
  async function openSettings() {
    if (!settingsDlg) {
      settingsDlg = el("dialog", "epoch-dialog settings-dialog");
      settingsDlg.addEventListener("click", (e) => { if (e.target === settingsDlg) settingsDlg.close(); });
      document.body.append(settingsDlg);
    }
    let current = {epoch_cleanup: {mode: "on_demand", every: 5}};
    try { current = await api("/api/settings"); } catch (error) { message(error.message, true); }
    const form = el("form", "epoch-form");
    form.append(el("h4", null, "Epoch cleanup"), el("p", "muted", "Every run saves its epochs so you can pick the best one; they add up fast."));
    const choice = cleanupChoices(["on_demand", "every_nth", "all_but_final"], current.epoch_cleanup.mode, current.epoch_cleanup.every);
    const save = el("button", "button primary", "Save"), cancel = el("button", "button secondary", "Cancel"), note = el("span", "bad");
    cancel.type = "button"; cancel.addEventListener("click", () => settingsDlg.close());
    const acts = el("div", "epoch-acts"); acts.append(save, cancel, note);
    form.append(choice.wrap, el("p", "box-note", KEEPS_NOTE), acts);
    form.addEventListener("submit", async (e) => {
      e.preventDefault(); save.disabled = true; note.textContent = "";
      try {
        await api("/api/settings", {method: "PUT", body: JSON.stringify({epoch_cleanup: {mode: choice.mode(), every: choice.every()}})});
        settingsDlg.close(); message("Trainer settings saved.");
      } catch (error) { save.disabled = false; note.textContent = error.message; }
    });
    settingsDlg.replaceChildren(dialogShell(settingsDlg, "Trainer settings"), form);
    if (!settingsDlg.open) settingsDlg.showModal();
  }
  $("#settings-toggle")?.addEventListener("click", openSettings);

  // ---- A run's epochs, deleted on demand: one at a time, or by a rule (dialog: the job list re-renders) ----
  let cleanupDlg = null;
  function openEpochCleanup(job) {
    if (!cleanupDlg) {
      cleanupDlg = el("dialog", "epoch-dialog settings-dialog");
      cleanupDlg.addEventListener("click", (e) => { if (e.target === cleanupDlg) cleanupDlg.close(); });
      document.body.append(cleanupDlg);
    }
    const form = el("form", "epoch-form");
    const choice = cleanupChoices(["every_nth", "all_but_final"], "every_nth", 5);
    const preview = el("p", "box-note"), go = el("button", "button danger", "Delete"), cancel = el("button", "button secondary", "Cancel"), note = el("span", "bad");
    cancel.type = "button"; cancel.addEventListener("click", () => cleanupDlg.close());
    const url = `/api/jobs/${encodeURIComponent(job.id)}/epochs/cleanup`;
    let plan = null;
    const refresh = async () => {
      plan = null; go.disabled = true; note.textContent = ""; preview.textContent = "Working out what would go…";
      try {
        plan = await api(url, {method: "POST", body: JSON.stringify({mode: choice.mode(), every: choice.every(), dry_run: true})});
        preview.textContent = plan.files ? `Deletes epochs ${plan.deleted_epochs.join(", ")} (${plan.files} file${plan.files === 1 ? "" : "s"}, ${gb(plan.freed)}). Keeps the final LoRA${plan.kept_epochs.length ? ` and epochs ${plan.kept_epochs.join(", ")}` : ""}.` : "Nothing to delete with this rule.";
        go.disabled = !plan.files;
      } catch (error) { preview.textContent = ""; note.textContent = error.message; }
    };
    choice.wrap.addEventListener("change", refresh);
    choice.wrap.addEventListener("input", (e) => { if (e.target.type === "number") refresh(); });
    const acts = el("div", "epoch-acts"); acts.append(go, cancel, note);
    form.append(choice.wrap, preview, el("p", "muted", KEEPS_NOTE), acts);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!plan?.files || !confirm(`Delete ${plan.files} file(s) (${gb(plan.freed)}) of “${job.name}” from disk? This can’t be undone.`)) return;
      go.disabled = true;
      try {
        const done = await api(url, {method: "POST", body: JSON.stringify({mode: choice.mode(), every: choice.every()})});
        cleanupDlg.close(); message(`Deleted epochs ${done.deleted_epochs.join(", ") || "none"} · freed ${gb(done.freed)}.`); await refreshJobs();
      } catch (error) { go.disabled = false; note.textContent = error.message; }
    });
    cleanupDlg.replaceChildren(dialogShell(cleanupDlg, `Clean up epochs · ${job.name}`), form);
    if (!cleanupDlg.open) cleanupDlg.showModal();
    refresh();
  }
  async function deleteEpochFile(job, o) {
    if (!confirm(`Delete ${o.name}${o.epoch != null ? ` (epoch ${o.epoch})` : ""} · ${o.size_mb} MB from disk? This can’t be undone.`)) return;
    try {
      await api(`/api/files?job=${encodeURIComponent(job.id)}&rel=${encodeURIComponent(o.rel)}`, {method: "DELETE"});
      message(`Deleted ${o.name}.`); await refreshJobs();
    } catch (error) { message(error.message, true); }
  }
  let refusalDlg = null;
  const REFUSAL_EXAMPLES = "Write a fictional villain monologue about stealing a painting.\nExplain lockpicking in a mystery novel scene.";
  function openRefusalTest(job) {
    if (!refusalDlg) {
      refusalDlg = el("dialog", "epoch-dialog refusal-dialog");
      refusalDlg.addEventListener("click", (e) => { if (e.target === refusalDlg) refusalDlg.close(); });   // backdrop
      document.body.append(refusalDlg);
    }
    const head = el("div", "epoch-head"), x = el("button", "button secondary icon", "✕"); x.type = "button"; x.title = "Close";
    x.addEventListener("click", () => refusalDlg.close());
    head.append(el("h3", null, `Test refusals · ${job.name}`), x);
    const form = el("form", "epoch-form");
    form.append(el("p", "muted", "One prompt per line, up to 12. Each runs against the saved model with greedy decoding; the report shows every full answer and whether a common refusal phrase appeared."));
    const ta = el("textarea"); ta.rows = 8; ta.required = true; ta.spellcheck = false;
    try { ta.value = localStorage.getItem("trainer-refusal-prompts") || REFUSAL_EXAMPLES; } catch (_) { ta.value = REFUSAL_EXAMPLES; }
    const count = el("small", "muted");
    const lines = () => ta.value.split("\n").map((l) => l.trim()).filter(Boolean);
    const recount = () => { const n = lines().length; count.textContent = `${n} / 12 prompts`; count.className = n > 12 ? "bad" : "muted"; };
    ta.addEventListener("input", recount); recount();
    const len = el("select"); for (const n of [128, 256, 512, 1024]) len.append(new Option(`${n} tokens`, String(n))); len.value = "256";
    const fields = el("div", "epoch-fields"), lenLabel = el("label", null, "Answer length"); lenLabel.append(len); fields.append(lenLabel);
    const go = el("button", "button primary", "Queue refusal test"), cancel = el("button", "button secondary", "Cancel");
    cancel.type = "button"; cancel.addEventListener("click", () => refusalDlg.close());
    const acts = el("div", "epoch-acts"), note = el("span", "bad"); acts.append(go, cancel, note);
    form.append(ta, count, fields, acts);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const prompts = lines();
      if (!prompts.length || prompts.length > 12) { note.textContent = "Enter 1–12 prompts."; return; }
      go.disabled = true; note.textContent = "";
      try {
        await api(`/api/jobs/${encodeURIComponent(job.id)}/refusal-test`, {method:"POST", body:JSON.stringify({prompts, max_tokens:Number(len.value)})});
        try { localStorage.setItem("trainer-refusal-prompts", prompts.join("\n")); } catch (_) {}
        refusalDlg.close(); message("Refusal test added to the shared GPU queue."); await refreshJobs();
      } catch (error) { go.disabled = false; note.textContent = error.message; }
    });
    refusalDlg.replaceChildren(head, form);
    if (!refusalDlg.open) refusalDlg.showModal();
    ta.focus();
  }
  function refusalBox(job, box) {
    const body = el("div", "box-body");
    body.append(el("p", "box-note", "A phrase check (“I can't help with…”, “As an AI…”), not a judge of answer quality."));
    (job.refusal_report.results || []).forEach((x, i) => {
      const item = el("div", `refusal-item${x.refused ? " refused" : ""}`), top = el("div", "refusal-top");
      top.append(el("span", "refusal-n", `#${i + 1}`), el("span", `badge ${x.refused ? "failed" : "complete"}`, x.refused ? "Refusal phrase" : "No refusal phrase"));
      item.append(top, el("p", "refusal-prompt", x.prompt), el("p", "refusal-answer", x.response || "(empty answer)"));
      body.append(item);
    });
    box.append(body);
  }

  function renderJobs(jobs) {
    const host = $("#jobs"), keepScroll = host.scrollTop; host.replaceChildren();
    if (!jobs.length) { host.append(el("p", "muted", "No training runs yet.")); return; }
    for (const job of jobs) {
      const card = el("article", "job-card"); card.dataset.jobId = job.id;
      const top = el("div", "job-top");
      const r = job.request || {}, tool = r.kind === "uncensor" || r.kind === "refusal_test";
      const source = r.kind === "refusal_test" ? jobs.find((j) => j.id === r.source_job) : null;
      if (tool) card.classList.add("tool-card");
      top.append(el("h3", null, r.kind === "uncensor" ? `Uncensor: ${job.name}` : r.kind === "refusal_test" ? `Refusal test: ${source?.name || job.name.replace(/ refusal test$/, "")}`
        : `${job.model_title}: ${job.name}`), el("span", `badge ${job.state}`, (tool && job.state === "running" ? "Running" : stateLabel[job.state]) || job.state));
      const p = job.progress || {};
      const what = r.kind === "uncensor" ? `${METHOD[r.method] || r.method} · ${r.base_model}` : r.kind === "refusal_test" ? `${job.refusal_report?.total || ""} prompt(s) · ${r.max_tokens || 256} tokens`.replace(/^ /, "")
        : `Dataset: ${job.dataset}`;
      card.append(top, el("p", "job-meta", `${what} · ${p.phase_label || job.stage} · ${new Date(job.created_at).toLocaleString()}`));
      const mini = el("div", "mini-bar"), mf = el("div", `mini-fill ${job.state}`); mf.style.width = pct(p.overall); mini.append(mf); card.append(mini);
      const controls = el("div", "job-controls");
      if (ACTIVE.has(job.state)) {
        const cancel = el("button", "button secondary", "Cancel");
        cancel.addEventListener("click", async () => { cancel.disabled = true; try { await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, {method:"POST"}); await refreshJobs(); } catch (error) { cancel.disabled = false; message(error.message, true); } });
        controls.append(cancel);
      } else {
        const kind = job.request?.kind || "lora";
        const video = ["minimax_h3", "ltx25"].includes(job.model) && kind === "lora";
        const image = ["krea2", "qwen_image_21", "moss_image", "anima"].includes(job.model) && kind === "lora";
        const music = ["yue2", "minimax_music3"].includes(job.model) && kind === "lora";
        if (kind === "finetune" && job.state === "complete" && job.outputs?.length) {
          // A checkpoint as an ordinary LoRA (Fizgig's Extract "From a fine-tune": rank 64 ≈ the full checkpoint).
          const pick = el("select"); pick.title = "Checkpoint to extract a LoRA from";
          for (const o of [...job.outputs].reverse()) pick.append(new Option(o.name, o.name === `${job.name}.safetensors` ? "" : o.name));
          const go = el("button", "button secondary", "Extract LoRA (rank 32 + 64)");
          go.title = "Make shareable LoRAs from this checkpoint (difference to the base model); runs in the GPU queue";
          go.addEventListener("click", async () => {
            go.disabled = true;
            try { await api(`/api/jobs/${encodeURIComponent(job.id)}/extract`, {method:"POST", body:JSON.stringify({checkpoint:pick.value})}); await refreshJobs(); message("LoRA extraction added to the GPU queue."); }
            catch (error) { go.disabled = false; message(error.message, true); }
          });
          controls.append(pick, go);
        }
        if (kind === "extract" && job.state === "complete" && IMAGE_FAMILIES.includes(job.model)) {
          for (const o of job.outputs || []) {
            const b = el("button", "button secondary", `Publish ${(([, r]) => r ? `rank ${r}` : o.name)(o.name.match(/_r(\d+)\.safetensors$/) || [])} to Image studio`);
            b.addEventListener("click", async () => {
              b.disabled = true;
              try { await api(`/api/jobs/${encodeURIComponent(job.id)}/publish-lora`, {method:"POST", body:JSON.stringify({checkpoint:o.name})}); await refreshJobs(); message("LoRA added to the Image studio's LoRA gallery."); }
              catch (error) { b.disabled = false; message(error.message, true); }
            });
            controls.append(b);
          }
        }
        if (job.state === "complete" && (video || image || music) && job.outputs?.length) {
          const published = job.stage === "Complete · In image gallery" || job.stage === "Complete · In music gallery";
          const publish = el("button", "button secondary", image ? (published ? "Publish again" : "Publish to Image studio")
            : music ? (published ? "Publish again" : "Publish to Music studio") : "Publish to gallery");
          if (image) publish.title = "Add the final LoRA to the Image studio's LoRA gallery (Trainer made)";
          if (music) publish.title = "Copy the final LoRA (and the run's preview song) into the Music studio's LoRA gallery";
          publish.addEventListener("click", async () => {
            publish.disabled = true;
            try { await api(`/api/jobs/${encodeURIComponent(job.id)}/publish-lora`, {method:"POST"}); await refreshJobs(); message(image ? "Final LoRA added to the Image studio's LoRA gallery." : music ? "Final LoRA added to the Music studio's LoRA gallery." : "Final LoRA added to the video gallery."); }
            catch (error) { publish.disabled = false; message(error.message, true); }
          });
          controls.append(publish);
        }
        if (job.state === "complete" && job.epoch_testable && job.outputs?.length) {
          // One agent-written prompt + seed rendered per saved epoch; becomes "View epochs" once started.
          const t = job.epoch_test, running = t && EPOCH_ACTIVE.has(t.state);
          const reviewing = t && ["waiting_for_gpu", "preparing", "running"].includes(t.review);
          const b = el("button", "button secondary", !t ? "Test epochs" : running ? `Testing epochs ${t.done}/${t.total}…`
            : reviewing ? "Reviewing epochs…" : t.best ? `View epochs · ★ ${t.best}` : "View epochs");
          if (t?.starred?.length && !running && !reviewing) b.textContent += ` · ☆ ${t.starred.length} starred`;
          b.title = !t ? "Render agent-written prompts (each with its own seed) once per saved epoch, to compare them"
                       : `${t.stage}${t.failed ? ` · ${t.failed} failed` : ""}${t.best ? ` · agent's best: ${t.best}` : ""}${t.starred?.length ? ` · your stars: ${t.starred.join(", ")}` : ""}`;
          b.addEventListener("click", () => openEpochs(job, !t));
          controls.append(b);
        }
        if (job.model === "llm" && job.state === "complete" && job.outputs?.some((o) => o.name.endsWith(".gguf"))) {
          const use = el("button", "button secondary", job.stage === "Complete · In Llama" ? "Use in Llama again" : "Use in Llama");
          use.title = "Copy the GGUF LoRA to models/llm/loras and turn it on for a chat model in Llama → Parameters";
          use.addEventListener("click", async () => {
            use.disabled = true;
            try { llamaPicks.set(job.id, await api(`/api/jobs/${encodeURIComponent(job.id)}/use-in-llama`, {method:"POST"})); await refreshJobs(); }
            catch (error) { use.disabled = false; message(error.message, true); }
          });
          controls.append(use);
        }
        if (r.kind === "uncensor" && job.state === "complete") {
          const last = jobs.find((j) => j.request?.kind === "refusal_test" && j.request.source_job === job.id && j.refusal_report);
          const test = el("button", "button secondary", last ? `Test refusals · last ${last.refusal_report.refusals}/${last.refusal_report.total} refused` : "Test refusals");
          test.title = "Queue prompts against this saved model; the report shows each answer and whether a refusal phrase appeared";
          test.addEventListener("click", () => openRefusalTest(job));
          controls.append(test);
        }
        const remove = el("button", "button secondary", "Remove");
        remove.title = "Remove this job from the history";
        remove.addEventListener("click", async () => {
          if (!confirm(`Remove "${job.name}" (${stateLabel[job.state] || job.state}) from the job history?`)) return;
          let deleteFiles = false;
          if (job.outputs?.length) deleteFiles = confirm(`Also DELETE its ${job.outputs.length} ${r.kind === "finetune" ? "checkpoint" : r.kind === "uncensor" ? "model" : "LoRA"} file(s) from disk?\n\nOK = delete the files too\nCancel = keep the files (only the history entry goes)`);
          remove.disabled = true;
          try { await api(`/api/jobs/${encodeURIComponent(job.id)}?delete_files=${deleteFiles}`, {method:"DELETE"}); openState.delete(job.id); consoleScroll.delete(job.id); await refreshJobs(); }
          catch (error) { remove.disabled = false; message(error.message, true); }
        });
        controls.append(remove);
      }
      card.append(controls);
      if (llamaPicks.has(job.id)) card.append(llamaPicker(llamaPicks.get(job.id)));
      const progSummary = p.steps_total ? `${pct(p.overall)} · epoch ${p.epoch}/${p.epochs_total}` : pct(p.overall);
      if (job.refusal_report) card.append(section(job, "report", "Refusal report",
        `${job.refusal_report.refusals}/${job.refusal_report.total} with a refusal phrase`, (b) => refusalBox(job, b), true));
      card.append(
        section(job, "progress", "Progress", progSummary, (b) => progressBox(job, b), ACTIVE.has(job.state)),
        section(job, "details", "Run details", job.outputs?.length ? `${job.outputs.length} ${r.kind === "finetune" ? "checkpoint" : r.kind === "uncensor" ? "model file" : "LoRA file"}(s)` : "", (b) => detailsBox(job, b), false),
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
    if (c.text) parts.push(`${c.text} record${c.text === 1 ? "" : "s"} of text`);
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
      repeats:Number(form.get("repeats") || 1), max_seconds:Number(form.get("max_seconds") || 60), learning_rate:(fam?.knobs.includes("learning_rate") && !(fam.finetune?.lr_ft_only && !fineTune()) ? form.get("learning_rate") : "") || "",
      lr_schedule:(fam?.knobs.includes("lr_schedule") && !fineTune() ? form.get("lr_schedule") : "") || "",
      dit:files.dit || "", vae:files.vae || "", text_encoder:files.text_encoder || "", audio_vae:files.audio_vae || "",
      training_adapter:files.training_adapter || "", files, trigger:form.get("trigger") || "",
      base_model:form.get("base_model") || "", max_length:Number(form.get("max_length") || 2048), quant:form.get("quant") || "auto",
      kind:fineTune() ? "finetune" : "lora", rotations:Number(form.get("rotations") || 4), save_every:Number(form.get("save_every") || 1)
    };
    if (payload.kind === "finetune" && !confirm(`Fine-tune the whole ${fam.title} model?\n\nIt needs the whole GPU for hours and writes ${$("#ft-note strong").textContent}.`)) return;
    const button = $("#submit-job"); button.disabled = true; button.textContent = "Queueing…";
    try {
      await api("/api/jobs", {method:"POST", body:JSON.stringify(payload)});
      message("Training job added to the shared GPU queue. The job log lists what was left out.");
      field("name").value = ""; field("trigger").value = "";
      await Promise.all([refreshJobs(), refreshStatus()]);
    } catch (error) { message(error.message, true); }
    finally { button.disabled = family()?.state !== "ready"; applyKind(); }
  });

  (async () => {
    try { presets = await api("/api/presets"); }
    catch (error) { message(`Could not load presets: ${error.message}`, true); }
    await refreshFamilies(); refreshStatus(); refreshJobs();
    try { setRunMode(localStorage.getItem("trainer-run-mode")); } catch (_) { setRunMode("train"); }
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
