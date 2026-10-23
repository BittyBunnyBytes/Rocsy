// Rocsy Settings: Compression tab - how hard the Master Agent's Hermes sessions shrink their
// history (Agent service /Agent/api/config "compression", saved on every change). bunny-hostctl
// copies it into Hermes' config.yaml and the rocsy plugin's rocsy-compression.json within 30 s.
// A preset fills every number; editing a number switches to Custom.
(() => {
  const panel = document.getElementById("panel-compression");
  if (!panel) return;
  const $ = (id) => document.getElementById(id);
  const inputs = [...panel.querySelectorAll("input[data-key]")];
  const NOTES = {
    light: "Compresses late and keeps long summaries, many recent messages and big tool outputs. Most detail, but long sessions re-read more and compress less often.",
    balanced: "The default: compresses at half the window and trims old tool outputs over 4,000 characters.",
    strong: "Compresses earlier with shorter summaries and a small recent tail. Good for long REA or coding sessions with big terminal output.",
    maximum: "Smallest context: compresses early, keeps only a handful of recent messages and trims nearly every old tool output. The agent may need to re-read files it already saw.",
    custom: "Your own numbers below.",
  };
  let cfg = null, loaded = false;

  async function call(url, options = {}) {
    const r = await fetch(url, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...options });
    if (r.status === 401) { location.href = "/login?next=/Settings%23compression"; throw new Error("Log in again"); }
    let body = {}; try { body = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error(body.detail || body.message || body.error || `Request failed (${r.status})`);
    return body;
  }
  function status(text, kind = "") { const s = $("compress-status"); s.textContent = text; s.dataset.kind = kind; }

  function render() {
    const c = cfg.compression || {};
    $("compress-preset").value = c.preset || "balanced";
    $("compress-preset-note").textContent = NOTES[$("compress-preset").value] || "";
    for (const input of inputs) {
      const [lo, hi] = (cfg.compression_fields || {})[input.dataset.key] || [];
      if (lo != null) { input.min = lo; input.max = hi; }
      input.value = c[input.dataset.key];
    }
  }

  async function save(compression) {
    status("Saving…");
    try {
      cfg = await call("/Agent/api/config", { method: "PUT", body: JSON.stringify({ compression }) });
      status("Saved · applies within 30 s", "ok");
    } catch (e) { status(e.message, "error"); }
    render();
  }

  $("compress-preset").addEventListener("change", (e) => {
    // Custom starts from the numbers on screen; a preset brings its own.
    save(e.target.value === "custom" ? { ...cfg.compression, preset: "custom" } : { preset: e.target.value });
  });
  for (const input of inputs) {
    input.addEventListener("change", () => {
      const values = { ...cfg.compression, preset: "custom" };
      for (const x of inputs) values[x.dataset.key] = Math.round(Number(x.value));
      save(values);   // the server clamps; render() shows what was saved
    });
  }

  async function load() {
    if (loaded) return;
    loaded = true;
    try { cfg = await call("/Agent/api/config"); render(); }
    catch (e) { loaded = false; status(`The Agent service isn't reachable: ${e.message}`, "error"); }
  }
  new MutationObserver(() => { if (!panel.hidden) load(); }).observe(panel, { attributes: true, attributeFilter: ["hidden"] });
  if (!panel.hidden) load();
})();
