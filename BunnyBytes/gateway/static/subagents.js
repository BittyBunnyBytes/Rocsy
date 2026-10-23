// Rocsy Settings: Sub-agents tab - every agentic mode's AI in one place. Each card reads and
// writes that service's own settings API, so this tab and the service's page always agree:
//   Master Agent  /Agent/api/config            (Hermes provider + model, reasoning)
//   Video         /Video/api/enhancement         (H3 Agentic: llama model, thinking, sampling, checks)
//   Image/Music/Voice  /<studio>/api/agentic/settings  (shared framework)
//   REA           /REA/api/settings            (Hermes model + thinking for every REA task)
//   Vibe Coding   /Code/api/settings           (Hermes model + thinking for project sessions)
// Every model list is the Master Agent's: Hermes' providers, then the coding CLIs installed on the host
// (Claude Code, Codex, Kimi Code ... from /Agent/api/options, found by bunny-hostctl). The llama planners
// store llama-swap models as plain ids, other Hermes providers as "hermes:<provider>/<model>" and CLIs as
// "cli:<id>/<model>"; the Hermes ones store provider + model. Settings a CLI doesn't use are greyed then.
(() => {
  const panel = document.getElementById("panel-subagents");
  if (!panel) return;
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const THINKING = [["none", "Off"], ["minimal", "Minimal"], ["low", "Low"], ["medium", "Medium"], ["high", "High"], ["xhigh", "Extra high"], ["max", "Max"], ["default", "Model default"]];
  const ICON = {
    master: '<path d="M12 8V4H8"/><rect x="4" y="8" width="16" height="12" rx="2"/><path d="M2 14h2M20 14h2M15 13v2M9 13v2"/>',
    video: '<rect x="3" y="6" width="13" height="12" rx="2.5"/><path d="M16 10.5l5-3v9l-5-3"/>',
    image: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><circle cx="9" cy="10" r="1.8"/><path d="M20.5 16l-5-5L6 20"/>',
    music: '<path d="M9 18V6l11-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>',
    voice: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
    rea: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94z"/>',
    code: '<path d="M8 8l-4 4 4 4M16 8l4 4-4 4M13.5 5l-3 14"/>',
  };
  // Studio planners share the framework's knobs; their switches differ.
  const PLANNER = [
    ["plan_review", "Review the plan", "A second pass checks the plan against your request and the model guide"],
    ["approve_plan", "Wait for my approval", "The plan waits for you to approve or revise it before anything renders"],
    ["use_model_guide", "Follow the model's prompt guide", "The loaded model's official prompting guide goes into the planner's instructions"],
  ];
  const SAMPLING = [["temperature", "Temperature", 0, 2, 0.05], ["top_p", "Top P", 0, 1, 0.01], ["top_k", "Top K", 0, 1000, 1],
    ["min_p", "Min P", 0, 1, 0.01], ["repeat_penalty", "Repeat penalty", 0, 4, 0.01], ["seed", "Seed (-1 = random)", -1, 2147483647, 1]];
  const llama = (url) => ({ kind: "llama", url, thinkingKey: "thinking_level", sampling: SAMPLING });
  const AGENTS = [
    { key: "master", name: "Master Agent", sub: "Drives every studio from the Agent page", tint: "var(--bb-purple)", open: ["#agentic", "All Master Agent settings"],
      kind: "hermes", url: "/Agent/api/config",
      read: (c) => ({ model: c.model || "", provider: c.provider || "", reasoning: c.reasoning || "default" }),
      write: (m) => ("reasoning" in m ? { reasoning: m.reasoning } : { model: m.model, provider: m.model ? m.provider : "" }) },
    { key: "video", name: "Video Agentic", sub: "H3, LTX and Wan sessions: plans clips, reviews and fixes them", tint: "var(--bb-blue)", open: ["/Video/", "Open Video"],
      ...llama("/Video/api/enhancement"),
      switches: [
        ["agentic_stage_enabled", "Staging previews", "Render every clip with a fast preset first, before the final renders"],
        ["agentic_stage_review", "Hermes reviews the previews", "Sampled frames are checked and failed clips get revised prompts (needs a vision model)"],
        ["agentic_stage_approval", "Wait for my approval", "Accept to render the finals, or Exclude with notes to redo the previews"],
        ["agentic_final_review", "Hermes reviews the final clips", "Same frame review after the final renders"],
        ["agentic_stage_series_review", "Series consistency pass (previews)", "Hermes looks at all clips together for drift in characters, style and cuts"],
        ["agentic_final_series_review", "Series consistency pass (finals)", "The same check on the final clips"],
        ["agentic_stage_best_of_two", "Best of 2 for flagged previews", "A redone clip renders with two seeds and Hermes keeps the better one"],
        ["agentic_final_best_of_two", "Best of 2 for flagged finals", "The same for final clips"],
        ["agentic_whisper_check", "Check preview dialogue with Whisper", "Gibberish, missing or unplanned speech gets a fixed prompt and a re-render"],
        ["agentic_final_whisper_check", "Check final dialogue with Whisper", "The same for final clips"],
        ["agentic_rushed_dialogue_check", "Catch sped-up dialogue", "Rejects plans that cram too many words into a clip and times rendered speech"],
        ["agentic_lora_gallery", "Let Hermes choose from the LoRA gallery", "One relevant LoRA per clip"],
        ["agentic_browser_enabled", "Web research", "Search and read public web pages"],
        ["agentic_video_search", "YouTube search", "Also search YouTube for videos"],
        ["agentic_manage_references", "Download and prepare references", "Fetch media Hermes finds, trim, crop or keep only the audio"],
        ["agentic_character_screenshots", "Keep fictional characters consistent", "Reuses a clear frame of an original character as an image reference"],
        ["agentic_scene_screenshots", "Keep backgrounds consistent across shots", "Reuses the frame that shows the most of each location"],
        ["agentic_continuation_tail", "Analyze the last 5 s for motion", "Carries subject and camera motion into the next prompt"],
        ["agentic_continuation_tail_audio", "Use the last 5 s of audio", "Voice and ambience continuity for continuations"],
        ["agentic_review_cap", "Cap review rounds", "Off = keep revising until the review passes (safety stop after 25 rounds)"],
      ],
      numbers: [["agentic_review_rounds", "Max review rounds", 0, 10, 1]],
      primary: ["agentic_stage_enabled", "agentic_stage_review", "agentic_stage_approval", "agentic_final_review"] },
    { key: "image", name: "Image Agentic", sub: "Krea 2, Qwen Image and Anima planner", tint: "var(--bb-green)", open: ["/Image/", "Open Image"],
      ...llama("/Image/api/agentic/settings"),
      switches: [...PLANNER, ["output_review", "Review the image", "Checks each render against your request and retries clear errors"],
        ["suggest_loras", "Look for relevant LoRAs", "Hermes may enable one gallery LoRA that fits the request"]],
      numbers: [["max_attempts", "Attempts per image", 1, 3, 1]], primary: ["plan_review", "approve_plan", "output_review"] },
    { key: "music", name: "Music Agentic", sub: "YuE2 and MiniMax Music 3: style and lyrics", tint: "var(--bb-amber)", open: ["/Music/", "Open Music"],
      ...llama("/Music/api/agentic/settings"), switches: PLANNER, numbers: [["max_attempts", "Attempts", 1, 3, 1]], primary: ["plan_review", "approve_plan"] },
    { key: "voice", name: "Voice Agentic", sub: "Qwen3-TTS, IndexTTS2 and Fish: the spoken text", tint: "var(--bb-red)", open: ["/Voice/", "Open Voice"],
      ...llama("/Voice/api/agentic/settings"), switches: PLANNER, numbers: [["max_attempts", "Attempts", 1, 3, 1]], primary: ["plan_review", "approve_plan"] },
    { key: "rea", name: "REA", sub: "Plans, builds and refines mods; maps games", tint: "#d58cf0", open: ["/REA/", "Open REA"],
      kind: "hermes", url: "/REA/api/settings", headers: { "X-REA": "1" },
      read: (c) => ({ model: c.model?.model || "", provider: c.model?.provider || "", reasoning: c.model?.reasoning || "medium" }),
      write: (m, cur) => ({ model: { ...cur, ...m } }) },
    { key: "code", name: "Vibe Coding", sub: "Project sessions on the Code page", tint: "#5ec8c8", open: ["/Code/", "Open Vibe Coding"],
      kind: "hermes", url: "/Code/api/settings",
      read: (c) => ({ model: c.model?.model || "", provider: c.model?.provider || "", reasoning: c.model?.reasoning || "" }),
      write: (m, cur) => ({ model: { ...cur, ...m } }) },
  ];
  let loaded = false, llamaModels = { default: "", models: [] }, hermes = { providers: [], default: {} }, clis = [];
  const state = {};   // key -> { data, model }

  async function call(url, options = {}, extra = {}) {
    const r = await fetch(url, { credentials: "same-origin", ...options, headers: { "Content-Type": "application/json", ...extra, ...(options.headers || {}) } });
    if (r.status === 401) { location.href = "/login?next=/Settings%23subagents"; throw new Error("Log in again"); }
    let body = {}; try { body = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error((typeof body.detail === "string" && body.detail) || body.message || body.error || `Request failed (${r.status})`);
    return body;
  }
  function status(node, text, kind = "") { node.textContent = text; node.dataset.kind = kind; if (kind === "ok") setTimeout(() => { if (node.textContent === text) node.textContent = ""; }, 2500); }
  function field(label, control) { const f = el("label", "bb-field"); f.append(label, control); return f; }
  function thinkingSelect(value, withInherit = false) {
    const s = el("select");
    for (const [v, l] of THINKING) s.append(new Option(l, v));
    if (withInherit) s.prepend(new Option("Same as the agent", ""));
    s.value = THINKING.some(([v]) => v === value) || (withInherit && !value) ? value : "default";
    return s;
  }
  // The CLI a model choice points at: {id, model, cli (undefined once uninstalled)}, or null for llama/Hermes.
  function cliChoice(model, provider) {
    const raw = provider?.startsWith("cli:") ? `${provider.slice(4)}/${model}` : model?.startsWith("cli:") ? model.slice(4) : "";
    if (!raw) return null;
    const [id, ...rest] = raw.split("/");
    return { id, model: rest.join("/") || "default", cli: clis.find((c) => c.id === id) };
  }
  const cliName = (c) => `${c.cli?.name || c.id} · ${c.model === "default" ? "its default model" : c.model}`;
  // Installed CLIs, last in the list; value(id, model) gives each option's value in the select's own format.
  function cliGroups(s, value) {
    for (const c of clis) {
      const g = el("optgroup"); g.label = `${c.name} · coding CLI`;
      g.append(new Option(`${c.name} default${c.default ? ` (${c.default})` : ""}`, value(c.id, "default")));
      for (const m of c.models) if (m.id !== "default") g.append(new Option(m.label === m.id ? m.id : `${m.label} (${m.id})`, value(c.id, m.id)));
      s.append(g);
    }
  }
  // A Hermes provider model in a llama planner's format: llama-swap's own models stay plain ids.
  const llamaValue = (slug, m) => {
    const pr = (hermes.providers || []).find((p) => p.slug === slug);
    return pr?.local && llamaModels.models.includes(m) ? m : `hermes:${slug}/${m}`;
  };
  // Hermes' providers, grouped as on the Master Agent's picker; value(slug, model) gives each option's value.
  function providerGroups(s, value) {
    const listed = new Set();
    for (const pr of hermes.providers || []) {
      const g = el("optgroup"); g.label = `${pr.name}${pr.local ? " · local GPU" : ""}`;
      for (const m of pr.models) { g.append(new Option(m, value(pr.slug, m))); listed.add(m); }
      s.append(g);
    }
    return listed;
  }
  function llamaSelect(value, allowDefault = true) {
    const s = el("select");
    if (allowDefault || !value) s.append(new Option(`Default (${llamaModels.default || "server default"})`, ""));
    const listed = providerGroups(s, llamaValue);
    const rest = llamaModels.models.filter((m) => !listed.has(m));   // Hermes down, or not in its config
    if (rest.length) { const g = el("optgroup"); g.label = "Local models"; for (const m of rest) g.append(new Option(m, m)); s.append(g); }
    cliGroups(s, (id, m) => `cli:${id}/${m}`);
    if (value && ![...s.options].some((o) => o.value === value)) s.append(new Option(`${value} (not installed)`, value));
    s.value = value || "";
    return s;
  }
  function hermesSelect(model, provider, defaultLabel) {
    const s = el("select");
    s.append(new Option(defaultLabel || `Hermes default (${hermes.default?.model || "?"})`, "|"));
    providerGroups(s, (slug, m) => `${slug}|${m}`);
    cliGroups(s, (id, m) => `cli:${id}|${m}`);
    const value = model ? `${provider}|${model}` : "|";
    if (![...s.options].some((o) => o.value === value)) s.append(new Option(`${model} (${provider || "?"})`, value));
    s.value = value;
    return s;
  }
  function switchRow(name, note, checked, onChange) {
    const row = el("label", "bb-switch-row");
    const text = el("span", "bb-module-text"); text.append(el("span", "bb-module-name", name), el("span", "bb-module-note", note));
    const box = el("input", "bb-switch"); box.type = "checkbox"; box.setAttribute("role", "switch"); box.checked = Boolean(checked);
    box.addEventListener("change", () => onChange(box));
    row.append(text, box);
    return row;
  }
  const modelLabel = (a, s) => {
    if (!s?.data) return "";
    const m = a.kind === "llama" ? { model: s.data.model, reasoning: s.data.thinking_level } : a.read(s.data);
    const cli = cliChoice(m.model, m.provider);
    const think = m.reasoning && (!cli || cli.cli?.efforts.length) ? ` · thinking ${m.reasoning}` : "";
    if (cli) return cliName(cli) + think;
    if (a.kind === "llama" && m.model?.startsWith("hermes:")) {
      const [slug, ...name] = m.model.slice(7).split("/");
      return `${name.join("/")} · ${(hermes.providers || []).find((p) => p.slug === slug)?.name || slug}${think}`;
    }
    if (a.kind === "llama") return `${m.model || `default (${llamaModels.default || "server"})`}${think}`;
    return `${m.model || (a.key === "master" ? `Hermes default (${hermes.default?.model || "?"})` : "Master Agent's model")}${think}`;
  };

  async function save(a, patch, foot, revert) {
    status(foot, "Saving…");
    try {
      const s = state[a.key];
      const body = a.kind === "llama" ? patch : a.write(patch, a.read(s.data));
      s.data = await call(a.url, { method: "PUT", body: JSON.stringify(body) }, a.headers);
      if ("model" in patch && card(a)) {   // switching to or from a CLI changes which settings apply
        card(a).replaceWith(render(a));
        foot = card(a).querySelector(".sa-foot .bb-status");
      }
      status(foot, "Saved", "ok");
      card(a).querySelector(".sa-title span").textContent = modelLabel(a, s);
    } catch (e) { status(foot, e.message, "error"); revert?.(); }
  }
  const card = (a) => $(`sa-${a.key}`);
  // Greys out a control (or a whole group) that the chosen coding CLI doesn't use.
  function notUsed(node, cli, why) {
    node.classList.add("sa-na");
    node.title = `${cli.cli?.name || cli.id} doesn't use this${why ? `: ${why}` : ""}`;
    node.querySelectorAll?.("input, select, textarea").forEach((x) => { x.disabled = true; });
    if (node.matches("input, select")) node.disabled = true;
  }

  function render(a) {
    const s = state[a.key];
    const root = el("article", "sa-card"); root.id = `sa-${a.key}`; root.style.setProperty("--sa-tint", a.tint);
    const head = el("header", "sa-head");
    const icon = el("span", "sa-icon"); icon.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${ICON[a.key]}</svg>`;
    const title = el("div", "sa-title"); title.append(el("strong", "", a.name), el("span", "", s.error ? a.sub : modelLabel(a, s)));
    title.title = a.sub;
    const open = el("a", "sa-open", a.open[1]); open.href = a.open[0];
    if (a.open[0].startsWith("#")) open.addEventListener("click", (e) => { e.preventDefault(); document.getElementById(`tab-${a.open[0].slice(1)}`)?.click(); });
    head.append(icon, title, open);
    const body = el("div", "sa-body");
    const foot = el("div", "sa-foot"); const st = el("span", "bb-status"); st.setAttribute("role", "status");
    root.append(head, body);
    if (s.error) {
      root.dataset.state = "down";
      body.append(el("p", "sa-down", `${a.name} isn't reachable right now (${s.error}). Start it in Settings → Services, then reopen this tab.`));
      return root;
    }
    const grid = el("div", "bb-form-grid");
    if (a.kind === "llama") {
      const d = s.data;
      const model = llamaSelect(d.model, a.key !== "video");   // Video always keeps a model of its own
      model.addEventListener("change", () => save(a, { model: model.value }, st));
      const think = thinkingSelect(d.thinking_level);
      think.addEventListener("change", () => save(a, { thinking_level: think.value }, st));
      const cli = cliChoice(d.model);
      const thinkField = field("Thinking", think);
      if (cli && !cli.cli?.efforts.length) notUsed(thinkField, cli, "it has no thinking-level setting");
      grid.append(field("Model", model), thinkField);
      for (const [key, label, min, max, step] of a.numbers || []) {
        const input = el("input"); Object.assign(input, { type: "number", min, max, step, value: d[key] });
        input.addEventListener("change", () => save(a, { [key]: Number(input.value) }, st, () => { input.value = state[a.key].data[key]; }));
        grid.append(field(label, input));
      }
      body.append(grid);
      const list = el("div", "bb-switch-list"), rest = el("div", "bb-switch-list");
      for (const [key, name, note] of a.switches || []) {
        if (!(key in d)) continue;
        const row = switchRow(name, note, d[key], (box) => save(a, { [key]: box.checked }, st, () => { box.checked = !box.checked; }));
        ((a.primary || []).includes(key) ? list : rest).append(row);
      }
      body.append(list);
      const more = el("details", "sa-more");
      const on = (a.switches || []).filter(([key]) => !(a.primary || []).includes(key) && d[key]).length;
      const sum = el("summary"); sum.append("More checks and tools", el("span", "sa-count", rest.childElementCount ? `${on} of ${rest.childElementCount} on` : ""));
      if (rest.childElementCount) { more.append(sum, rest); body.append(more); }
      const samp = el("details", "sa-more"); samp.append(el("summary", "", "Sampling"));
      const sg = el("div", "bb-form-grid");
      for (const [key, label, min, max, step] of a.sampling) {
        const input = el("input"); Object.assign(input, { type: "number", min, max, step, value: d[key] });
        input.addEventListener("change", () => save(a, { [key]: Number(input.value) }, st, () => { input.value = state[a.key].data[key]; }));
        sg.append(field(label, input));
      }
      samp.append(sg); body.append(samp);
      if (cli) {
        notUsed(samp, cli, "it runs its model with the provider's own sampling");
        samp.querySelector("summary").append(el("span", "sa-count", `not used by ${cli.cli?.name || cli.id}`));
        body.insertBefore(el("p", "sa-note", `Runs through ${cli.cli?.name || cli.id} on this machine, signed in with your own account: `
          + "no GPU turn, no model loading, and it can look at the frames and images it reviews."), samp);
      }
    } else {
      const m = a.read(s.data);
      const fallback = a.key === "master" ? null : "Same model as the Master Agent";
      const model = hermesSelect(m.model, m.provider, fallback);
      model.addEventListener("change", () => { const [provider, name] = model.value.split("|"); save(a, { model: name || "", provider: name ? provider : "" }, st); });
      const think = a.key === "code" ? thinkingSelect(m.reasoning, true) : thinkingSelect(m.reasoning);
      think.addEventListener("change", () => save(a, { reasoning: think.value }, st));
      const cli = cliChoice(m.model, m.provider);
      const thinkField = field("Thinking", think);
      if (cli && !cli.cli?.efforts.length) notUsed(thinkField, cli, "it has no thinking-level setting");
      grid.append(field("Model", model), thinkField);
      body.append(grid);
      if (cli) body.append(el("p", "sa-note", `Each turn runs ${cli.cli?.name || cli.id} on this machine in the Hermes workspace, `
        + "with the whole brief, under its own safety mode. Hermes' memory, compression, loop guard, tool permissions "
        + (a.key === "rea" ? "and REA's MCP tools (Ghidra, JADX …) don't apply." : "and the GPU hand-off don't apply.")));
      body.append(el("p", "sa-note", {
        master: "Its permissions, review mode, schedules and rules are on the Agentic tab.",
        rea: "Used for every REA task: planning, building and refining mods, mapping games, questions, decompiling and textures.",
        code: "Used when the Code page's model picker is on its default. Picking a model there changes this too.",
      }[a.key]));
    }
    body.append(foot); foot.append(st);
    return root;
  }

  function fillAll() {
    const model = $("sa-all-model");   // values in the Hermes format: "<provider>|<model>", "cli:<id>|<model>"
    model.replaceChildren(new Option("Choose a model…", ""));
    const listed = providerGroups(model, (slug, m) => `${slug}|${m}`);
    const rest = llamaModels.models.filter((m) => !listed.has(m));
    if (rest.length) { const g = el("optgroup"); g.label = "Local models"; for (const m of rest) g.append(new Option(m, `|${m}`)); model.append(g); }
    cliGroups(model, (id, m) => `cli:${id}|${m}`);
    $("sa-all-thinking").replaceChildren(new Option("Keep each one's", ""), ...THINKING.map(([v, l]) => new Option(l, v)));
  }
  $("sa-all-apply").addEventListener("click", async () => {
    const choice = $("sa-all-model").value, think = $("sa-all-thinking").value, st = $("sa-all-status");
    if (!choice && !think) { status(st, "Choose a model or a thinking level first", "error"); return; }
    const [provider, model] = choice ? choice.split("|") : ["", ""];
    // The same choice in a llama planner's format.
    const asLlama = !model ? "" : provider.startsWith("cli:") ? `${provider}/${model}` : provider ? llamaValue(provider, model) : model;
    const skipped = [];
    status(st, "Applying…");
    for (const a of AGENTS) {
      const s = state[a.key]; if (!s || s.error) { skipped.push(a.name); continue; }
      const foot = card(a).querySelector(".sa-foot .bb-status");
      if (a.kind === "llama") await save(a, { ...(model ? { model: asLlama } : {}), ...(think ? { thinking_level: think } : {}) }, foot);
      else {
        if (model && provider) await save(a, { model, provider }, foot);
        else if (model) skipped.push(a.name);   // a llama-swap model Hermes has no provider for
        if (think) await save(a, { reasoning: think }, foot);
      }
    }
    redraw();
    status(st, skipped.length ? `Applied · skipped ${skipped.join(", ")}` : "Applied to every sub-agent", skipped.length ? "" : "ok");
  });

  function redraw() { $("sa-grid").replaceChildren(...AGENTS.map(render)); }
  async function load() {
    if (loaded) return;
    loaded = true;
    $("sa-grid").replaceChildren(el("p", "sa-note", "Loading the sub-agents…"));
    const [lm, opts] = await Promise.all([
      call("/Image/api/agentic/models").catch(() => call("/Video/api/enhancement/models")).catch(() => ({ models: [] })),
      call("/Agent/api/options").catch(() => ({})),
    ]);
    llamaModels = { default: lm.default || "", models: lm.models || [] };
    hermes = opts.hermes || hermes;
    clis = opts.clis || [];
    await Promise.all(AGENTS.map(async (a) => {
      try { state[a.key] = { data: await call(a.url, {}, a.headers) }; }
      catch (e) { state[a.key] = { error: e.message }; }
    }));
    fillAll();
    redraw();
  }
  new MutationObserver(() => { if (!panel.hidden) load(); }).observe(panel, { attributes: true, attributeFilter: ["hidden"] });
  if (!panel.hidden) load();
})();
