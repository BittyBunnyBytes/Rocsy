// Rocsy Settings: Maintenance tab - automatic restarts for home servers (hostctl/maintenance.py via
// /api/maintenance): a network watchdog (restart after the Wi-Fi is out for N minutes, at most M times in a row)
// and a daily restart. Everything is off until the master switch is on. Polls every 10 s while the tab is open.
(() => {
  const panel = document.getElementById("panel-maintenance");
  if (!panel) return;
  const $ = (id) => document.getElementById(id);
  let data = null, timer = null, busy = false;

  function status(text, kind = "") { const s = $("mt-status"); s.textContent = text; s.dataset.kind = kind; }
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
    return new Date(t * 1000).toLocaleString();
  }

  async function call(url, method = "GET", body) {
    const options = { method, cache: "no-store", headers: {} };
    if (method !== "GET") {
      options.headers = { "Content-Type": "application/json", "X-BB-Action": "1" };
      options.body = JSON.stringify(body || {});
    }
    const r = await fetch(url, options);
    if (r.status === 401) { location.href = "/login?next=/Settings%23maintenance"; throw new Error("Log in again"); }
    let reply = {}; try { reply = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error(reply.error || `Request failed (${r.status})`);
    return reply;
  }

  async function save(change, label = "Saving…") {
    if (busy) return;
    busy = true;
    status(label);
    try { data = await call("/api/maintenance", "PUT", change); render(); status("Saved", "ok"); }
    catch (e) { status(e.message, "error"); if (data) render(); }
    finally { busy = false; }
  }

  function chip(k, v) {
    const c = el("div", "gt-chip");
    c.append(el("span", "gt-chip-k", k), el("span", "gt-chip-v", v));
    return c;
  }
  const yesNo = (v) => (v == null ? "–" : v ? "Reachable" : "Not reachable");

  function render() {
    const c = data.config, l = data.live || {};
    $("mt-enabled").disabled = false;
    $("mt-enabled").checked = c.enabled;
    $("mt-body").disabled = !c.enabled;
    $("mt-master-note").textContent = c.enabled
      ? "On: the parts switched on below run in the background, even with this page closed."
      : "Nothing below runs until this is on. Restarts reboot the whole computer, so Rocsy has to start by itself after a boot (it does once installed).";

    const perm = $("mt-permission");
    const cannot = data.can_restart && data.can_restart !== "yes";
    perm.hidden = !cannot && !data.dry_run;
    perm.textContent = data.dry_run ? "Test mode (MAINTENANCE_DRY_RUN=1): restarts are logged, not done."
      : `This user may not be allowed to restart the computer without a password (logind says “${data.can_restart}”). Allow it with a polkit rule for org.freedesktop.login1.reboot, or restarts will fail.`;

    const w = c.watchdog;
    $("mt-net-enabled").checked = w.enabled;
    $("mt-net-minutes").value = w.minutes;
    $("mt-net-max").value = w.max_restarts;
    $("mt-net-mode").value = w.mode;
    const chips = [];
    if (c.enabled && w.enabled && l.checked) {
      chips.push(chip("Network", l.online ? "Working" : l.outage_minutes ? `Out ${l.outage_minutes} min` : "Out"));
      if (l.router != null) chips.push(chip("Router", yesNo(l.router)));
      chips.push(chip("Internet", yesNo(l.internet)));
    } else if (l.checked) {
      chips.push(chip("Router", yesNo(l.router)), chip("Internet", yesNo(l.internet)));
    }
    chips.push(chip("Restarts in a row", `${data.net_restarts} / ${w.max_restarts}`));
    if (l.checked) chips.push(chip("Checked", ago(l.checked)));
    $("mt-live").replaceChildren(...chips);
    $("mt-net-note").textContent = l.note
      || "If the network stays out, the computer restarts, which often brings a stuck Wi-Fi adapter back. It stops after the last try and waits for the network; the count resets once it's back. Running GPU jobs get up to an hour to finish first.";

    const d = c.daily;
    $("mt-daily-enabled").checked = d.enabled;
    $("mt-daily-time").value = d.time;
    $("mt-daily-wait").checked = d.wait_idle;
    $("mt-daily-note").textContent = data.next_daily
      ? `Next restart: ${new Date(data.next_daily * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}. Uses this computer's clock.`
      : "Clears memory and stuck processes. Uses this computer's clock.";

    const items = (data.history || []).map((e) => {
      const li = el("li", `gt-log-${e.kind}`);
      li.append(el("time", null, ago(e.t)), document.createTextNode(` ${e.text}`));
      return li;
    });
    $("mt-log").replaceChildren(...(items.length ? items : [el("li", null, "Nothing yet.")]));
  }

  $("mt-enabled").addEventListener("change", async (e) => {
    if (e.target.checked && !confirm("Turn on maintenance?\n\nThis computer may then restart by itself when the network is out or at the daily time. Meant for home servers.")) {
      e.target.checked = false;
      return;
    }
    save({ enabled: e.target.checked });
  });
  $("mt-net-enabled").addEventListener("change", (e) => save({ watchdog: { enabled: e.target.checked } }));
  $("mt-net-minutes").addEventListener("change", (e) => save({ watchdog: { minutes: Number(e.target.value) } }));
  $("mt-net-max").addEventListener("change", (e) => save({ watchdog: { max_restarts: Number(e.target.value) } }));
  $("mt-net-mode").addEventListener("change", (e) => save({ watchdog: { mode: e.target.value } }));
  $("mt-daily-enabled").addEventListener("change", (e) => save({ daily: { enabled: e.target.checked } }));
  $("mt-daily-time").addEventListener("change", (e) => { if (e.target.value) save({ daily: { time: e.target.value.slice(0, 5) } }); });
  $("mt-daily-wait").addEventListener("change", (e) => save({ daily: { wait_idle: e.target.checked } }));
  $("mt-check").addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    status("Checking…");
    try { data = await call("/api/maintenance/check", "POST"); render(); status(data.live.online ? "Network working" : "Network out", data.live.online ? "ok" : "error"); }
    catch (e) { status(e.message, "error"); }
    finally { busy = false; }
  });

  async function poll() {
    clearTimeout(timer);
    if (panel.hidden || document.hidden) return;
    if (!busy) {
      try { data = await call("/api/maintenance"); render(); if ($("mt-status").dataset.kind === "error") status(""); }
      catch (e) { status(e.message, "error"); }
    }
    timer = setTimeout(poll, 10000);
  }
  new MutationObserver(() => { if (!panel.hidden) poll(); else clearTimeout(timer); })
    .observe(panel, { attributes: true, attributeFilter: ["hidden"] });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  if (!panel.hidden) poll();
})();
