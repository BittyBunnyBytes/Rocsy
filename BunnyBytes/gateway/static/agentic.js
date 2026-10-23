// Rocsy Settings: Agentic tab - the Master Agent's model and powers (Agent service
// /Agent/api/config, saved on every change) and the Rules editor: the Master Agent
// brief plus the Hermes dataset rules that used to live in the Trainer. Permissions: which
// Hermes tools the agent may use (web, files, terminal, memory, ...) and risky-command approvals.
// Also fills the Compression tab's online backup summarizer picker - a placeholder for now (disabled, not saved).
(() => {
  const panel = document.getElementById("panel-agentic");
  if (!panel) return;
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const SWITCHES = [
    ["svc:Vid", "Video", "Make videos (MiniMax H3)"],
    ["svc:Krea", "Image", "Make images (Krea 2, Lustify, Qwen Image)"],
    ["svc:Mus", "Music", "Make songs"],
    ["svc:Voice", "Voice", "Make speech with a saved voice"],
    ["svc:Trainer", "Trainer", "Start LoRA training runs"],
    ["svc:Motion", "Motion", "Code-based videos: it writes Remotion (React) projects and renders them on the GPU"],
    ["use_agentic", "Use each studio's Agentic mode", "Where a studio has one (Vid: H3 Agentic sessions), the Master Agent hands the work to it, which plans the clips, reviews and fixes them"],
    ["priority", "Highest GPU priority", "Its jobs go to the front of the GPU queue (positions you locked stay put)"],
    ["may_cancel_others", "May cancel other jobs", "Allow it to cancel jobs it didn't start"],
    ["confirm_jobs", "Review before it uses a service", "Its plan and every prompt (video, image, music, voice, edits) wait on the Agent page until you approve, edit or veto them. /review in the Agent chat toggles it or picks what to check"],
    ["ask_services", "Ask before using each service", "The first time it uses a service in a session (video, image, music, voice, Motion, training, REA tests, restarting services) it asks you on the Agent page. Once you allow it, that service is cleared for the session, or for good with Always allow. /askfirst in the Agent chat"],
    ["gpu_guard", "GPU hand-off for local models", "Hold the GPU while a local model thinks, unload it before renders (keeps VRAM safe)"],
    ["cache_save", "Save the model's cache between turns", "Before a local model is unloaded, its memory of the conversation is saved to disk and restored after the reload, so it doesn't re-read the whole chat (Strata and llama.cpp)"],
    ["notify", "Notifications", "Tell you when a session finishes or fails"],
  ];
  let cfg = null, loaded = false, rules = [], current = null;

  async function call(url, options = {}) {
    const r = await fetch(url, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...options });
    if (r.status === 401) { location.href = "/login?next=/Settings%23agentic"; throw new Error("Log in again"); }
    let body = {}; try { body = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error(body.detail || body.message || body.error || `Request failed (${r.status})`);
    return body;
  }
  function status(id, text, kind = "") { const s = $(id); s.textContent = text; s.dataset.kind = kind; }

  /* ------------------------------------------------ config -- */
  async function save(patch) {
    status("agentic-status", "Saving…");
    try {
      cfg = await call("/Agent/api/config", { method: "PUT", body: JSON.stringify(patch) });
      status("agentic-status", "Saved", "ok");
    } catch (e) { status("agentic-status", e.message, "error"); }
  }

  function fillModel(options) {
    const select = $("agent-model");
    const hermes = options.hermes || {};
    const def = hermes.default || {};
    select.replaceChildren(new Option(`Hermes default (${def.model || "?"})`, "|"));
    for (const pr of hermes.providers || []) {
      const group = el("optgroup"); group.label = `${pr.name}${pr.local ? " · local GPU" : ""}`;
      for (const m of pr.models) group.append(new Option(m, `${pr.slug}|${m}`));
      select.append(group);
    }
    if (hermes.error) select.append(new Option(hermes.error, "", false, false));
    const value = cfg.model ? `${cfg.provider}|${cfg.model}` : "|";
    if (![...select.options].some((o) => o.value === value)) select.append(new Option(`${cfg.model} (${cfg.provider || "?"})`, value));
    select.value = value;
    // Placeholder: online (non-local) models only, not wired to Hermes yet.
    const backup = $("agent-compress-backup");
    backup.replaceChildren(new Option("Online backup · coming soon", ""));
    for (const pr of (hermes.providers || []).filter((x) => !x.local)) {
      const group = el("optgroup"); group.label = pr.name;
      for (const m of pr.models) group.append(new Option(m, `${pr.slug}|${m}`));
      backup.append(group);
    }
    backup.value = "";
    backup.disabled = true;
    $("agent-reasoning").replaceChildren(...(options.reasoning || ["default"]).map((r) => new Option(r === "default" ? "Model default" : r, r)));
    $("agent-reasoning").value = cfg.reasoning;
    const vp = $("agent-vid-preset");
    vp.replaceChildren(new Option("Vid defaults", ""), ...(options.vid_presets || []).map((n) => new Option(n, n)));
    if (cfg.vid_preset && ![...vp.options].some((o) => o.value === cfg.vid_preset)) vp.append(new Option(`${cfg.vid_preset} (missing)`, cfg.vid_preset));
    vp.value = cfg.vid_preset;
    const labels = {
      minimax_h3: "MiniMax H3", ltx_2_5: "LTX 2.5",
      qwen3: "Qwen3-TTS 1.7B", indextts2: "IndexTTS2", fish_s2: "Fish Audio S2 Pro", fish_v1: "Fish Audio 1.5",
      krea2: "Krea 2", lustify: "Lustify", qwen_image_21: "Qwen Image 2.1", moss: "MOSS-Image",
      yue2: "YuE2", minimax_music3: "MiniMax Music 3"
    };
    for (const kind of ["video", "voice", "image", "music"]) {
      const pick = $(`agent-${kind}-model`);
      pick.replaceChildren(...(options[`${kind}_models`] || []).map((id) => new Option(labels[id] || id, id)));
      pick.value = cfg[`${kind}_model`];
    }
    $("agent-vid-preset-field").hidden = cfg.video_model !== "minimax_h3";
    $("agent-max-jobs").value = cfg.max_jobs;
    $("agent-keep-loaded").value = String(cfg.keep_loaded_ask ?? 0);
  }

  function renderSwitches() {
    const host = $("agent-switches");
    host.replaceChildren(...SWITCHES.map(([key, name, note]) => {
      const row = el("label", "bb-switch-row");
      const text = el("span", "bb-module-text"); text.append(el("span", "bb-module-name", name), el("span", "bb-module-note", note));
      const box = el("input", "bb-switch"); box.type = "checkbox"; box.setAttribute("role", "switch");
      box.checked = key.startsWith("svc:") ? cfg.services[key.slice(4)] : cfg[key];
      box.dataset.key = key;
      box.addEventListener("change", () => save(key.startsWith("svc:") ? { services: { ...cfg.services, [key.slice(4)]: box.checked } } : { [key]: box.checked }));
      row.append(text, box);
      return row;
    }));
    const cleared = cfg.cleared_services || [];
    if (cleared.length) {   // "Always allow" answers: shown under the Ask first switch, with a way to take them back
      const row = el("div", "bb-switch-row");
      const names = cleared.map((k) => (cfg.ask_services_list || {})[k] || k).join(", ");
      const text = el("span", "bb-module-text"); text.append(el("span", "bb-module-name", "Always allowed"), el("span", "bb-module-note", `Never asked about: ${names}`));
      const forget = el("button", "bb-button", "Forget"); forget.type = "button";
      forget.addEventListener("click", async () => { await save({ cleared_services: [] }); renderSwitches(); });
      row.append(text, forget);
      host.querySelector("input[data-key='ask_services']")?.closest("label").after(row);
    }
  }

  const LOOP_NOTES = {
    off: "When the agent repeats the same tool call: no warnings, no stopping, goals keep their full turns.",
    warn: "When the agent repeats the same tool call: you get a warning and a notification, and it is told to try another approach. It is never stopped.",
    stop: "When the agent repeats the same tool call: Hermes halts the run, and a goal stuck for two turns pauses.",
  };
  const limitNote = (v) => +v === 0
    ? "When a run uses up Hermes' iteration limit (max tool calls per turn), the agent stops and waits for you."
    : "When a run uses up Hermes' iteration limit, the agent is restarted by itself to continue the work"
      + (+v < 0 ? ", every time." : `, up to ${v} time${+v === 1 ? "" : "s"} in a row.`);
  const FILE_LABELS = { none: "No file access", read: "Read only", workspace: "Read, write in its workspace", full: "Read and write anywhere" };
  function renderPermissions() {
    const list = cfg.permissions_list || [];
    const files = list.find((x) => x.choices);
    const pick = $("agent-perm-files");
    if (files) {
      pick.replaceChildren(...files.choices.map((c) => new Option(FILE_LABELS[c] || c, c)));
      pick.value = cfg.permissions[files.key];
      $("agent-perm-files-note").textContent = files.notes[pick.value] || "";
    }
    $("agent-approval").value = cfg.command_approval;
    $("agent-loop-guard").value = cfg.loop_guard || "off";
    $("agent-loop-guard-note").textContent = LOOP_NOTES[$("agent-loop-guard").value];
    $("agent-limit-restarts").value = String(cfg.limit_restarts ?? 0);
    $("agent-limit-restarts-note").textContent = limitNote($("agent-limit-restarts").value);
    renderImageSearch();
    const rows = list.filter((x) => !x.choices).map((x) => [x.key, x.label, x.note]);
    rows.push(["continuity", "Hermes session memory", "Each Agent session is one Hermes conversation: it keeps its earlier tool calls and results, not just a summary (/fresh starts over)"]);
    $("agent-perms").replaceChildren(...rows.map(([key, name, note]) => {
      const row = el("label", "bb-switch-row");
      const text = el("span", "bb-module-text"); text.append(el("span", "bb-module-name", name), el("span", "bb-module-note", note));
      const box = el("input", "bb-switch"); box.type = "checkbox"; box.setAttribute("role", "switch");
      box.checked = key === "continuity" ? cfg.continuity : !!cfg.permissions[key];
      box.addEventListener("change", () => save(key === "continuity" ? { continuity: box.checked } : { permissions: { [key]: box.checked } }));
      row.append(text, box);
      return row;
    }));
  }
  $("agent-perm-files").addEventListener("change", async (e) => {
    await save({ permissions: { files: e.target.value } });
    renderPermissions();
  });
  $("agent-approval").addEventListener("change", (e) => save({ command_approval: e.target.value }));
  $("agent-loop-guard").addEventListener("change", (e) => {
    $("agent-loop-guard-note").textContent = LOOP_NOTES[e.target.value];
    save({ loop_guard: e.target.value });
  });
  const PRIVACY_WARN = "Not the most private: Microsoft (Bing) and Google log searches and tie them to your IP and browser. DuckDuckGo (the default), Commons or Brave track less.";
  function renderImageSearch() {
    const mode = cfg.image_mode || "headless", engines = cfg.image_engines || [];
    $("agent-image-mode").value = mode;
    $("agent-image-engine").replaceChildren(...engines.map((x) => {
      const o = new Option(x.label + (x.headless ? "" : " (live browser only)"), x.id);
      o.disabled = mode === "headless" && !x.headless;
      if (!x.private) { o.classList.add("bb-not-private"); o.title = PRIVACY_WARN; }
      return o;
    }));
    $("agent-image-engine").value = cfg.image_engine || "duckduckgo";
    const picked = engines.find((x) => x.id === $("agent-image-engine").value) || {}, label = picked.label || "the engine";
    $("agent-image-engine").classList.toggle("bb-not-private", picked.private === false);
    $("agent-image-engine").title = picked.private === false ? PRIVACY_WARN : "";
    $("agent-image-note").textContent = mode === "headless"
      ? `Reference pictures: the agent's $R images searches ${label} in the background and gives it numbered results to grab. Google, Brave and Yandex block background searches, so they need the live browser.`
      : `Reference pictures: $R images connects the live browser if needed (a visible Chromium window on the desktop, like /browser connect) and the agent searches ${label} there itself.`;
  }
  $("agent-image-engine").addEventListener("change", async (e) => { await save({ image_engine: e.target.value }); renderImageSearch(); });
  $("agent-image-mode").addEventListener("change", async (e) => { await save({ image_mode: e.target.value }); renderImageSearch(); });
  $("agent-limit-restarts").addEventListener("change", (e) => {
    $("agent-limit-restarts-note").textContent = limitNote(e.target.value);
    save({ limit_restarts: +e.target.value });
  });

  $("agent-model").addEventListener("change", (e) => { const [provider, model] = e.target.value.split("|"); save({ provider: model ? provider : "", model: model || "" }); });
  $("agent-reasoning").addEventListener("change", (e) => save({ reasoning: e.target.value }));
  $("agent-vid-preset").addEventListener("change", (e) => save({ vid_preset: e.target.value }));
  for (const kind of ["video", "voice", "image", "music"]) {
    $(`agent-${kind}-model`).addEventListener("change", (e) => {
      if (kind === "video") $("agent-vid-preset-field").hidden = e.target.value !== "minimax_h3";
      save({ [`${kind}_model`]: e.target.value });
    });
  }
  $("agent-keep-loaded").addEventListener("change", (e) => save({ keep_loaded_ask: +e.target.value }));
  $("agent-max-jobs").addEventListener("change", (e) => {
    const n = Math.round(Number(e.target.value));
    const value = Number.isFinite(n) && n > 0 ? n : 0;   // 0 = no limit
    e.target.value = value;  // show what is saved, not what was typed
    save({ max_jobs: value });
  });

  /* ------------------------------------------------- rules -- */
  // The Master Agent brief lives in the Agent config; the rest are the Trainer's (/Trainer/api/rules).
  const rule = () => rules.find((r) => r.key === current);
  const dirty = () => !!rule() && $("rule-text").value !== rule().text;
  function briefRule() {
    return { key: "agent_brief", source: "agent", title: "Master Agent brief", group: "Master Agent",
             help: "What the Master Agent is told at the start of every turn. Placeholders: {session} {script} {services} {history} {request} (required: {session} {script} {request}). Literal braces must be doubled.",
             text: cfg.brief || cfg.default_brief, default: cfg.default_brief, is_default: !cfg.brief, history: [], path: "Agent data/config.json" };
  }
  function renderRuleState() {
    const r = rule(); if (!r) return;
    $("rule-save").disabled = !dirty();
    $("rule-revert").disabled = !dirty();
    $("rule-reset").disabled = !r.default || (r.is_default && !dirty());
    if (dirty()) status("rule-status", "Unsaved changes");
    else status("rule-status", r.is_default ? "Default" : "Edited", r.is_default ? "" : "ok");
  }
  function openRule(key) {
    if (current && key !== current && dirty() && !confirm("Throw away your unsaved changes to this rule?")) { $("rule-pick").value = current; return; }
    current = key;
    const r = rule();
    $("rule-pick").value = key;
    $("rule-help").textContent = r.help;
    $("rule-path").textContent = `Stored in ${r.path}`;
    $("rule-text").value = r.text;
    const h = $("rule-history");
    h.hidden = r.source === "agent";
    h.replaceChildren(new Option(r.history.length ? `History (${r.history.length})…` : "No earlier versions", ""));
    for (const v of r.history) h.append(new Option(`${new Date(v.saved_at * 1000).toLocaleString()} · ${v.chars} chars`, v.id));
    h.disabled = !r.history.length;
    try { localStorage.setItem("settings-rule", key); } catch (_) {}
    renderRuleState();
  }
  function renderPicker() {
    const pick = $("rule-pick"); pick.replaceChildren();
    let group = null, og = null;
    for (const r of rules) {
      if (r.group !== group) { group = r.group; og = el("optgroup"); og.label = group; pick.append(og); }
      og.append(new Option(`${r.title}${r.is_default ? "" : " (edited)"}`, r.key));
    }
  }
  async function loadRules(keep) {
    rules = [briefRule()];
    try {
      const trainer = await call("/Trainer/api/rules");
      rules.push(...trainer.rules.map((r) => ({ ...r, source: "trainer", group: `Hermes · ${r.group}` })));
    } catch (e) { status("rule-status", `Trainer rules unavailable: ${e.message}`, "error"); }
    renderPicker();
    let pick = keep;
    if (!pick) { try { pick = localStorage.getItem("settings-rule"); } catch (_) {} }
    if (!rules.some((r) => r.key === pick)) pick = rules[0].key;
    current = null; openRule(pick);
  }
  function replaceRule(updated) { rules = rules.map((r) => (r.key === updated.key ? { ...r, ...updated } : r)); renderPicker(); }

  async function saveRule(text) {
    const r = rule();
    try {
      if (r.source === "agent") {
        cfg = await call("/Agent/api/config", { method: "PUT", body: JSON.stringify({ brief: text === r.default ? "" : text }) });
        replaceRule(briefRule());
      } else {
        const updated = await call(`/Trainer/api/rules/${r.key}`, { method: "PUT", body: JSON.stringify({ text }) });
        replaceRule({ ...updated, source: "trainer", group: r.group });
      }
      const k = current; current = null; openRule(k);
      status("rule-status", "Saved · applies to the next run", "ok");
    } catch (e) { status("rule-status", e.message, "error"); }
  }
  $("rule-pick").addEventListener("change", (e) => openRule(e.target.value));
  $("rule-text").addEventListener("input", renderRuleState);
  $("rule-text").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); if (dirty()) saveRule($("rule-text").value); }
  });
  $("rule-save").addEventListener("click", () => saveRule($("rule-text").value));
  $("rule-revert").addEventListener("click", () => { $("rule-text").value = rule().text; renderRuleState(); });
  $("rule-reset").addEventListener("click", async () => {
    const r = rule();
    if (!confirm(`Reset "${r.title}" to its default?${r.source === "trainer" ? " The current text is kept in History." : ""}`)) return;
    if (r.source === "agent") return saveRule(r.default);
    try {
      const updated = await call(`/Trainer/api/rules/${r.key}/reset`, { method: "POST" });
      replaceRule({ ...updated, source: "trainer", group: r.group });
      const k = current; current = null; openRule(k);
      status("rule-status", "Back to the default", "ok");
    } catch (e) { status("rule-status", e.message, "error"); }
  });
  $("rule-history").addEventListener("change", async (e) => {
    const v = e.target.value; e.target.value = "";
    if (!v) return;
    if (dirty() && !confirm("Replace your unsaved edits with this older version?")) return;
    try { const r = await call(`/Trainer/api/rules/${current}/history/${v}`); $("rule-text").value = r.text; renderRuleState(); status("rule-status", "Older version loaded. Save to make it current."); }
    catch (err) { status("rule-status", err.message, "error"); }
  });
  window.addEventListener("beforeunload", (e) => { if (dirty()) { e.preventDefault(); e.returnValue = ""; } });

  /* ------------------------------------------------- schedules -- */
  // Agent /api/schedules: a request sent to a new session at a set time (in this browser's timezone).
  const TZ = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (_) { return ""; } })();
  async function loadSchedules() {
    const { items } = await call("/Agent/api/schedules");
    const host = $("agent-sched-list");
    host.replaceChildren(...items.map((x) => {
      const row = el("div", "bb-switch-row");
      const next = x.enabled && x.next ? ` · next ${new Date(x.next * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}` : "";
      const text = el("span", "bb-module-text");
      text.append(el("span", "bb-module-name", `${x.summary}${x.tz !== TZ ? ` (${x.tz})` : ""}${next}`), el("span", "bb-module-note", x.prompt));
      const box = el("input", "bb-switch"); box.type = "checkbox"; box.setAttribute("role", "switch"); box.checked = x.enabled;
      box.setAttribute("aria-label", `Schedule ${x.summary} on`);
      box.addEventListener("change", async () => {
        try { await call(`/Agent/api/schedules/${x.id}`, { method: "PATCH", body: JSON.stringify({ enabled: box.checked }) }); await loadSchedules(); }
        catch (e) { box.checked = !box.checked; status("agent-sched-status", e.message, "error"); }
      });
      const remove = el("button", "bb-button", "Delete"); remove.type = "button";
      remove.addEventListener("click", async () => {
        try { await call(`/Agent/api/schedules/${x.id}`, { method: "DELETE" }); await loadSchedules(); }
        catch (e) { status("agent-sched-status", e.message, "error"); }
      });
      const actions = el("span", "bb-sched-actions"); actions.append(box, remove);
      row.append(text, actions);
      return row;
    }));
  }
  const today = () => { const d = new Date(); return new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); };
  $("agent-sched-date").value = today();
  $("agent-sched-repeat").addEventListener("change", (e) => { $("agent-sched-date-field").hidden = e.target.value !== "once"; });
  $("agent-sched-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    status("agent-sched-status", "Saving…");
    try {
      await call("/Agent/api/schedules", { method: "POST", body: JSON.stringify({
        prompt: $("agent-sched-prompt").value, time: $("agent-sched-time").value, repeat: $("agent-sched-repeat").value,
        date: $("agent-sched-repeat").value === "once" ? $("agent-sched-date").value : "", hours: Number($("agent-sched-hours").value) || 0, tz: TZ,
      }) });
      $("agent-sched-prompt").value = "";
      status("agent-sched-status", "Scheduled", "ok");
      await loadSchedules();
    } catch (err) { status("agent-sched-status", err.message, "error"); }
  });

  /* ------------------------------------------------- load -- */
  async function load() {
    if (loaded) return;
    loaded = true;
    try {
      cfg = await call("/Agent/api/config");
      renderSwitches();
      renderPermissions();
      loadSchedules().catch((e) => status("agent-sched-status", e.message, "error"));
      const options = await call("/Agent/api/options");
      fillModel(options);
      await loadRules();
    } catch (e) {
      loaded = false;
      status("agentic-status", `The Agent service isn't reachable: ${e.message}`, "error");
    }
  }
  new MutationObserver(() => { if (!panel.hidden) load(); }).observe(panel, { attributes: true, attributeFilter: ["hidden"] });
  if (!panel.hidden) load();
})();
