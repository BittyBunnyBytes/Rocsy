// Rocsy Settings: GPU tuning tab - AI overclock profiles applied through LACT by bunny-hostctl
// (hostctl/gputune.py via /api/gpu-tune), the smart switch that picks a profile per workload, and the
// guided one-variable-at-a-time tuner. Polls every 3 s while the tab is open.
(() => {
  const panel = document.getElementById("panel-gputune");
  if (!panel) return;
  const $ = (id) => document.getElementById(id);
  let data = null, timer = null, busy = false, drafts = {};

  function status(text, kind = "") { const s = $("gt-status"); s.textContent = text; s.dataset.kind = kind; }
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }
  function ago(t) {
    const s = Math.max(0, Date.now() / 1000 - t);
    if (s < 60) return `${Math.round(s)} s ago`;
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return new Date(t * 1000).toLocaleDateString();
  }
  const name = (id) => (data.profiles.find((p) => p.id === id) || {}).name || id;

  async function call(url, method = "GET", body) {
    const options = { method, cache: "no-store", headers: {} };
    if (body !== undefined) {
      options.headers = { "Content-Type": "application/json", "X-BB-Action": "1" };
      options.body = JSON.stringify(body);
    }
    const r = await fetch(url, options);
    if (r.status === 401) { location.href = "/login?next=/Settings%23gputune"; throw new Error("Log in again"); }
    let reply = {}; try { reply = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error(reply.error || `Request failed (${r.status})`);
    return reply;
  }

  async function act(label, url, method, body) {
    if (busy) return;
    busy = true;
    status(label);
    try { data = await call(url, method, body); render(); status("Done", "ok"); }
    catch (e) { status(e.message, "error"); }
    finally { busy = false; }
  }

  function renderLive() {
    const l = data.live || {};
    $("gt-device").textContent = data.lact
      ? `${data.device?.name || "GPU"} · ${data.active ? `${name(data.active)} applied${data.applied_at ? ` ${ago(data.applied_at)}` : ""}` : "nothing applied yet (LACT defaults)"}`
      : `LACT isn't reachable: ${data.error || "unknown error"}. Is lactd running?`;
    const chips = [
      ["Workload", data.workload ? `${data.workloads[data.workload]}${data.source ? ` · ${data.source}` : ""}` : "Idle"],
      ["Core", l.sclk != null ? `${l.sclk} MHz` : "–"],
      ["VRAM", l.mclk != null ? `${l.mclk} MHz` : "–"],
      ["Power", l.power != null ? `${Math.round(l.power)} / ${Math.round(l.cap || 0)} W` : "–"],
      ["Hotspot", l.junction != null ? `${Math.round(l.junction)} °C` : "–"],
      ["Memory", l.mem_temp != null ? `${Math.round(l.mem_temp)} °C` : "–"],
      ["Load", l.busy != null ? `${l.busy} %` : "–"],
    ];
    if (l.throttles?.length) chips.push(["Throttling", l.throttles.join(", ")]);
    $("gt-live").replaceChildren(...chips.map(([k, v]) => {
      const chip = el("div", "gt-chip");
      chip.append(el("span", "gt-chip-k", k), el("span", "gt-chip-v", v));
      return chip;
    }));
    $("gt-hold").hidden = !(data.auto.enabled && data.hold);
    $("gt-hold-text").textContent = data.hold ? `Held on ${name(data.hold)}: the smart switch waits until you resume.` : "";
  }

  function select(value, onChange, label) {
    const s = el("select");
    s.setAttribute("aria-label", label);
    data.profiles.forEach((p) => { const o = el("option", null, p.name); o.value = p.id; s.append(o); });
    s.value = value;
    s.addEventListener("change", () => onChange(s.value));
    return s;
  }

  function renderAuto() {
    $("gt-auto").checked = data.auto.enabled;
    const fields = Object.entries(data.workloads).map(([w, label]) => {
      const f = el("label", "bb-field", label);
      f.append(select(data.auto.map[w], (v) => act("Saving…", "/api/gpu-tune", "PUT", { auto: { map: { [w]: v } } }), `${label} profile`));
      return f;
    });
    const idle = el("label", "bb-field", "When idle");
    idle.append(select(data.auto.idle_profile, (v) => act("Saving…", "/api/gpu-tune", "PUT", { auto: { idle_profile: v } }), "Idle profile"));
    const after = el("label", "bb-field", "Idle after (seconds)");
    const input = el("input");
    Object.assign(input, { type: "number", min: 10, max: 1800, step: 10, value: data.auto.idle_after });
    input.addEventListener("change", () => act("Saving…", "/api/gpu-tune", "PUT", { auto: { idle_after: Number(input.value) } }));
    after.append(input);
    $("gt-map").replaceChildren(...fields, idle, after);
  }

  function numberField(p, key, label, hint) {
    const lim = data.limits?.[key] || [];
    const f = el("label", "bb-field", label);
    const input = el("input");
    Object.assign(input, { type: "number", step: key === "vram" ? 5 : key === "power" ? 1 : 10 });
    if (lim.length) { input.min = lim[0]; input.max = lim[1]; }
    input.value = drafts[p.id]?.[key] ?? p[key];
    input.dataset.key = key;
    input.addEventListener("input", () => {
      (drafts[p.id] ||= {})[key] = input.value;
      if (key === "vram") small.textContent = `= ${Number(input.value) * 2} MHz effective`;
    });
    f.append(input);
    const small = el("small", "gt-hint", key === "vram" ? `= ${Number(input.value) * 2} MHz effective` : hint || (lim.length ? `${lim[0]} … ${lim[1]}` : ""));
    f.append(small);
    return f;
  }

  function runsTable(runs) {
    if (!runs?.length) return el("p", "bb-section-note bb-tight", "No jobs recorded on this profile yet.");
    const table = el("table", "gt-runs");
    const head = el("tr");
    ["When", "Workload", "Time", "Power", "Core", "VRAM", "Hotspot", "Throttle"].forEach((h) => head.append(el("th", null, h)));
    table.append(head);
    runs.slice().reverse().forEach((r) => {
      const tr = el("tr");
      tr.title = r.source || "";
      [ago(r.start), data.workloads[r.workload] || r.workload, `${Math.round(r.secs / 6) / 10} min`, `${Math.round(r.avg_power)} W`,
        `${r.avg_sclk} MHz`, `${r.avg_mclk} MHz`, r.max_junction != null ? `${Math.round(r.max_junction)} °C` : "–",
        r.throttles?.join(", ") || "–"].forEach((v) => tr.append(el("td", null, v)));
      table.append(tr);
    });
    const wrap = el("div", "gt-runs-wrap");
    wrap.append(table);
    return wrap;
  }

  function tuner(p) {
    const box = el("div", "gt-tuner");
    const sug = p.suggestion;
    box.append(el("h4", null, "Guided tuning"));
    if (!sug) return box;
    box.append(el("p", "bb-section-note bb-tight", sug.text));
    const row = el("div", "bb-rules-actions");
    if (sug.kind === "next") {
      const b = el("button", "bb-button bb-button-primary", "Try this step");
      b.type = "button";
      b.addEventListener("click", () => {
        if (confirm(`Apply ${p.name} with ${data.fields[sug.field].label} ${sug.to} ${data.fields[sug.field].unit}? It stays on this profile until you mark it.`))
          act("Applying step…", "/api/gpu-tune/step", "POST", { profile: p.id });
      });
      row.append(b);
    } else if (sug.kind === "testing") {
      if (sug.ready) box.append(el("p", "gt-ready", "Looks good so far: enough load time to call it."));
      const good = el("button", "bb-button bb-button-primary", "Mark stable");
      const bad = el("button", "bb-button", "Unstable: back off");
      good.type = bad.type = "button";
      good.addEventListener("click", () => act("Saving…", "/api/gpu-tune/mark", "POST", { profile: p.id, result: "stable" }));
      bad.addEventListener("click", () => act("Backing off…", "/api/gpu-tune/mark", "POST", { profile: p.id, result: "unstable" }));
      row.append(bad, good);
    }
    box.append(row);
    const steps = p.tuning?.steps || [];
    if (steps.length) {
      const list = el("ol", "gt-steps");
      steps.slice().reverse().forEach((s) => {
        const li = el("li", `gt-step is-${s.result}`);
        li.textContent = `${data.fields[s.field].label} ${s.from} → ${s.to} ${data.fields[s.field].unit} · ${s.result} · ${ago(s.time)}`;
        list.append(li);
      });
      box.append(list);
    }
    return box;
  }

  function profileCard(p) {
    const card = el("article", "gt-profile" + (data.active === p.id ? " is-active" : ""));
    const head = el("header", "gt-profile-head");
    const title = el("div", "bb-svc-text");
    title.append(el("span", "bb-svc-name", p.name + (data.active === p.id ? " · applied" : "")), el("span", "bb-svc-note", p.note || ""));
    const apply = el("button", "bb-button" + (data.active === p.id ? "" : " bb-button-primary"), data.active === p.id ? "Re-apply" : "Apply");
    apply.type = "button";
    apply.addEventListener("click", () => act(`Applying ${p.name}…`, "/api/gpu-tune/apply", "POST", { profile: p.id }));
    head.append(title, apply);
    card.append(head);
    if (p.stock) {
      card.append(el("p", "bb-section-note bb-tight", "Driver defaults: automatic performance level, stock clocks, voltage and power limit."));
      if (data.stock_runs?.length) card.append(runsTable(data.stock_runs));
      return card;
    }
    const grid = el("div", "bb-form-grid gt-values");
    grid.append(
      numberField(p, "vram", "VRAM clock (raw MHz)"),
      numberField(p, "voltage", "Voltage offset (mV)", data.limits ? `${data.limits.voltage[0]} … 0` : ""),
      numberField(p, "core", "Max core clock (MHz)"),
      numberField(p, "power", "Power limit (%)", data.limits?.power_w?.default ? `stock ${data.limits.power_w.default} W` : ""),
    );
    const mode = el("label", "gt-check");
    const box = el("input"); box.type = "checkbox"; box.checked = drafts[p.id]?.compute_mode ?? p.compute_mode !== false;
    box.addEventListener("change", () => { (drafts[p.id] ||= {}).compute_mode = box.checked; });
    mode.append(box, document.createTextNode(" Compute power mode"));
    card.append(grid, mode);
    const actions = el("div", "bb-rules-actions");
    const reset = el("button", "bb-button", "Starting values");
    const save = el("button", "bb-button bb-button-primary", data.active === p.id ? "Save and apply" : "Save");
    reset.type = save.type = "button";
    reset.addEventListener("click", () => {
      if (confirm(`Put ${p.name} back to its starting values and clear its tuning history?`)) {
        delete drafts[p.id];
        act("Resetting…", "/api/gpu-tune/reset-tuning", "POST", { profile: p.id });
      }
    });
    save.addEventListener("click", () => {
      const values = {};
      card.querySelectorAll(".gt-values input").forEach((i) => { values[i.dataset.key] = Number(i.value); });
      values.compute_mode = box.checked;
      delete drafts[p.id];
      act("Saving…", "/api/gpu-tune", "PUT", { profiles: { [p.id]: values } });
    });
    actions.append(reset, save);
    card.append(actions, tuner(p));
    const runs = el("details", "gt-details");
    runs.append(el("summary", null, `Recorded jobs (${p.runs?.length || 0})`), runsTable(p.runs));
    card.append(runs);
    return card;
  }

  function renderLog() {
    const items = data.events.map((e) => {
      const li = el("li", `gt-log-${e.kind}`);
      li.append(el("time", null, ago(e.time)), document.createTextNode(` ${e.text}`));
      return li;
    });
    $("gt-log").replaceChildren(...(items.length ? items : [el("li", null, "Nothing yet.")]));
  }

  function render() {
    renderLive();
    renderAuto();
    // Don't rebuild the profile cards while someone is typing in them.
    if (!panel.querySelector(".gt-profiles :focus")) $("gt-profiles").replaceChildren(...data.profiles.map(profileCard));
    renderLog();
  }

  async function poll() {
    clearTimeout(timer);
    if (panel.hidden || document.hidden) return;
    try { if (!busy) { data = await call("/api/gpu-tune"); render(); } }
    catch (e) { status(e.message, "error"); }
    timer = setTimeout(poll, 3000);
  }

  $("gt-auto").addEventListener("change", (e) => {
    const on = e.target.checked;
    if (on && !confirm("Turn on the smart switch? Rocsy will change GPU clocks by itself when jobs start and stop.")) { e.target.checked = false; return; }
    act("Saving…", "/api/gpu-tune", "PUT", { auto: { enabled: on } });
  });
  $("gt-resume").addEventListener("click", () => act("Resuming…", "/api/gpu-tune/resume", "POST", {}));
  new MutationObserver(() => { if (!panel.hidden) poll(); else clearTimeout(timer); })
    .observe(panel, { attributes: true, attributeFilter: ["hidden"] });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  if (!panel.hidden) poll();
})();
