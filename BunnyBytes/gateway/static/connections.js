// Rocsy Settings: Security -> Connections. Everyone who has reached Rocsy,
// shown by anonymous nickname + country flag; revoke / restore per person.
(() => {
  const list = document.getElementById("connections-list");
  if (!list) return;
  const stats = document.getElementById("connections-stats");
  const status = document.getElementById("connections-status");
  const panel = document.getElementById("panel-security");
  const SHOWN = 25;
  let showAll = false;
  let filter = "all";
  let lastData = null;
  const FILTERS = {
    all: () => true,
    pw: (p) => p.has_password,
    bot: (p) => p.bot,
    revoked: (p) => p.blocked,
  };
  let timer = null;
  let statusTimer = null;
  let regionNames = null;
  try { regionNames = new Intl.DisplayNames(undefined, { type: "region" }); } catch { /* old browser */ }

  const setStatus = (text, kind = "") => {
    status.textContent = text;
    status.dataset.kind = kind;
    clearTimeout(statusTimer);
    if (kind === "ok") statusTimer = setTimeout(() => { status.textContent = ""; }, 5000);
  };
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const flagEmoji = (code) => String.fromCodePoint(...[...code].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65));
  const countryName = (code) => {
    try { return regionNames?.of(code) || code; } catch { return code; }
  };
  const ago = (seconds) => {
    const s = Math.max(0, Date.now() / 1000 - seconds);
    if (s < 90) return "just now";
    if (s < 5400) return `${Math.round(s / 60)} min ago`;
    if (s < 172800) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} days ago`;
  };

  function flag(person) {
    const node = el("span", "bb-conn-flag");
    if (person.country) {
      node.textContent = flagEmoji(person.country);
      node.title = countryName(person.country);
    } else {
      node.textContent = person.local ? "LAN" : "??";
      node.dataset.unknown = "";
      node.title = person.local ? "Local network" : "Unknown country";
    }
    node.setAttribute("role", "img");
    node.setAttribute("aria-label", node.title);
    return node;
  }

  function row(person) {
    const item = el("div", "bb-conn");
    item.append(flag(person));
    const text = el("div", "bb-conn-text");
    const name = el("span", "bb-conn-name", person.nickname);
    if (person.you) {
      const you = el("span", "bb-conn-tag", "You");
      you.dataset.kind = "you";
      name.append(you);
    }
    const tag = person.auto_blocked ? ["Auto-blocked", "revoked"] : person.blocked ? ["Revoked", "revoked"]
      : person.has_password ? ["Has password", "pw"] : ["No password", ""];
    const tagNode = el("span", "bb-conn-tag", tag[0]);
    tagNode.dataset.kind = tag[1];
    name.append(tagNode);
    if (person.bot) {
      const bot = el("span", "bb-conn-tag", "Bot");
      bot.dataset.kind = "bot";
      bot.title = person.probes.length ? "Probed for files Rocsy never serves" : "Not a web browser";
      name.append(bot);
    }
    text.append(name);
    const where = person.country ? countryName(person.country) : person.local ? "Local network" : "Unknown country";
    const devices = person.devices.filter((d) => d.active).map((d) => d.device + (d.current ? " (this browser)" : ""));
    const meta = [where, `last seen ${ago(person.last)}`, `${person.hits.toLocaleString()} request${person.hits === 1 ? "" : "s"}`];
    text.append(el("span", "bb-conn-meta", meta.join(" · ")));
    if (person.probes.length) text.append(el("span", "bb-conn-meta", `Probed ${person.probes.join(", ")}`));
    if (devices.length) text.append(el("span", "bb-conn-meta", `Signed in on ${devices.join(", ")}`));
    item.append(text);
    if (person.you) {
      const self = el("button", "bb-button", "Revoke");
      self.type = "button";
      self.disabled = true;
      self.title = "This is your own connection";
      item.append(self);
    } else {
      const button = el("button", person.blocked ? "bb-button" : "bb-button bb-button-danger", person.blocked ? "Un-revoke" : "Revoke");
      button.type = "button";
      button.addEventListener("click", () => act(person, person.blocked ? "restore" : "revoke", button));
      item.append(button);
    }
    return item;
  }

  function render(data) {
    lastData = data;
    stats.replaceChildren();
    // Each chip is a filter: click to show only those, click again (or the total) for everyone.
    const stat = (value, label, kind = "") => {
      const key = kind || "all";
      const node = el("button", "bb-conn-stat");
      node.type = "button";
      node.dataset.kind = kind;
      node.setAttribute("aria-pressed", String(filter === key));
      node.title = key === "all" ? "Show everyone" : "Show only these (click again for everyone)";
      node.append(el("strong", "", value.toLocaleString()), ` ${label}`);
      node.addEventListener("click", () => { filter = filter === key ? "all" : key; showAll = false; render(lastData); });
      stats.append(node);
    };
    if (!FILTERS[filter] || (filter === "bot" && !data.bots) || (filter === "revoked" && !data.revoked)) filter = "all";
    stat(data.total, data.total === 1 ? "person connected" : "people connected");
    stat(data.with_password, data.with_password === 1 ? "has the password" : "have the password", "pw");
    if (data.bots) stat(data.bots, data.bots === 1 ? "bot" : "bots", "bot");
    if (data.revoked) stat(data.revoked, data.auto_blocked ? `revoked (${data.auto_blocked} automatically)` : "revoked", "revoked");
    if (!data.password_set) setStatus("No password is set, so everyone who connects is let in.", "error");
    list.replaceChildren();
    const matching = data.people.filter(FILTERS[filter]);
    if (!matching.length) {
      list.append(el("p", "bb-section-note", data.people.length ? "No connections match this filter." : "No connections yet."));
      return;
    }
    const people = showAll ? matching : matching.slice(0, SHOWN);
    people.forEach((person) => list.append(row(person)));
    if (matching.length > SHOWN) {
      const more = el("button", "bb-button bb-conn-more", showAll ? "Show fewer" : `Show all ${matching.length}`);
      more.type = "button";
      more.addEventListener("click", () => { showAll = !showAll; render(data); });
      list.append(more);
    }
  }

  async function load() {
    try {
      const response = await fetch("/api/connections", { cache: "no-store", credentials: "same-origin" });
      if (response.status === 401) { location.href = "/login?next=/Settings%23security"; return; }
      if (!response.ok) throw new Error(`Could not load connections (HTTP ${response.status}).`);
      render(await response.json());
    } catch (error) { setStatus(error.message, "error"); }
  }

  async function act(person, action, button) {
    if (action === "revoke" && !confirm(`Revoke ${person.nickname}?\n\nThey are signed out on every device and their connection is blocked.`)) return;
    button.disabled = true;
    if (action === "restore" && !confirm(`Un-revoke ${person.nickname}?\n\nTheir connection is allowed again and won't be auto-blocked as a scanner.`)) return;
    setStatus(`${action === "revoke" ? "Revoking" : "Un-revoking"} ${person.nickname}…`);
    try {
      const response = await fetch(`/api/connections/${encodeURIComponent(person.id)}/${action}`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-BB-Action": "1" }, body: "{}",
      });
      if (response.status === 401) { location.href = "/login?next=/Settings%23security"; return; }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      setStatus(data.message || "Done.", "ok");
    } catch (error) {
      setStatus(error.message, "error");
    } finally {
      load();
    }
  }

  const clearButton = document.getElementById("connections-clear");
  clearButton.addEventListener("click", async () => {
    if (!confirm("Clear the connections list?\n\nRemoves everyone who isn't revoked and doesn't have the password. Revoked connections and signed-in people stay.")) return;
    clearButton.disabled = true;
    try {
      const response = await fetch("/api/connections/clear", {
        method: "POST", headers: { "Content-Type": "application/json", "X-BB-Action": "1" }, body: "{}",
      });
      if (response.status === 401) { location.href = "/login?next=/Settings%23security"; return; }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      setStatus(data.message || "Cleared.", "ok");
    } catch (error) {
      setStatus(error.message, "error");
    } finally {
      clearButton.disabled = false;
      load();
    }
  });

  // Settings -> Security: auto-block scanners (site setting, on by default).
  const scannerBlock = document.getElementById("scanner-block");
  fetch("/api/settings", { cache: "no-store", credentials: "same-origin" })
    .then((response) => (response.ok ? response.json() : null))
    .then((saved) => { if (saved) { scannerBlock.checked = saved.scanner_block !== false; scannerBlock.disabled = false; } })
    .catch(() => {});
  scannerBlock.addEventListener("change", async () => {
    const on = scannerBlock.checked;
    scannerBlock.disabled = true;
    try {
      const response = await fetch("/api/settings", {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scanner_block: on }),
      });
      if (response.status === 401) { location.href = "/login?next=/Settings%23security"; return; }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      scannerBlock.checked = (await response.json()).scanner_block !== false;
      setStatus(on ? "Scanners are blocked automatically." : "Scanner blocking is off.", "ok");
    } catch (error) {
      scannerBlock.checked = !on;
      setStatus(`Couldn't save (${error.message})`, "error");
    } finally {
      scannerBlock.disabled = false;
    }
  });

  // Refresh while the Security tab is open and the page is visible.
  function tick() {
    clearTimeout(timer);
    if (!panel.hidden && !document.hidden) load();
    timer = setTimeout(tick, 20000);
  }
  new MutationObserver(() => { if (!panel.hidden) load(); }).observe(panel, { attributes: true, attributeFilter: ["hidden"] });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !panel.hidden) load(); });
  tick();
})();
