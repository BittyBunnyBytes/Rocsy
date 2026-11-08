// Rocsy Settings: Services tab - Plugins (gateway /api/plugins, plugins.py): every custom
// service from BunnyBytes/plugins, whether it answers, and manifests that were rejected.
(() => {
  const list = document.getElementById("plugin-list");
  const status = document.getElementById("plugins-status");
  const panel = document.getElementById("panel-services");
  if (!list || !panel) return;

  function text(className, value) {
    return Object.assign(document.createElement("span"), { className, textContent: value });
  }

  function row(plugin) {
    const el = document.createElement("div");
    el.className = "bb-svc";
    const dot = document.createElement("span");
    dot.className = "bb-device-dot";
    dot.dataset.state = plugin.up ? "on" : "blocked";
    const body = document.createElement("div");
    body.className = "bb-svc-text";
    const where = [plugin.path + "/", plugin.upstream, plugin.home ? `home: ${plugin.group}` : "URL only",
                   plugin.gpu_queue ? "GPU queue" : ""].filter(Boolean).join(" · ");
    body.append(text("bb-svc-name", plugin.name), text("bb-svc-note", plugin.note || `plugins/${plugin.folder}`),
                text("bb-svc-state", `${plugin.up ? "answering" : "not answering"} · ${where}`));
    const controls = document.createElement("div");
    controls.className = "bb-svc-controls";
    const open = Object.assign(document.createElement("a"), { className: "bb-button", href: `${plugin.path}/`, textContent: "Open" });
    controls.append(open);
    el.append(dot, body, controls);
    return el;
  }

  function broken(item) {
    const el = document.createElement("div");
    el.className = "bb-svc";
    const dot = document.createElement("span");
    dot.className = "bb-device-dot";
    dot.dataset.state = "blocked";
    const body = document.createElement("div");
    body.className = "bb-svc-text";
    body.append(text("bb-svc-name", `plugins/${item.folder}`), text("bb-svc-state", `Not loaded: ${item.error}`));
    el.append(dot, body);
    return el;
  }

  async function load() {
    try {
      const response = await fetch("/api/plugins", { cache: "no-store" });
      if (response.status === 401) { location.href = "/login?next=/Settings%23services"; return; }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const rows = [...data.plugins.map(row), ...data.errors.map(broken)];
      list.replaceChildren(...(rows.length ? rows
        : [Object.assign(document.createElement("p"), { className: "bb-section-note", textContent: "No plugins yet." })]));
      status.textContent = "";
    } catch (error) {
      status.textContent = `Couldn't load plugins: ${error.message}`;
      status.dataset.kind = "error";
    }
  }

  new MutationObserver(() => { if (!panel.hidden) load(); }).observe(panel, { attributes: true, attributeFilter: ["hidden"] });
  if (!panel.hidden) load();
})();
