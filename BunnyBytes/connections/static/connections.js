// Connections page: websites the Master Agent works with (logins, scouting results, upload scripts),
// the Telegram output bot, the Hermes <-> llama-swap link and the Cloudflare tunnels.
(() => {
  const base = "/Connections";
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const ACTION = { "X-Connections-Action": "1", "Content-Type": "application/json" };

  async function api(path, options = {}) {
    const r = await fetch(base + path, { cache: "no-store", ...options });
    let data = {};
    try { data = await r.json(); } catch (_) { /* empty body */ }
    if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${r.status})`);
    return data;
  }
  const act = (path, method = "POST", body = {}) => api(path, { method, headers: ACTION, body: JSON.stringify(body) });

  function pill(node, text, tone = "") { node.textContent = text; node.dataset.tone = tone; }
  function stat(id, value, tone) { const tile = $(id); tile.querySelector("strong").textContent = value; tile.dataset.tone = tone; }
  const when = (t) => new Date(t * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  const hostOf = (url) => { try { return new URL(url).host.replace(/^www\./, ""); } catch (_) { return url; } };
  const hue = (text) => [...text].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7);

  /* ------------------------------------------------ websites -- */
  const STATES = {
    new: ["Not scouted", ""], scouting: ["Scouting…", "warn"], scouted: ["Scouted", "info"], automated: ["Automated", "ok"],
  };
  let accounts = [], editing = null, accountTimer = 0;

  function guideBlock(g) {
    const box = el("div", "cx-guide");
    if (g.summary) box.append(el("p", null, g.summary));
    const part = (title, ...nodes) => { const d = el("div"); d.append(el("h4", null, title), ...nodes); box.append(d); };
    if (g.fields.length) {
      const chips = el("div", "cx-chips");
      for (const f of g.fields) {
        const chip = el("span", "cx-chip" + (f.required ? " is-required" : ""), f.name);
        chip.title = (f.required ? "Required" : "Optional") + (f.notes ? ` · ${f.notes}` : "");
        chips.append(chip);
      }
      part("Fields  (* required)", chips);
    }
    const files = Object.entries(g.files || {}).map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`);
    if (files.length) { const ul = el("ul"); files.forEach((x) => ul.append(el("li", null, x))); part("Files", ul); }
    const login = Object.entries(g.login || {}).map(([k, v]) => (k === "how" ? v : `${k.replace(/_/g, " ")}: ${v}`));
    if (login.length) part("Login", el("p", null, login.join(" · ")));
    if (g.limits) part("Limits", el("p", null, g.limits));
    if (g.warnings.length) { const ul = el("ul"); g.warnings.forEach((x) => ul.append(el("li", "cx-warn", x))); part("Watch out", ul); }
    if (g.steps.length) {
      const d = el("details"); d.append(el("summary", null, `How it posts (${g.steps.length} steps)`));
      const ol = el("ol"); g.steps.forEach((x) => ol.append(el("li", null, x))); d.append(ol); box.append(d);
    }
    box.append(el("small", null, `Scouted ${when(g.updated)}`));
    return box;
  }

  function siteCard(a) {
    const card = el("article", "cx-site"); card.dataset.state = a.state;
    card.style.setProperty("--hue", hue(a.name));
    const head = el("div", "cx-site-head");
    const mono = el("span", "cx-mono", a.name.slice(0, 1).toUpperCase()); mono.setAttribute("aria-hidden", "true");
    const titles = el("div"); titles.append(el("h3", null, a.name));
    const link = el("a", "cx-host", hostOf(a.url)); link.href = a.url; link.target = "_blank"; link.rel = "noopener noreferrer"; titles.append(link);
    const status = el("span", "cx-pill"); pill(status, ...STATES[a.state]);
    head.append(mono, titles, status); card.append(head);
    if (a.purpose) card.append(el("p", "cx-purpose", a.purpose));
    const meta = el("dl", "cx-meta");
    const row = (k, v) => { const d = el("div"); d.append(el("dt", null, k), el("dd", null, v)); meta.append(d); };
    row("Account", a.username); row("Browser", a.browser === "headed" ? "Headed" : "Headless");
    if (a.script) row("Script", `${a.script} · ${when(a.script_updated)}`);
    card.append(meta);
    if (a.state === "scouting") {
      const box = el("div", "cx-scouting"); box.append(el("span", null, "The agent is working the site out."));
      const watch = el("a", null, "Watch"); watch.href = `/Agent/?s=${a.scout_session}`; box.append(watch); card.append(box);
    }
    if (a.guide) card.append(guideBlock(a.guide));
    else if (a.state !== "scouting") card.append(el("p", "cx-empty-guide", "Not scouted yet. Scout it to find out how to log in and what posting needs."));
    const actions = el("div", "cx-actions");
    const button = (text, cls, fn, title = "") => {
      const b = el("button", "cx-btn" + (cls ? ` ${cls}` : ""), text); b.type = "button"; if (title) b.title = title;
      b.addEventListener("click", async () => { b.disabled = true; try { await fn(); } catch (e) { alert(e.message); } finally { b.disabled = false; } });
      actions.append(b);
    };
    if (a.state !== "scouting") {
      button(a.guide ? "Scout again" : "Scout", a.guide ? "" : "cx-btn-primary", async () => {
        const fresh = await act(`/api/accounts/${a.id}/scout`);
        if (fresh.scout_session) window.open(`/Agent/?s=${fresh.scout_session}`, "_blank", "noopener");
        await refreshAccounts();
      }, "Starts a Master Agent session that logs in and works out the site. Nothing is posted.");
    }
    button("Edit", "cx-btn-ghost", async () => openDialog(a));
    if (a.guide || a.script) button("Start over", "cx-btn-ghost", async () => {
      if (!confirm(`Set the ${a.name} guide and script aside? The next scout or upload works the site out from scratch.`)) return;
      await act(`/api/accounts/${a.id}/reset-script`); await refreshAccounts();
    });
    button("Remove", "cx-btn-ghost cx-btn-danger", async () => {
      if (!confirm(`Remove the saved ${a.name} login?`)) return;
      await act(`/api/accounts/${a.id}`, "DELETE"); await refreshAccounts();
    });
    card.append(actions);
    return card;
  }

  async function refreshAccounts() {
    clearTimeout(accountTimer);
    const list = $("account-list");
    try {
      ({ accounts } = await api("/api/accounts"));
      const add = el("button", "cx-add-card"); add.type = "button";
      add.append(el("strong", null, "+"), el("span", null, accounts.length ? "Add another website" : "Add your first website"));
      add.addEventListener("click", () => openDialog(null));
      list.replaceChildren(...accounts.map(siteCard), add);
      const ready = accounts.filter((a) => a.state === "automated" || a.state === "scouted").length;
      stat("stat-accounts", accounts.length ? `${ready}/${accounts.length} ready` : "None",
        accounts.some((a) => a.state === "scouting") ? "warn" : accounts.length ? "ok" : "");
    } catch (e) { list.replaceChildren(el("p", "cx-detail", e.message)); stat("stat-accounts", "Error", "bad"); }
    // Faster while a scout runs, so its results appear when it finishes.
    accountTimer = setTimeout(refreshAccounts, accounts.some((a) => a.state === "scouting") ? 8000 : 30000);
  }

  const dialog = $("site-dialog"), form = $("site-form");
  function openDialog(account) {
    editing = account;
    form.reset();
    $("site-error").hidden = true;
    $("site-dialog-title").textContent = account ? `Edit ${account.name}` : "Add a website";
    if (account) {
      for (const key of ["name", "url", "username", "purpose"]) form.elements[key].value = account[key];
      form.elements.browser.value = account.browser;
    }
    form.elements.password.placeholder = account?.password_set ? "Keep current password" : "";
    form.elements.password.required = !account?.password_set;
    dialog.showModal();
    form.elements[account ? "username" : "name"].focus();
  }
  $("site-add").addEventListener("click", () => openDialog(null));
  $("site-cancel").addEventListener("click", () => dialog.close());
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const save = $("site-save"); save.disabled = true;
    const body = Object.fromEntries(["name", "url", "username", "password", "purpose", "browser"].map((k) => [k, form.elements[k].value]));
    try {
      await act(editing ? `/api/accounts/${editing.id}` : "/api/accounts", editing ? "PUT" : "POST", body);
      dialog.close();
      await refreshAccounts();
    } catch (err) { $("site-error").textContent = err.message; $("site-error").hidden = false; }
    finally { save.disabled = false; form.elements.password.value = ""; }
  });

  /* ------------------------------------------------ telegram -- */
  async function refreshTelegram() {
    try {
      const s = await api("/api/status");
      const toggle = $("toggle");
      pill($("state"), s.running ? "Running" : s.enabled ? "Starting" : "Paused", s.running ? "ok" : s.enabled ? "warn" : "");
      $("detail").textContent = (!s.configured ? "The bot isn't configured yet. " : s.outputs_available ? "Watching ComfyUI and H3 Machine outputs. "
        : "An output folder is unavailable. ") + (s.last_error || "");
      toggle.textContent = s.enabled ? "Pause watcher" : "Start watcher";
      toggle.disabled = !s.enabled && (!s.configured || !s.outputs_available);
      $("log").textContent = s.recent_log.join("\n") || "Nothing logged yet.";
      stat("stat-telegram", s.running ? "Running" : s.configured ? "Paused" : "Not set up", s.running ? "ok" : s.configured ? "warn" : "");
    } catch (e) { pill($("state"), "Unavailable", "bad"); $("detail").textContent = e.message; stat("stat-telegram", "Unavailable", "bad"); }
  }
  $("toggle").addEventListener("click", async () => {
    try { await act(`/api/watcher/${$("toggle").textContent === "Pause watcher" ? "stop" : "start"}`); } catch (e) { alert(e.message); }
    refreshTelegram();
  });
  const telegramForm = $("telegram-form");
  $("telegram-edit").addEventListener("click", async () => {
    try {
      const data = await api("/api/telegram/config");
      $("telegram-chat").value = data.chat_id || ""; $("telegram-token").value = "";
      $("telegram-token-hint").textContent = data.token_hint ? `Current token: ${data.token_hint}. Leave blank to keep it.` : "Paste the token from BotFather.";
      telegramForm.hidden = false; $("telegram-token").focus();
    } catch (e) { alert(e.message); }
  });
  $("telegram-cancel").addEventListener("click", () => { telegramForm.hidden = true; });
  telegramForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const submit = telegramForm.querySelector('button[type="submit"]'); submit.disabled = true;
    try {
      await act("/api/telegram/config", "PUT", { bot_token: $("telegram-token").value, chat_id: $("telegram-chat").value });
      telegramForm.hidden = true; await refreshTelegram();
    } catch (err) { alert(err.message); } finally { submit.disabled = false; }
  });

  /* ------------------------------------------------ hermes link -- */
  async function refreshHermes() {
    const select = $("hermes-model"), connect = $("hermes-connect"), disconnect = $("hermes-disconnect");
    try {
      const s = await api("/api/hermes/status");
      pill($("hermes-state"), s.connected ? "Connected" : s.configured ? "Model unavailable" : "Not connected", s.connected ? "ok" : s.configured ? "warn" : "");
      $("hermes-detail").textContent = s.error || (s.connected ? `Hermes alias ${s.alias} uses ${s.model} through the local llama-swap API.`
        : s.configured ? `Hermes is set to ${s.model}, which llama-swap doesn't list right now.` : "Choose a model and connect Hermes to llama-swap.");
      const previous = select.value;
      select.replaceChildren(...s.models.map((m) => new Option(m, m)));
      select.value = s.models.includes(previous) ? previous : s.models.includes(s.model) ? s.model
        : s.models.includes("qwen3.8-flash-next-uncensored") ? "qwen3.8-flash-next-uncensored" : s.models[0] || "";
      select.disabled = connect.disabled = !s.models.length;
      connect.textContent = s.connected ? "Update connection" : "Connect Hermes";
      disconnect.disabled = !s.configured;
      stat("stat-hermes", s.connected ? "Connected" : s.configured ? "Model missing" : "Off", s.connected ? "ok" : s.configured ? "warn" : "");
    } catch (e) {
      pill($("hermes-state"), "Unavailable", "bad"); $("hermes-detail").textContent = e.message;
      select.disabled = connect.disabled = disconnect.disabled = true; stat("stat-hermes", "Unavailable", "bad");
    }
  }
  $("hermes-connect").addEventListener("click", async () => {
    const b = $("hermes-connect"); b.disabled = true;
    try { await act("/api/hermes/connect", "POST", { model: $("hermes-model").value }); await refreshHermes(); } catch (e) { alert(e.message); b.disabled = false; }
  });
  $("hermes-disconnect").addEventListener("click", async () => {
    if (!confirm("Disconnect Rocsy from Hermes? Its other models and API key stay in place.")) return;
    const b = $("hermes-disconnect"); b.disabled = true;
    try { await act("/api/hermes/disconnect"); await refreshHermes(); } catch (e) { alert(e.message); b.disabled = false; }
  });

  /* ------------------------------------------------ cloudflare -- */
  async function refreshCloudflare() {
    try {
      const s = await api("/api/cloudflare");
      pill($("cf-state"), s.connected ? "Connected" : s.configured ? "Disconnected" : "Not configured", s.connected ? "ok" : s.configured ? "bad" : "");
      const count = `${s.active_connections} active edge connection${s.active_connections === 1 ? "" : "s"}`;
      $("cf-detail").textContent = (s.configured ? `Tunnel ${s.tunnel_id} · ${count}` : s.error || "No tunnel configured") + (s.error && s.configured ? ` · ${s.error}` : "");
      $("cf-connections").textContent = s.metrics_available ? s.active_connections : "—";
      $("cf-requests").textContent = s.total_requests === null ? "—" : s.total_requests.toLocaleString();
      $("cf-errors").textContent = s.request_errors === null ? "—" : s.request_errors.toLocaleString();
      $("cf-routes").replaceChildren(...s.routes.map((r) => { const line = el("div", "cx-route"); line.append(el("b", null, r.hostname), el("span", null, "→"), el("code", null, r.origin)); return line; }));
      stat("stat-tunnel", s.connected ? "Connected" : s.configured ? "Down" : "None", s.connected ? "ok" : s.configured ? "bad" : "");
    } catch (e) { pill($("cf-state"), "Unavailable", "bad"); $("cf-detail").textContent = e.message; stat("stat-tunnel", "Unavailable", "bad"); }
  }

  function routeLines(raw) {
    return raw.split(/\n/).map((x) => x.trim()).filter(Boolean).map((line) => {
      const i = line.indexOf(" ");
      if (i < 1) throw new Error("Use hostname origin, one per line");
      return { hostname: line.slice(0, i), origin: line.slice(i + 1).trim() };
    });
  }
  async function tunnelAction(id, action) {
    if ((action === "stop" || action === "restart") && !confirm(`${action[0].toUpperCase() + action.slice(1)} this tunnel? Public access may drop briefly.`)) return;
    await act(`/api/tunnels/${encodeURIComponent(id)}/${action}`);
    await refreshTunnels();
  }
  async function refreshTunnels() {
    const list = $("tunnel-list");
    try {
      const data = await api("/api/tunnels");
      list.replaceChildren(...data.tunnels.map((t) => {
        const box = el("div", "cx-tunnel");
        const head = el("div", "cx-tunnel-head");
        const name = el("div"); name.append(el("strong", null, t.name), el("small", null, ` · ${t.active_connections} edge connections${t.managed ? "" : " · legacy Docker service"}`));
        const state = el("span", "cx-pill"); pill(state, t.connected ? "Connected" : t.enabled ? "Starting" : "Stopped", t.connected ? "ok" : t.enabled ? "warn" : "");
        head.append(name, state); box.append(head);
        if (t.managed) {
          const area = el("textarea"); area.rows = Math.max(2, t.routes.length); area.value = t.routes.map((x) => `${x.hostname} ${x.origin}`).join("\n");
          area.setAttribute("aria-label", `${t.name} routes`);
          const actions = el("div", "cx-actions");
          for (const action of ["start", "stop", "restart"]) {
            const b = el("button", "cx-btn cx-btn-ghost", action[0].toUpperCase() + action.slice(1)); b.type = "button";
            b.addEventListener("click", () => tunnelAction(t.id, action).catch((e) => alert(e.message)));
            actions.append(b);
          }
          const save = el("button", "cx-btn", "Save routes"); save.type = "button";
          save.addEventListener("click", async () => {
            try { await act(`/api/tunnels/${encodeURIComponent(t.id)}/routes`, "PUT", { routes: routeLines(area.value) }); await refreshTunnels(); } catch (e) { alert(e.message); }
          });
          actions.append(save); box.append(area, actions);
        }
        return box;
      }));
      if (!data.tunnels.length) list.replaceChildren(el("p", "cx-detail", "No tunnels yet."));
    } catch (e) { list.replaceChildren(el("p", "cx-detail", e.message)); }
  }
  $("import-tunnel").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const form = new FormData(e.target);
      form.set("routes", JSON.stringify(routeLines(form.get("routes"))));
      await api("/api/tunnels/import", { method: "POST", headers: { "X-Connections-Action": "1" }, body: form });
      e.target.reset(); alert("Tunnel imported. Point a proxied CNAME for its hostname at it."); refreshTunnels();
    } catch (err) { alert(err.message); }
  });
  $("create-tunnel").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const body = Object.fromEntries(new FormData(e.target).entries());
      body.routes = routeLines(body.routes);
      const data = await act("/api/tunnels/create", "POST", body);
      e.target.reset();
      alert(data.dns_errors.length ? `Tunnel created, but DNS needs attention: ${data.dns_errors.join("; ")}` : "Tunnel and DNS routes created");
      refreshTunnels();
    } catch (err) { alert(err.message); } finally { e.target.elements.api_token.value = ""; }
  });

  /* ------------------------------------------------ polling -- */
  const polls = [[refreshTelegram, 5000], [refreshHermes, 10000], [refreshCloudflare, 10000]];
  for (const [fn, every] of polls) { fn(); setInterval(() => { if (!document.hidden) fn(); }, every); }
  refreshTunnels();
  refreshAccounts();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshAccounts(); });
})();
