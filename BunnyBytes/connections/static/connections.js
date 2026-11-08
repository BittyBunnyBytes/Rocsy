// Connections page: websites the Master Agent works with (logins, scouting results, upload scripts),
// scheduled posts to them, the Telegram output bot, the Hermes <-> llama-swap link and the Cloudflare tunnels.
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
  let accounts = [], websites = [], editing = null, accountTimer = 0;
  const RUNNING = ["starting", "waiting_for_gpu", "thinking", "rendering"];

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

  /* ------------------------------------------------ shared bits -- */
  function makeButton(parent, text, cls, fn, title = "") {
    const b = el("button", "cx-btn" + (cls ? ` ${cls}` : ""), text); b.type = "button"; if (title) b.title = title;
    b.addEventListener("click", async () => {
      b.closest("details.cx-menu")?.removeAttribute("open");
      b.disabled = true; try { await fn(); } catch (e) { alert(e.message); } finally { b.disabled = false; }
    });
    parent.append(b); return b;
  }
  // "⋯" overflow menu: the less used buttons of a card.
  function menu() {
    const d = el("details", "cx-menu"); const s = el("summary", null, "⋯"); s.setAttribute("aria-label", "More actions"); s.title = "More";
    const list = el("div", "cx-menu-list"); d.append(s, list); return { node: d, list };
  }
  document.addEventListener("click", (e) => {
    for (const m of document.querySelectorAll("details.cx-menu[open]")) if (!m.contains(e.target)) m.removeAttribute("open");
  });
  function copyButton(parent, label, value) {
    const copy = el("button", "cx-btn cx-btn-ghost cx-btn-sm", label); copy.type = "button";
    copy.addEventListener("click", async (e) => { e.preventDefault(); await navigator.clipboard.writeText(value); copy.textContent = "Copied"; setTimeout(() => (copy.textContent = label), 1500); });
    parent.append(copy); return copy;
  }
  // Masked password with Show/Hide + Copy. get() may be async (fetched on first use).
  function passwordField(parent, get) {
    const box = el("span", "cx-secret"); const text = el("code", null, "••••••••"); let value = null; let shown = false;
    const load = async () => (value ??= String(await get()));
    const show = el("button", "cx-btn cx-btn-ghost cx-btn-sm", "Show"); show.type = "button";
    show.addEventListener("click", async (e) => {
      e.preventDefault(); e.stopPropagation();
      try { shown = !shown; text.textContent = shown ? (await load()) || "(none saved)" : "••••••••"; show.textContent = shown ? "Hide" : "Show"; }
      catch (err) { shown = false; alert(err.message); }
    });
    const copy = el("button", "cx-btn cx-btn-ghost cx-btn-sm", "Copy"); copy.type = "button";
    copy.addEventListener("click", async (e) => {
      e.preventDefault(); e.stopPropagation();
      try { await navigator.clipboard.writeText(await load()); copy.textContent = "Copied"; setTimeout(() => (copy.textContent = "Copy"), 1500); }
      catch (err) { alert(err.message); }
    });
    box.append(text, show, copy); parent.append(box); return box;
  }
  const accountPassword = (id) => async () => (await api(`/api/accounts/${id}/password`)).password;

  /* ------------------------------------------------ websites -- */
  function websiteCard(w) {
    const card = el("article", "cx-site"); card.dataset.state = w.state;
    card.style.setProperty("--hue", hue(w.name));
    const head = el("div", "cx-site-head");
    const mono = el("span", "cx-mono", w.name.slice(0, 1).toUpperCase()); mono.setAttribute("aria-hidden", "true");
    const titles = el("div"); titles.append(el("h3", null, w.name));
    const link = el("a", "cx-host", hostOf(w.url)); link.href = w.url; link.target = "_blank"; link.rel = "noopener noreferrer"; titles.append(link);
    const status = el("span", "cx-pill"); pill(status, ...STATES[w.state]);
    head.append(mono, titles, status); card.append(head);
    const facts = [w.accounts.length ? `${w.accounts.length} account${w.accounts.length === 1 ? "" : "s"}` : "No accounts",
      w.script ? "upload script" : "", w.action_scripts.length ? `${w.action_scripts.length} action script${w.action_scripts.length === 1 ? "" : "s"}` : ""].filter(Boolean);
    card.append(el("p", "cx-facts", facts.join(" · ")));
    if (w.notes) card.append(el("p", "cx-notes cx-clamp", w.notes));
    if (w.state === "scouting") {
      const box = el("div", "cx-scouting"); box.append(el("span", null, "The agent is working the site out."));
      const watch = el("a", null, "Watch"); watch.href = `/Agent/?s=${w.scout_session}`; box.append(watch); card.append(box);
    } else if (w.guide) {
      const d = el("details", "cx-fold"); d.append(el("summary", null, w.guide.summary ? `Site guide · ${w.guide.summary}` : "Site guide"), guideBlock(w.guide)); card.append(d);
    }
    if (w.last_action) card.append(el("p", "cx-facts", `Last action: ${w.last_action.task.slice(0, 70)} · ${actionState(w.last_action.state)}`));
    const actions = el("div", "cx-actions");
    if (w.state !== "scouting") makeButton(actions, w.guide ? "Scout again" : "Scout", w.guide ? "" : "cx-btn-primary", async () => openScoutDialog(w),
      "The agent looks around (logged in or out) and writes the site guide. Nothing is posted.");
    makeButton(actions, "Action", "", async () => openActionDialog(siteTarget(w)), "Give the agent a task on this site without an account, e.g. sign up new accounts.");
    const more = menu();
    makeButton(more.list, "Add an account", "cx-btn-ghost", async () => openDialog(null, w.id));
    if (w.last_action) makeButton(more.list, `Action log (${w.last_action.count})`, "cx-btn-ghost", async () => openActionLog(siteTarget(w)));
    makeButton(more.list, "Scripts & notes", "cx-btn-ghost", async () => { showTab("library"); document.getElementById(`lib-${w.id}`)?.scrollIntoView({ block: "start" }); });
    makeButton(more.list, "Edit", "cx-btn-ghost", async () => openWebsiteDialog(w));
    if (w.guide || w.script) makeButton(more.list, "Start over", "cx-btn-ghost", async () => {
      if (!confirm(`Set the ${w.name} guide and upload script aside? The next scout or upload works the site out from scratch.`)) return;
      await act(`/api/websites/${w.id}/reset-script`); await refreshAccounts();
    });
    if (!w.accounts.length) makeButton(more.list, "Remove", "cx-btn-ghost cx-btn-danger", async () => {
      if (!confirm(`Remove ${w.name}? Its guide and scripts are kept.`)) return;
      await act(`/api/websites/${w.id}`, "DELETE"); await refreshAccounts();
    });
    actions.append(more.node); card.append(actions);
    return card;
  }

  /* ------------------------------------------------ accounts (grouped by website) -- */
  function accountRow(a) {
    const row = el("div", "cx-row");
    const who = el("div", "cx-row-main");
    const name = el("strong", null, a.name);
    who.append(name);
    if (a.email) who.append(el("small", null, `Email: ${a.email}`));
    if (a.username && a.username !== a.email) who.append(el("small", null, `Username: ${a.username}`));
    if (a.password_set) { const pw = el("small", "cx-row-secret", "Password: "); passwordField(pw, accountPassword(a.id)); who.append(pw); }
    if (a.purpose) who.append(el("small", "cx-row-purpose", a.purpose));
    const side = el("div", "cx-row-side");
    if (a.created_by) side.append(el("span", "cx-tag", "Created by agent"));
    side.append(el("span", "cx-tag", a.browser === "headed" ? "Headed" : "Headless"));
    if (a.last_action) side.append(el("small", null, `Last action ${actionState(a.last_action.state).toLowerCase()} · ${when(a.last_action.t)}`));
    const actions = el("div", "cx-actions");
    makeButton(actions, "Action", "cx-btn-sm", async () => openActionDialog(accountTarget(a)), "Give the agent a task to do as this account.");
    const more = menu();
    makeButton(more.list, "Schedule a post", "cx-btn-ghost", async () => openPostDialog(null, a.id));
    if (a.last_action) makeButton(more.list, `Action log (${a.last_action.count})`, "cx-btn-ghost", async () => openActionLog(accountTarget(a)));
    makeButton(more.list, "Edit", "cx-btn-ghost", async () => openDialog(a));
    makeButton(more.list, "Remove", "cx-btn-ghost cx-btn-danger", async () => {
      if (!confirm(`Remove the saved ${a.name} login? The ${a.site_name} guide and scripts stay.`)) return;
      await act(`/api/accounts/${a.id}`, "DELETE"); await refreshAccounts(); refreshEmails();
    });
    actions.append(more.node);
    row.append(who, side, actions);
    return row;
  }
  function accountGroups() {
    const groups = websites.map((w) => {
      const mine = accounts.filter((a) => a.site === w.id);
      const box = el("section", "cx-group"); box.style.setProperty("--hue", hue(w.name));
      const head = el("div", "cx-group-head");
      head.append(el("span", "cx-mono cx-mono-sm", w.name.slice(0, 1).toUpperCase()), el("h3", null, w.name), el("small", null, hostOf(w.url)));
      const add = el("button", "cx-btn cx-btn-ghost cx-btn-sm", "+ Account"); add.type = "button"; add.addEventListener("click", () => openDialog(null, w.id));
      head.append(add); box.append(head);
      box.append(...(mine.length ? mine.map(accountRow) : [el("p", "cx-detail cx-row-empty", "No accounts on this site yet.")]));
      return box;
    });
    return groups.length ? groups : [el("p", "cx-empty", "Add a website first (Websites tab), then its accounts.")];
  }

  function addCard(text, fn) {
    const add = el("button", "cx-add-card"); add.type = "button";
    add.append(el("strong", null, "+"), el("span", null, text)); add.addEventListener("click", fn); return add;
  }

  async function refreshAccounts() {
    clearTimeout(accountTimer);
    const list = $("account-list"), siteList = $("website-list");
    try {
      let automation;
      [{ websites, automation }, { accounts }] = await Promise.all([api("/api/websites"), api("/api/accounts")]);
      for (const key of ["use_scripts", "write_scripts"]) $("automation").querySelector(`[name=${key}]`).checked = automation[key];
      siteList.replaceChildren(...websites.map(websiteCard), addCard(websites.length ? "Add another website" : "Add your first website", () => openWebsiteDialog(null)));
      list.replaceChildren(...accountGroups());
      const ready = websites.filter((w) => w.state === "automated" || w.state === "scouted").length;
      stat("stat-websites", websites.length ? `${ready}/${websites.length}` : "0",
        websites.some((w) => w.state === "scouting") ? "warn" : websites.length ? "ok" : "");
      stat("stat-accounts", String(accounts.length), accounts.some((a) => RUNNING.includes(a.last_action?.state)) ? "warn" : accounts.length ? "ok" : "");
    } catch (e) { siteList.replaceChildren(el("p", "cx-detail", e.message)); stat("stat-accounts", "!", "bad"); stat("stat-websites", "!", "bad"); }
    // Faster while a scout or an action runs, so its results appear when it finishes.
    const busy = websites.some((w) => w.state === "scouting" || RUNNING.includes(w.last_action?.state)) || accounts.some((a) => RUNNING.includes(a.last_action?.state));
    accountTimer = setTimeout(refreshAccounts, busy ? 8000 : 30000);
  }

  $("automation").addEventListener("change", async (e) => {
    const box = e.target; box.disabled = true;
    try { await act("/api/automation", "PUT", { [box.name]: box.checked }); refreshLibrary(); }
    catch (err) { box.checked = !box.checked; alert(err.message); }
    finally { box.disabled = false; }
  });

  const websiteDialog = $("website-dialog"), websiteForm = $("website-form");
  let editingSite = null;
  function openWebsiteDialog(w) {
    editingSite = w; websiteForm.reset(); $("website-error").hidden = true;
    $("website-dialog-title").textContent = w ? `Edit ${w.name}` : "Add a website";
    if (w) for (const key of ["name", "url", "notes"]) websiteForm.elements[key].value = w[key];
    websiteDialog.showModal(); websiteForm.elements.name.focus();
  }
  $("website-add").addEventListener("click", () => openWebsiteDialog(null));
  $("website-cancel").addEventListener("click", () => websiteDialog.close());
  websiteForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const save = $("website-save"); save.disabled = true;
    const body = Object.fromEntries(["name", "url", "notes"].map((k) => [k, websiteForm.elements[k].value]));
    try {
      await act(editingSite ? `/api/websites/${editingSite.id}` : "/api/websites", editingSite ? "PUT" : "POST", body);
      websiteDialog.close(); await refreshAccounts(); refreshLibrary();
    } catch (err) { $("website-error").textContent = err.message; $("website-error").hidden = false; }
    finally { save.disabled = false; }
  });

  const scoutDialog = $("scout-dialog"), scoutForm = $("scout-form");
  let scoutSite = null;
  function openScoutDialog(w) {
    scoutSite = w; scoutForm.reset(); $("scout-error").hidden = true;
    $("scout-dialog-title").textContent = `Scout ${w.name}`;
    scoutForm.elements.note.value = w.scout_note || "";
    const none = el("option", null, "No account (stay logged out)"); none.value = "";
    scoutForm.elements.account.replaceChildren(none, ...w.accounts.map((a) => { const o = el("option", null, `${a.name} (${a.login})`); o.value = a.id; return o; }));
    scoutForm.elements.account.value = w.accounts[0]?.id || "";
    scoutDialog.showModal(); scoutForm.elements.note.focus();
  }
  $("scout-cancel").addEventListener("click", () => scoutDialog.close());
  scoutForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const go = $("scout-run"); go.disabled = true;
    try {
      const fresh = await act(`/api/websites/${scoutSite.id}/scout`, "POST", { account: scoutForm.elements.account.value, note: scoutForm.elements.note.value });
      scoutDialog.close();
      if (fresh.scout_session) window.open(`/Agent/?s=${fresh.scout_session}`, "_blank", "noopener");
      await refreshAccounts();
    } catch (err) { $("scout-error").textContent = err.message; $("scout-error").hidden = false; }
    finally { go.disabled = false; }
  });

  const dialog = $("site-dialog"), form = $("site-form");
  function openDialog(account, site = "") {
    if (!websites.length) { alert("Add the website first, then its accounts."); showTab("websites"); openWebsiteDialog(null); return; }
    editing = account;
    form.reset();
    $("site-error").hidden = true;
    $("site-dialog-title").textContent = account ? `Edit ${account.name}` : "Add an account";
    form.elements.site.replaceChildren(...websites.map((w) => { const o = el("option", null, `${w.name} (${hostOf(w.url)})`); o.value = w.id; return o; }));
    form.elements.site.value = account?.site || site || websites[0].id;
    if (account) {
      for (const key of ["name", "username", "email", "purpose", "notes"]) form.elements[key].value = account[key];
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
    const body = Object.fromEntries(["site", "name", "username", "email", "password", "purpose", "notes", "browser"].map((k) => [k, form.elements[k].value]));
    try {
      await act(editing ? `/api/accounts/${editing.id}` : "/api/accounts", editing ? "PUT" : "POST", body);
      dialog.close();
      await refreshAccounts(); refreshLibrary(); refreshEmails();
    } catch (err) { $("site-error").textContent = err.message; $("site-error").hidden = false; }
    finally { save.disabled = false; form.elements.password.value = ""; }
  });

  /* ------------------------------------------------ emails -- */
  async function refreshEmails() {
    const box = $("emails-list");
    try {
      const { emails } = await api("/api/emails");
      stat("stat-emails", String(emails.length), emails.length ? "ok" : "");
      if (!emails.length) { box.replaceChildren(el("p", "cx-empty", "No email addresses yet. Add an account with an email, or have the agent create a mailbox with Action.")); return; }
      const table = el("table", "cx-table"); const head = el("tr");
      ["Email", "Password", "Where", "Account", "Source", "Added", ""].forEach((h) => head.append(el("th", null, h)));
      const thead = el("thead"); thead.append(head); table.append(thead);
      const body = el("tbody");
      for (const m of emails) {
        const tr = el("tr");
        const addr = el("td"); addr.append(el("strong", null, m.email)); if (m.note) addr.append(el("small", null, m.note));
        const tools = el("td", "cx-table-tools");
        copyButton(tools, "Copy", m.email);
        const pw = el("td");
        if (m.password) passwordField(pw, () => m.password);
        else if (m.account) passwordField(pw, accountPassword(m.account));
        else pw.textContent = "—";
        const src = el("td"); src.append(el("span", "cx-tag" + (m.source === "agent" ? " is-agent" : ""), m.source === "agent" ? "Created by agent" : "Connected"));
        tr.append(addr, pw, el("td", null, m.site_name || "—"), el("td", null, m.account_name || "—"), src, el("td", null, m.t ? when(m.t) : "—"), tools);
        body.append(tr);
      }
      table.append(body);
      const wrap = el("div", "cx-table-wrap"); wrap.append(table); box.replaceChildren(wrap);
    } catch (e) { box.replaceChildren(el("p", "cx-detail", e.message)); stat("stat-emails", "!", "bad"); }
  }
  $("emails-refresh").addEventListener("click", refreshEmails);

  /* ------------------------------------------------ scripts & notes -- */
  function codeBlock(title, code, updated, open = false) {
    const d = el("details", "cx-code"); d.open = open;
    const sum = el("summary"); sum.append(el("strong", null, title)); if (updated) sum.append(el("small", null, ` · ${when(updated)}`));
    copyButton(sum, "Copy", code);
    d.append(sum, el("pre", "cx-log", code || "(empty)")); return d;
  }
  function prettyJson(text) { try { return JSON.stringify(JSON.parse(text), null, 2); } catch (_) { return text; } }
  function libraryEntry(w) {
    const box = el("details", "cx-lib-site"); box.id = `lib-${w.id}`; box.style.setProperty("--hue", hue(w.name));
    const head = el("summary", "cx-site-head");
    head.append(el("span", "cx-mono cx-mono-sm", w.name.slice(0, 1).toUpperCase()));
    const titles = el("div"); titles.append(el("h3", null, w.name), el("small", null, hostOf(w.url)));
    head.append(titles, el("small", "cx-detail", `${w.scripts.length} script${w.scripts.length === 1 ? "" : "s"}${w.guide ? " · guide" : ""}`));
    box.append(head);
    const inner = el("div", "cx-lib-body"); box.append(inner);
    const part = (title, ...nodes) => { const d = el("div", "cx-lib-part"); d.append(el("h4", null, title), ...nodes); inner.append(d); };
    const notes = [];
    if (w.notes) notes.push(["Website", w.notes]);
    for (const a of w.accounts) {
      if (a.purpose) notes.push([`${a.name}: what it's for`, a.purpose]);
      if (a.notes) notes.push([`${a.name}: notes`, a.notes]);
    }
    if (notes.length) {
      const dl = el("dl", "cx-lib-notes");
      for (const [k, v] of notes) dl.append(el("dt", null, k), el("dd", "cx-notes", v));
      part("Your notes", dl);
    }
    part("Site guide", w.guide ? codeBlock(`${w.id}.json`, prettyJson(w.guide), w.guide_updated) : el("p", "cx-detail", "Not scouted yet."));
    part("Scripts", ...(w.scripts.length
      ? w.scripts.map((s) => codeBlock(`${s.kind === "upload" ? "Upload" : "Action"} · ${s.name}`, s.code, s.updated))
      : [el("p", "cx-detail", "No scripts yet.")]));
    if (w.old.length) {
      const d = el("details"); d.append(el("summary", null, `Set aside (${w.old.length})`), ...w.old.map((s) => codeBlock(s.name, s.name.endsWith(".json") ? prettyJson(s.code) : s.code, s.updated)));
      part("Older versions", d);
    }
    return box;
  }
  async function refreshLibrary() {
    const list = $("library-list");
    try {
      const { websites: sites, automation } = await api("/api/library");
      list.replaceChildren(...(sites.length ? sites.map(libraryEntry) : [el("p", "cx-empty", "No websites yet.")]));
      if (sites.length === 1) list.querySelector("details").open = true;
      const count = sites.reduce((n, w) => n + w.scripts.length, 0);
      stat("stat-library", String(count), !automation.use_scripts || !automation.write_scripts ? "warn" : count ? "ok" : "");
    } catch (e) { list.replaceChildren(el("p", "cx-detail", e.message)); stat("stat-library", "!", "bad"); }
  }
  $("library-refresh").addEventListener("click", refreshLibrary);

  /* ------------------------------------------------ actions -- */
  // A target is an account (logged in) or a website (logged out); both have an action endpoint and a log.
  const accountTarget = (a) => ({ base: `/api/accounts/${a.id}`, title: `${a.name} on ${a.site_name}`,
    hint: "It logs in as this account and follows your notes for it and its website." });
  const siteTarget = (w) => ({ base: `/api/websites/${w.id}`, title: `${w.name} (no account)`,
    hint: "It works on the site logged out, e.g. signing up new accounts. Logins it creates are added to Accounts by themselves." });
  const ACTION_STATES = {
    starting: ["Starting…", "warn"], waiting_for_gpu: ["Waiting for GPU", "warn"], thinking: ["Running…", "warn"], rendering: ["Running…", "warn"],
    idle: ["Done", "ok"], done: ["Done", "ok"], failed: ["Failed", "bad"], stopped: ["Stopped", "bad"], interrupted: ["Interrupted", "bad"], unknown: ["Unknown", ""],
  };
  const actionState = (s) => ACTION_STATES[s]?.[0] || s;
  const LOG_KINDS = { say: "Said", tool: "Did", assistant: "Reply", approval: "Asked", warn: "Warning", error: "Error" };
  let actionTarget = null;
  const actionDialog = $("action-dialog"), actionForm = $("action-form"), logDialog = $("actlog-dialog");

  function openActionDialog(target, task = "") {
    actionTarget = target; actionForm.reset(); $("action-error").hidden = true;
    $("action-dialog-title").textContent = `Action · ${target.title}`;
    $("action-hint").textContent = `${target.hint} Passwords for anything it creates are saved, never shown in the chat. After the first run it saves a script, so the same task runs automatically next time.`;
    actionForm.elements.task.value = task;
    actionDialog.showModal(); actionForm.elements.task.focus();
  }
  async function runAction(target, task) {
    const run = await act(`${target.base}/action`, "POST", { task });
    window.open(`/Agent/?s=${run.session}`, "_blank", "noopener");
    await refreshAccounts();
    if (logDialog.open) await renderActionLog();
  }
  $("action-cancel").addEventListener("click", () => actionDialog.close());
  actionForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const go = $("action-run"); go.disabled = true;
    try { await runAction(actionTarget, actionForm.elements.task.value); actionDialog.close(); }
    catch (err) { $("action-error").textContent = err.message; $("action-error").hidden = false; }
    finally { go.disabled = false; }
  });

  function createdBlock(created) {
    if (!created.length) return el("div");
    const box = el("div", "cx-guide"); box.append(el("h4", null, `Logins it created (${created.length})`));
    const ul = el("ul", "cx-created");
    for (const c of created) {
      const li = el("li"); li.append(el("strong", null, c.email || c.username || "(no name)"));
      if (c.email && c.username && c.username !== c.email) li.append(el("span", null, ` · ${c.username}`));
      if (c.url) li.append(el("span", null, ` · ${hostOf(c.url)}`));
      if (c.t) li.append(el("small", null, ` · ${when(c.t)}`));
      if (c.note) li.append(el("div", "cx-detail", c.note));
      if (c.password) { const pw = el("div", "cx-detail", "Password: "); passwordField(pw, () => c.password); li.append(pw); }
      ul.append(li);
    }
    box.append(ul); return box;
  }
  function actionEntry(r) {
    const d = el("details", "cx-actlog-run"); const sum = el("summary");
    const status = el("span", "cx-pill"); pill(status, ...(ACTION_STATES[r.state] || [r.state || "Old", ""]));
    sum.append(status, el("strong", null, r.task.length > 120 ? r.task.slice(0, 120) + "…" : r.task), el("small", null, when(r.t)));
    d.append(sum);
    const tools = el("div", "cx-actions");
    const watch = el("a", "cx-btn cx-btn-ghost cx-btn-sm", "Open session"); watch.href = `/Agent/?s=${r.session}`; watch.target = "_blank"; watch.rel = "noopener";
    tools.append(watch);
    makeButton(tools, "Run again", "cx-btn-sm", async () => runAction(actionTarget, r.task));
    makeButton(tools, "Edit and run", "cx-btn-ghost cx-btn-sm", async () => openActionDialog(actionTarget, r.task));
    d.append(tools);
    if (r.task.length > 120) d.append(el("p", "cx-notes", r.task));
    const ol = el("ol", "cx-actlog-steps");
    for (const x of r.log) {
      const li = el("li"); li.dataset.kind = x.kind;
      li.append(el("span", "cx-actlog-kind", LOG_KINDS[x.kind] || x.kind), el("span", "cx-actlog-text", x.text));
      if (x.t) li.title = when(x.t);
      ol.append(li);
    }
    if (!r.log.length) ol.append(el("li", "cx-detail", r.state ? "No steps yet." : "Older run: open the session to see its steps."));
    d.append(ol); return d;
  }
  async function renderActionLog() {
    const list = $("actlog-list");
    try {
      const { actions, created } = await api(`${actionTarget.base}/actions`);
      $("actlog-created").replaceChildren(createdBlock(created));
      list.replaceChildren(...(actions.length ? actions.map(actionEntry) : [el("p", "cx-detail", "No actions yet.")]));
      const first = list.querySelector("details"); if (first) first.open = true;
    } catch (e) { list.replaceChildren(el("p", "cx-detail", e.message)); }
  }
  async function openActionLog(target) {
    actionTarget = target; $("actlog-title").textContent = `Action log · ${target.title}`;
    $("actlog-list").replaceChildren(el("p", "cx-detail", "Loading…")); $("actlog-created").replaceChildren();
    logDialog.showModal(); await renderActionLog();
  }
  $("actlog-refresh").addEventListener("click", renderActionLog);
  $("actlog-close").addEventListener("click", () => logDialog.close());

  /* ------------------------------------------------ tabs -- */
  const TABS = [...document.querySelectorAll(".cx-tab")].map((b) => b.dataset.tab);
  function showTab(name, remember = true) {
    if (!TABS.includes(name)) name = "websites";
    for (const b of document.querySelectorAll(".cx-tab")) { const on = b.dataset.tab === name; b.setAttribute("aria-selected", on); b.classList.toggle("is-active", on); }
    for (const p of document.querySelectorAll(".cx-panel")) p.hidden = p.dataset.tab !== name;
    if (remember) { try { localStorage.setItem("cx-tab", name); } catch (_) { /* storage blocked */ } history.replaceState(null, "", `#${name}`); }
    if (name === "emails") refreshEmails();
    if (name === "library") refreshLibrary();
  }
  for (const b of document.querySelectorAll(".cx-tab")) b.addEventListener("click", () => showTab(b.dataset.tab));
  // The Integrations tab shows the worst of its three links.
  function syncLinksTab() {
    const tones = ["stat-telegram", "stat-hermes", "stat-tunnel"].map((id) => $(id).dataset.tone || "");
    const tone = tones.includes("bad") ? "bad" : tones.includes("warn") ? "warn" : tones.every((x) => x === "ok") ? "ok" : "";
    const tab = $("tab-links"); tab.dataset.tone = tone;
    tab.querySelector("strong").textContent = `${tones.filter((x) => x === "ok").length}/3`;
  }
  new MutationObserver(syncLinksTab).observe($("links"), { subtree: true, attributes: true, attributeFilter: ["data-tone"] });
  const hashTab = location.hash.slice(1);
  const linkTab = { telegram: "links", hermes: "links", tunnels: "links", activity: "links" }[hashTab];
  let savedTab = ""; try { savedTab = localStorage.getItem("cx-tab") || ""; } catch (_) { /* storage blocked */ }
  showTab(TABS.includes(hashTab) ? hashTab : linkTab || savedTab || "websites", false);
  window.addEventListener("hashchange", () => {
    const h = location.hash.slice(1);
    if (TABS.includes(h)) showTab(h, false);
    else if (["telegram", "hermes", "tunnels", "activity"].includes(h)) { showTab("links", false); $(h).scrollIntoView(); }
  });

  /* ------------------------------------------------ scheduled posts -- */
  const POST_STATES = {
    scheduled: ["Scheduled", "info"], posting: ["Posting…", "warn"], done: ["Posted", "ok"], failed: ["Failed", "bad"],
    paused: ["Paused", ""], missed: ["Missed", "bad"], waiting: ["File missing", "bad"], skipped: ["Skipped (blank)", ""],
  };
  const BATCH_STATES = { active: ["Active", "info"], paused: ["Paused", ""], complete: ["Complete", "ok"], empty: ["Empty", ""] };
  let batchList = [], galleryBatch = null;
  const blankLabel = (x) => (x.blank === "agent" ? "Blank: the agent writes it" : "Blank: skipped unless filled in");
  const postLabel = (x) => x.title || x.body.slice(0, 90) || (x.media || x.awaiting_file ? "No title: the agent writes one" : blankLabel(x));
  const RUN_STATES = { done: "finished", failed: "failed", stopped: "was stopped", interrupted: "was interrupted" };
  let posts = [], editingPost = null, postTimer = 0;
  const postForm = $("post-form"), postDialog = $("post-dialog");
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const pad = (n) => String(n).padStart(2, "0");

  function postCard(x) {
    const card = el("article", "cx-post"); card.dataset.state = x.state;
    card.style.setProperty("--hue", hue(x.account_name));
    const at = x.next ? new Date(x.next * 1000) : null;
    const box = el("div", "cx-when");
    box.append(el("strong", null, at ? at.toLocaleTimeString([], { timeStyle: "short" }) : x.time),
      el("small", null, at ? at.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }) : x.repeat === "once" ? x.date : "—"));
    const main = el("div", "cx-post-main");
    const heading = el("h3", null, postLabel(x));
    if (x.batch_label) heading.append(el("span", "cx-batch-tag", x.batch_label));
    main.append(heading);
    const who = x.account_names.length > 1 ? `${x.account_names.join(", ")} (${x.account_mode === "random" ? "random" : "taking turns"})` : x.account_name;
    const to = el("p", "cx-post-to"); to.append(el("b", null, who), document.createTextNode(x.where ? ` → ${x.where}` : ""),
      document.createTextNode(` · ${x.repeat === "once" ? "once" : x.summary}` + (x.tz !== timeZone ? ` (${x.tz})` : "")));
    main.append(to);
    if (x.title && x.body) main.append(el("p", "cx-post-text", x.body));
    if (x.last_run) {
      const run = el("p", "cx-post-run");
      const state = x.last_run.error || (RUN_STATES[x.last_run.state] ? `the session ${RUN_STATES[x.last_run.state]}` : x.last_run.state ? "running" : "");
      run.append(document.createTextNode(`Last sent ${when(x.last_run.t)}` + (x.account_names.length > 1 && x.last_run.account_name ? ` from ${x.last_run.account_name}` : "")
        + (state ? `: ${state}` : "")));
      if (x.last_run.session) { const a = el("a", null, "Open session"); a.href = `/Agent/?s=${x.last_run.session}`; run.append(document.createTextNode(" · "), a); }
      main.append(run);
    }
    if (x.state === "waiting") main.append(el("p", "cx-post-run cx-warn", `${x.awaiting_file} didn't upload. Edit the post and attach it to schedule it.`));
    if (x.state === "missed") main.append(el("p", "cx-post-run cx-warn", `Missed ${when(x.missed)} (Connections wasn't running). Post now sends it.`));
    const side = el("div", "cx-post-side");
    const status = el("span", "cx-pill"); pill(status, ...POST_STATES[x.state]); side.append(status);
    if (x.media) {
      const src = `${base}/api/posts/${x.id}/media?f=${encodeURIComponent(x.media)}`;
      const thumb = /\.(mp4|webm|mov)$/i.test(x.media) ? el("video", "cx-post-thumb") : el("img", "cx-post-thumb");
      thumb.src = src; if (thumb.tagName === "VIDEO") { thumb.muted = true; thumb.preload = "metadata"; } else { thumb.alt = ""; thumb.loading = "lazy"; }
      side.append(thumb);
    }
    card.append(box, main, side);
    const actions = el("div", "cx-actions");
    const button = (text, cls, fn) => {
      const b = el("button", "cx-btn" + (cls ? ` ${cls}` : ""), text); b.type = "button";
      b.addEventListener("click", async () => { b.disabled = true; try { await fn(); } catch (e) { alert(e.message); } finally { b.disabled = false; } });
      actions.append(b);
    };
    if (x.state !== "posting" && x.state !== "waiting") button("Post now", "", async () => {
      if (!confirm(`Post "${x.title || x.body.slice(0, 60)}" to ${x.account_name} now?`)) return;
      const fresh = await act(`/api/posts/${x.id}/now`);
      if (fresh.last_run?.session) window.open(`/Agent/?s=${fresh.last_run.session}`, "_blank", "noopener");
      await refreshPosts();
    });
    if (x.enabled) button("Pause", "cx-btn-ghost", async () => { await act(`/api/posts/${x.id}/pause`); await refreshPosts(); });
    else if (x.next) button("Resume", "cx-btn-ghost", async () => { await act(`/api/posts/${x.id}/resume`); await refreshPosts(); });
    button(x.next ? "Edit" : "Reschedule", "cx-btn-ghost", async () => openPostDialog(x));
    button("Delete", "cx-btn-ghost cx-btn-danger", async () => {
      if (!confirm("Delete this scheduled post?")) return;
      await act(`/api/posts/${x.id}`, "DELETE"); await refreshPosts();
    });
    card.append(actions);
    return card;
  }

  async function refreshPosts() {
    clearTimeout(postTimer);
    const list = $("post-list");
    try {
      [{ posts }, { batches: batchList }] = await Promise.all([api("/api/posts"), api("/api/batches")]);
      const upcoming = posts.filter((x) => x.state === "scheduled");
      const filter = $("post-filter").value;
      const shown = posts.filter((x) => filter === "all" || (filter === "done" ? x.state === "done" : x.state !== "done"));
      list.replaceChildren(...(shown.length ? shown.map(postCard)
        : [el("p", "cx-empty", posts.length ? "Nothing to show here. Change Show to see the rest."
          : accounts.length ? "Nothing scheduled. Schedule a post or queue many to one of your websites." : "Add a website and an account first, then schedule posts to it.")]));
      renderBatches();
      if (galleryBatch && $("gallery-dialog").open) renderGallery();
      stat("stat-posts", upcoming.length ? `${upcoming.length} upcoming` : "None",
        posts.some((x) => x.state === "failed" || x.state === "missed") ? "bad" : posts.some((x) => x.state === "posting") ? "warn" : upcoming.length ? "ok" : "");
    } catch (e) { list.replaceChildren(el("p", "cx-detail", e.message)); stat("stat-posts", "Error", "bad"); }
    postTimer = setTimeout(refreshPosts, posts.some((x) => x.state === "posting") ? 8000 : 30000);
  }

  function batchButton(box, text, cls, fn) {
    const b = el("button", "cx-btn" + (cls ? ` ${cls}` : ""), text); b.type = "button";
    b.addEventListener("click", async () => { b.disabled = true; try { await fn(); await refreshPosts(); } catch (e) { alert(e.message); } finally { b.disabled = false; } });
    box.append(b);
  }
  function batchControls(box, b, inGallery) {
    if (!inGallery) batchButton(box, "Open", "", async () => openGallery(b.id));
    if (inGallery) {
      batchButton(box, "Add posts", "cx-btn-primary", async () => openBulkDialog("add", b));
      batchButton(box, "Settings", "cx-btn-ghost", async () => openBulkDialog("edit", b));
    }
    if (b.status === "active") batchButton(box, "Pause all", "cx-btn-ghost", () => act(`/api/posts/batch/${b.id}/pause`));
    else if (b.status === "paused") batchButton(box, "Resume all", "cx-btn-ghost", () => act(`/api/posts/batch/${b.id}/resume`));
    if (inGallery) batchButton(box, "Delete batch", "cx-btn-ghost cx-btn-danger", async () => {
      if (!confirm(`Delete ${b.label} and all ${b.posts} of its posts?`)) return;
      await act(`/api/posts/batch/${b.id}/delete`); $("gallery-dialog").close(); galleryBatch = null;
    });
  }
  function batchLine(b) {
    return `${b.posts} post${b.posts === 1 ? "" : "s"}` + (b.left ? `, ${b.left} to go` : "") + (b.blank ? ` (${b.blank} blank)` : "")
      + (b.next ? ` · next ${when(b.next)}` : b.status === "complete" && b.last_at ? ` · last ${when(b.last_at)}` : "");
  }
  function renderBatches() {
    $("batch-list").replaceChildren(...batchList.map((b) => {
      const box = el("div", "cx-batch"); box.dataset.status = b.status;
      const state = el("span", "cx-pill"); pill(state, ...BATCH_STATES[b.status]);
      const name = el("span"); name.append(el("b", null, b.label), document.createTextNode(` · ${batchLine(b)}`));
      box.append(state, name); batchControls(box, b, false);
      return box;
    }));
  }

  function galleryTile(x) {
    const tile = el("button", "cx-tile" + (!x.title && !x.body && !x.media ? " is-blank" : "")); tile.type = "button";
    const media = el("div", "cx-tile-media");
    if (x.media) {
      const src = `${base}/api/posts/${x.id}/media?f=${encodeURIComponent(x.media)}`;
      const node = /\.(mp4|webm|mov)$/i.test(x.media) ? el("video") : el("img");
      node.src = src; if (node.tagName === "VIDEO") { node.muted = true; node.preload = "metadata"; } else { node.alt = ""; node.loading = "lazy"; }
      media.append(node);
    } else media.append(el("div", "cx-tile-text", x.body || (x.awaiting_file ? `📎 ${x.awaiting_file} (not uploaded)` : x.title ? "Text post" : blankLabel(x))));
    const status = el("span", "cx-pill"); pill(status, ...POST_STATES[x.state]); media.append(status);
    const body = el("div", "cx-tile-body");
    const at = x.next ? when(x.next) : x.last_run ? `sent ${when(x.last_run.t)}` : `${x.date} ${x.time}`;
    body.append(el("strong", null, postLabel(x)), el("small", null, at),
      el("small", null, [x.account_names.join(", "), x.where].filter(Boolean).join(" → ")));
    tile.append(media, body);
    tile.addEventListener("click", () => openPostDialog(x));
    return tile;
  }
  function renderGallery() {
    const b = batchList.find((x) => x.id === galleryBatch);
    if (!b) { $("gallery-dialog").close(); galleryBatch = null; return; }
    $("gallery-title").textContent = b.label;
    $("gallery-sub").textContent = batchLine(b) + (b.account_names.length ? ` · ${b.account_names.join(", ")}` : "");
    pill($("gallery-state"), ...BATCH_STATES[b.status]);
    const actions = $("gallery-actions"); actions.replaceChildren(); batchControls(actions, b, true);
    const mine = posts.filter((x) => x.batch === b.id).sort((a, c) => (a.next || a.last_run?.t || 0) - (c.next || c.last_run?.t || 0));
    const add = el("button", "cx-tile cx-tile-add"); add.type = "button";
    add.append(el("strong", null, "+"), el("span", null, b.status === "complete" ? "Continue with more posts" : "Add posts"));
    add.addEventListener("click", () => openBulkDialog("add", b));
    $("gallery-grid").replaceChildren(...mine.map(galleryTile), add);
  }
  function openGallery(id) { galleryBatch = id; renderGallery(); if (!$("gallery-dialog").open) $("gallery-dialog").showModal(); }
  $("gallery-close").addEventListener("click", () => { $("gallery-dialog").close(); galleryBatch = null; });

  $("post-filter").addEventListener("change", refreshPosts);

  function fillAccounts(form, selected, random) {
    const list = form.querySelector(".cx-account-list"), toggle = form.querySelector("[data-random]");
    list.replaceChildren(...accounts.map((a) => {
      const label = el("label"), box = el("input"); box.type = "checkbox"; box.name = "accounts"; box.value = a.id; box.checked = selected.includes(a.id);
      const chip = el("span", null, a.name); chip.append(el("small", null, `${a.site_name} · ${a.username || a.email}`)); label.append(box, chip); return label;
    }));
    form.elements.account_random.checked = !!random;
    const sync = () => { toggle.hidden = list.querySelectorAll("input:checked").length < 2; };
    list.onchange = sync; sync();
  }
  function readAccounts(form) {
    const ids = [...form.querySelectorAll('.cx-account-list input:checked')].map((x) => x.value);
    if (!ids.length) throw new Error("Tick at least one website account");
    return { accounts: ids, account_mode: ids.length > 1 && form.elements.account_random.checked ? "random" : "rotate" };
  }
  // "🎲 Agent decides": put {? } at the cursor, the caret inside it, so the user can say what to decide.
  for (const button of document.querySelectorAll(".cx-decide")) {
    button.addEventListener("click", () => {
      const field = button.closest("form").elements[button.dataset.decide];
      if (button.dataset.decide === "file_title" && field.value === "{name}") field.value = "";
      const at = field.selectionStart ?? field.value.length, end = field.selectionEnd ?? at;
      const chosen = field.value.slice(at, end);
      const lead = field.tagName === "TEXTAREA" && at > 0 && field.value[at - 1] !== "\n" ? "\n" : "";
      const text = `${lead}{? ${chosen}}`;
      field.value = field.value.slice(0, at) + text + field.value.slice(end);
      field.focus(); const caret = at + text.length - 1; field.setSelectionRange(caret, caret);
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  function openPostDialog(post, accountId = "") {
    if (!accounts.length) { alert("Add an account first (Accounts tab)."); return; }
    editingPost = post;
    postForm.reset();
    $("post-error").hidden = true;
    $("post-dialog-title").textContent = post ? "Edit scheduled post" : "Schedule a post";
    $("post-save").textContent = post ? "Save" : "Schedule";
    fillAccounts(postForm, post ? post.accounts : [accountId || accounts[0].id], post?.account_mode === "random");
    const soon = new Date(Date.now() + 60 * 60 * 1000);
    const values = post || { date: `${soon.getFullYear()}-${pad(soon.getMonth() + 1)}-${pad(soon.getDate())}`,
      time: `${pad(soon.getHours())}:00`, repeat: "once" };
    for (const key of ["where", "title", "body", "tags", "notes", "date", "time", "repeat"]) if (values[key] != null) postForm.elements[key].value = values[key];
    if (post && !post.next) {        // rescheduling a finished one: suggest the same time, an hour from now at the earliest
      postForm.elements.date.value = `${soon.getFullYear()}-${pad(soon.getMonth() + 1)}-${pad(soon.getDate())}`;
    }
    const tz = post && post.tz !== timeZone ? post.tz : timeZone;
    $("post-tz").textContent = `Times are in ${tz}.` + (post && post.tz !== timeZone ? ` Saving changes it to your timezone (${timeZone}).` : "");
    $("post-file").hidden = !post?.media;
    if (post?.media) $("post-file").querySelector("span").textContent = `Attached: ${post.media}`;
    postDialog.showModal();
    postForm.elements[post ? "title" : "where"].focus();
  }
  $("post-add").addEventListener("click", () => openPostDialog(null));
  $("post-cancel").addEventListener("click", () => postDialog.close());
  postForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const save = $("post-save"); save.disabled = true;
    const f = postForm.elements;
    const body = Object.fromEntries(["where", "title", "body", "tags", "notes", "date", "time", "repeat"].map((k) => [k, f[k].value]));
    body.tz = timeZone; body.remove_media = f.remove_media.checked;
    try { Object.assign(body, readAccounts(postForm)); }
    catch (err) { $("post-error").textContent = err.message; $("post-error").hidden = false; save.disabled = false; return; }
    const data = new FormData();
    data.set("post", JSON.stringify(body));
    if (f.media.files[0]) data.set("media", f.media.files[0]);
    try {
      await api(editingPost ? `/api/posts/${editingPost.id}` : "/api/posts",
        { method: editingPost ? "PUT" : "POST", headers: { "X-Connections-Action": "1" }, body: data });
      postDialog.close();
      await refreshPosts();
    } catch (err) { $("post-error").textContent = err.message; $("post-error").hidden = false; }
    finally { save.disabled = false; }
  });

  /* ------------------------------------------------ queue many -- */
  const bulkDialog = $("bulk-dialog"), bulkForm = $("bulk-form");
  const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  let bulkSeed = 0, bulkPlanned = null, bulkMode = "new", bulkBatch = null;
  $("bulk-dialog").querySelector(".cx-days").append(...DAYS.map((d, i) => {
    const label = el("label"); const box = el("input"); box.type = "checkbox"; box.name = "days"; box.value = i; box.checked = true;
    label.append(box, el("span", null, d)); return label;
  }));
  const bulkFiles = () => [...bulkForm.elements.files.files];

  function bulkBody(preview) {
    const f = bulkForm.elements;
    const perDay = f.mode.value === "per_day";
    return {
      ...readAccounts(bulkForm), label: f.label.value, where: f.where.value, text: f.text.value, file_title: f.file_title.value,
      tags: f.tags.value, notes: f.notes.value, start_date: f.start_date.value, start_time: f.start_time.value,
      jitter_minutes: Number(f.jitter_minutes.value || 0), window_start: f.window_start.value || "00:00", window_end: f.window_end.value || "23:59",
      per_day: perDay ? Number(f.per_day.value || 1) : 0, interval_minutes: perDay ? 0 : Number(f.gap.value || 0) * Number(f.gap_unit.value),
      max_per_day: perDay ? 0 : Number(f.max_per_day.value || 0),
      days: [...bulkForm.querySelectorAll('input[name="days"]:checked')].map((x) => Number(x.value)),
      shuffle: f.shuffle.checked, tz: timeZone, seed: bulkSeed, files: bulkFiles().map((x) => x.name), preview,
      count: Number(f.count.value || 0), blank: f.blank.value, batch: bulkBatch?.id || "",
      continue: bulkMode !== "new" && f.continue.checked, retime: f.retime.checked, apply: f.apply.checked,
    };
  }
  function staleBulk() {
    bulkPlanned = null;
    $("bulk-save").disabled = bulkMode !== "edit"; $("bulk-save").textContent = bulkMode === "edit" ? "Save settings" : bulkMode === "add" ? "Add" : "Queue";
  }
  function bulkSections() {
    const f = bulkForm.elements, show = (name, on) => bulkForm.querySelectorAll(`[data-section="${name}"]`).forEach((n) => { n.hidden = !on; });
    show("content", bulkMode !== "edit");
    show("continue", bulkMode === "add" || (bulkMode === "edit" && f.retime.checked));
    show("apply", bulkMode === "edit");
    show("start", bulkMode === "new" || (!f.continue.checked && (bulkMode === "add" || f.retime.checked)));
    $("bulk-preview-btn").hidden = bulkMode === "edit";
  }
  bulkForm.addEventListener("input", (e) => { if (e.target.name !== "label") staleBulk(); });
  bulkForm.addEventListener("change", (e) => {
    if (e.target.name === "mode") for (const n of bulkForm.querySelectorAll("[data-mode]")) n.hidden = n.dataset.mode !== e.target.value;
    if (["continue", "retime"].includes(e.target.name)) bulkSections();
    if (e.target.name !== "label") staleBulk();
  });

  function openBulkDialog(mode = "new", batch = null) {
    if (!accounts.length) { alert("Add an account first (Accounts tab)."); return; }
    bulkMode = mode; bulkBatch = batch;
    bulkForm.reset();
    const f = bulkForm.elements, s = batch?.settings || {};
    $("bulk-dialog-title").textContent = mode === "edit" ? `${batch.label}: settings` : mode === "add" ? `Add posts to ${batch.label}` : "Queue many posts";
    const known = (s.accounts || []).filter((id) => accounts.some((a) => a.id === id));
    fillAccounts(bulkForm, known.length ? known : [accounts[0].id], s.account_mode === "random");
    if (batch) {
      f.label.value = batch.label;
      for (const key of ["where", "tags", "notes", "window_start", "window_end"]) if (s[key] != null) f[key].value = s[key];
      if (s.file_title != null) f.file_title.value = s.file_title;
      if (s.jitter_minutes != null) f.jitter_minutes.value = s.jitter_minutes;
      if (s.per_day) { f.mode.value = "per_day"; f.per_day.value = s.per_day; }
      else if (s.interval_minutes) {
        f.mode.value = "interval";
        const unit = s.interval_minutes % 1440 === 0 ? 1440 : s.interval_minutes % 60 === 0 ? 60 : 1;
        f.gap_unit.value = String(unit); f.gap.value = s.interval_minutes / unit; f.max_per_day.value = s.max_per_day || 0;
      }
      if (Array.isArray(s.days)) bulkForm.querySelectorAll('input[name="days"]').forEach((x) => { x.checked = s.days.includes(Number(x.value)); });
      f.shuffle.checked = !!s.shuffle; f.blank.value = s.blank || "skip";
    }
    for (const n of bulkForm.querySelectorAll("[data-mode]")) n.hidden = n.dataset.mode !== f.mode.value;
    const soon = new Date(Date.now() + 60 * 60 * 1000);
    f.start_date.value = `${soon.getFullYear()}-${pad(soon.getMonth() + 1)}-${pad(soon.getDate())}`;
    f.start_time.value = `${pad(soon.getHours())}:00`;
    $("bulk-tz").textContent = `Times are in ${timeZone}.` + (s.tz && s.tz !== timeZone ? ` (The batch was planned in ${s.tz}.)` : "");
    $("bulk-preview").hidden = true; $("bulk-error").hidden = true; $("bulk-reroll").hidden = true;
    bulkSeed = Math.floor(Math.random() * 2 ** 31) + 1;
    bulkSections(); staleBulk();
    bulkDialog.showModal();
  }

  async function previewBulk() {
    $("bulk-error").hidden = true;
    try {
      const { posts: planned } = await act("/api/posts/bulk", "POST", bulkBody(true));
      bulkPlanned = planned;
      const table = el("table"); const head = el("tr");
      const several = new Set(planned.map((x) => x.account)).size > 1;
      ["When", "Post", ...(several ? ["Account"] : []), "Where"].forEach((h) => head.append(el("th", null, h))); table.append(head);
      for (const x of planned) {
        const row = el("tr");
        row.append(el("td", null, new Date(x.at * 1000).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })));
        const what = el("td", null, x.title || x.body.slice(0, 80) || (x.file ? "No title: the agent writes one" : "Blank")); if (x.file) what.append(el("small", null, `📎 ${x.file}`));
        row.append(what, ...(several ? [el("td", null, x.account_name)] : []), el("td", null, x.where || "—")); table.append(row);
      }
      const last = planned[planned.length - 1];
      $("bulk-preview").replaceChildren(table); $("bulk-preview").hidden = false; $("bulk-reroll").hidden = false;
      $("bulk-save").disabled = false;
      $("bulk-save").textContent = `Queue ${planned.length} post${planned.length === 1 ? "" : "s"}` + (planned.length > 1 ? ` until ${new Date(last.at * 1000).toLocaleDateString([], { day: "numeric", month: "short" })}` : "");
    } catch (err) { staleBulk(); $("bulk-error").textContent = err.message; $("bulk-error").hidden = false; }
  }
  $("bulk-add").addEventListener("click", () => openBulkDialog("new"));
  $("bulk-cancel").addEventListener("click", () => bulkDialog.close());
  $("bulk-preview-btn").addEventListener("click", previewBulk);
  $("bulk-reroll").addEventListener("click", () => { bulkSeed = Math.floor(Math.random() * 2 ** 31) + 1; previewBulk(); });
  bulkForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const save = $("bulk-save");
    if (bulkMode === "edit") {
      save.disabled = true;
      try { await act(`/api/batches/${bulkBatch.id}`, "PUT", bulkBody(false)); bulkDialog.close(); await refreshPosts(); }
      catch (err) { $("bulk-error").textContent = err.message; $("bulk-error").hidden = false; }
      finally { save.disabled = false; }
      return;
    }
    if (!bulkPlanned) return;
    save.disabled = true;
    try {
      const made = await act("/api/posts/bulk", "POST", bulkBody(false));
      const files = new Map(bulkFiles().map((x) => [x.name, x]));
      const failed = [];
      const needing = made.posts.filter((x) => x.file);
      for (const [i, x] of needing.entries()) {
        save.textContent = `Uploading ${i + 1} of ${needing.length}…`;
        const data = new FormData(); data.set("media", files.get(x.file));
        try { await api(`/api/posts/${x.id}/media`, { method: "POST", headers: { "X-Connections-Action": "1" }, body: data }); }
        catch (err) { failed.push(`${x.file}: ${err.message}`); }
      }
      bulkDialog.close();
      await refreshPosts();
      openGallery(made.batch);
      if (failed.length) alert(`Queued ${made.posts.length} posts, but ${failed.length} file(s) didn't upload; those posts wait until you attach them:\n${failed.join("\n")}`);
    } catch (err) { $("bulk-error").textContent = err.message; $("bulk-error").hidden = false; staleBulk(); save.disabled = false; }
  });

  /* ------------------------------------------------ telegram -- */
  async function refreshTelegram() {
    try {
      const s = await api("/api/status");
      const toggle = $("toggle");
      pill($("state"), s.running ? "Running" : s.enabled ? "Starting" : "Paused", s.running ? "ok" : s.enabled ? "warn" : "");
      $("detail").textContent = (!s.configured ? "The bot isn't configured yet. " : s.outputs_available ? "Watching ComfyUI and Video outputs. "
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
  refreshAccounts().then(refreshPosts); refreshLibrary(); refreshEmails();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { refreshAccounts(); refreshPosts(); } });
})();
