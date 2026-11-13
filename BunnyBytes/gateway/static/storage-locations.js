// Rocsy Settings: Storage tab - storage locations for model files and service outputs (gateway /api/storage-locations ->
// bunny-hostctl storage.py). Add/remove locations, pick where new downloads go; files are moved in
// Models -> Manager -> Installed. A new location is mounted into the apps that read models once each is idle.
(() => {
  const panel = document.getElementById("panel-locations");
  const list = document.getElementById("loc-list");
  const status = document.getElementById("loc-status");
  const applyBox = document.getElementById("loc-apply");
  const addButton = document.getElementById("loc-add");
  const picker = document.getElementById("loc-picker");
  const drives = document.getElementById("loc-drives");
  const pathInput = document.getElementById("loc-path");
  const dirs = document.getElementById("loc-dirs");
  const here = document.getElementById("loc-here");
  const labelInput = document.getElementById("loc-label");
  const subfolder = document.getElementById("loc-subfolder");
  const confirmButton = document.getElementById("loc-confirm");
  const defaultsBox = document.getElementById("loc-defaults");
  const defaultsStatus = document.getElementById("loc-defaults-status");
  const other = document.getElementById("loc-other");
  const outBox = document.getElementById("loc-outputs");
  const outTo = document.getElementById("loc-outputs-to");
  const outMove = document.getElementById("loc-outputs-move");
  const outJob = document.getElementById("loc-outputs-job");
  const outStatus = document.getElementById("loc-outputs-status");
  const OUT_RUNNING = ["queued", "connecting", "copying", "verifying", "waiting", "switching"];
  const OUT_STEPS = { queued: "Starting", connecting: "Connecting the drive", copying: "Copying", verifying: "Checking copies",
                      waiting: "Copied · waiting for a quiet moment", switching: "Switching over", done: "Moved",
                      failed: "Move failed", cancelled: "Cancelled" };
  const HOME_RE = /^\/home\/[^/]+(?=\/|$)/;
  const CATEGORIES = [["models", "Models", "Video, image and audio models, encoders, VAEs, upscalers"],
                      ["loras", "LoRAs", "LoRAs for every studio"], ["llm", "LLMs", "GGUF language models for llama-swap"]];
  let data = null;
  let browsing = null;
  let timer = null;
  let lastKey = "";
  let statusTimer = null;
  let editing = "";        // id of the location whose Edit form is open (polling doesn't redraw it)
  const HOLDS = [["models", "Models", "Video, image and audio models, encoders, VAEs, upscalers"], ["loras", "LoRAs", "LoRAs for every studio"],
                 ["llm", "LLMs", "GGUF language models"], ["outputs", "Service outputs", "Renders, films, trained LoRAs, Voice clips"]];
  const holdName = (key) => HOLDS.find(([k]) => k === key)?.[1] || key;

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  };
  const tilde = (path) => String(path || "").replace(HOME_RE, "~");
  function size(bytes) {
    if (!bytes) return "0 GB";
    const gb = bytes / 1024 ** 3;
    if (gb >= 1000) return `${(gb / 1024).toFixed(1)} TB`;
    return gb >= 10 ? `${Math.round(gb)} GB` : gb >= 1 ? `${gb.toFixed(1)} GB` : `${Math.max(1, Math.round(bytes / 1024 ** 2))} MB`;
  }
  function setStatus(text, kind = "", node = status) {
    node.textContent = text;
    node.dataset.kind = kind;
    clearTimeout(statusTimer);
    if (kind === "ok") statusTimer = setTimeout(() => { node.textContent = ""; }, 4000);
  }

  async function call(method, path, body) {
    const response = await fetch(path, {
      method, cache: "no-store",
      headers: body ? { "Content-Type": "application/json", "X-BB-Action": "1" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    if (response.status === 401) { location.href = "/login?next=/Settings%23locations"; throw new Error("Signed out"); }
    const reply = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(reply.error || `HTTP ${response.status}`);
    return reply;
  }
  const action = (name, body) => call("POST", `/api/storage-locations/${name}`, body);

  function driveText(disk) {
    if (!disk?.online) return "Not reachable";
    const kind = disk.removable ? (disk.bus === "usb" ? "USB drive" : "Removable drive") : "";
    return [disk.label || disk.model, kind, disk.fstype].filter(Boolean).join(" · ");
  }

  function card(loc, builtin) {
    const box = el("div", "bb-loc-card" + (loc.online ? "" : " is-offline"));
    const head = el("div");
    const title = el("div", "bb-loc-title");
    title.append(el("strong", "", loc.label));
    const pill = el("span", "bb-loc-pill");
    if (!loc.online) { pill.textContent = "Offline"; pill.dataset.kind = "error"; pill.title = "The folder isn't there (drive unplugged?). Files stored here won't load until it's back."; }
    else if (!loc.ready) { pill.textContent = "Connecting"; pill.dataset.kind = "warn"; pill.title = "Waiting for the apps that read models to pick it up (they refresh once idle)"; }
    else { pill.textContent = builtin ? "Built in" : "Ready"; pill.dataset.kind = "ok"; }
    title.append(pill);
    head.append(title, el("div", "bb-loc-path", tilde(loc.path)));
    const actions = el("div", "bb-loc-actions");
    if (!builtin) {
      const rename = el("button", "bb-button", editing === loc.id ? "Close" : "Edit");
      rename.type = "button";
      rename.title = "Name, what it holds and which new downloads go here";
      rename.addEventListener("click", () => { editing = editing === loc.id ? "" : loc.id; lastKey = ""; render(); });
      const remove = el("button", "bb-button bb-button-danger", "Remove");
      remove.type = "button";
      const stored = loc.stored?.count || 0;
      remove.disabled = stored > 0;
      remove.title = stored ? `${stored} model file${stored === 1 ? "" : "s"} still live here: move them first in Models → Manager → Installed` : "Stop using this folder (its files stay on disk)";
      remove.addEventListener("click", async () => {
        if (!confirm(`Stop using “${loc.label}” for models?\n\nNothing is deleted. The apps that read models are refreshed once idle.`)) return;
        try { data = await action("remove", { id: loc.id }); lastKey = ""; render(); setStatus("Removed", "ok"); } catch (error) { setStatus(error.message, "error"); }
      });
      actions.append(rename, remove);
    }
    box.append(head, actions);
    const meter = el("div", "bb-loc-meter");
    const disk = loc.disk || {};
    if (disk.online && disk.total) {
      const bar = el("div", "bb-loc-bar");
      const used = el("span");
      used.style.width = `${Math.min(100, (disk.total - disk.free) / disk.total * 100)}%`;
      bar.append(used);
      if (!builtin && loc.stored?.bytes) {
        const models = el("span", "is-models");
        models.style.width = `${Math.min(100, loc.stored.bytes / disk.total * 100)}%`;
        models.title = `${size(loc.stored.bytes)} of moved models`;
        bar.append(models);
      }
      meter.append(bar);
    }
    const facts = el("div", "bb-loc-facts");
    if (disk.online) facts.append(el("span", "", `${size(disk.free)} free of ${size(disk.total)}`));
    const drive = driveText(disk);
    if (drive) facts.append(el("span", "", drive));
    if (!builtin) {
      const s = loc.stored || {};
      facts.append(el("span", "", s.count ? `${s.count} model file${s.count === 1 ? "" : "s"} · ${size(s.bytes)}${s.offline ? ` (${s.offline} unreachable)` : ""}` : "No models here yet"));
    }
    if (!builtin) {
      const handles = loc.handles || HOLDS.map(([k]) => k);
      const downloads = ["models", "loras", "llm"].filter((c) => data.defaults?.[c] === loc.id);
      facts.append(el("span", "", `Holds ${handles.map(holdName).join(", ")}`));
      if (downloads.length) facts.append(el("span", "", `New ${downloads.map(holdName).join(", ")} downloads go here`));
    }
    meter.append(facts);
    box.append(meter);
    if (!builtin && editing === loc.id) box.append(editForm(loc));
    return box;
  }

  function editForm(loc) {
    const form = el("div", "bb-loc-edit");
    const name = el("label", "bb-field", "Name");
    const input = el("input");
    input.type = "text";
    input.maxLength = 60;
    input.value = loc.label;
    name.append(input);
    const grid = el("div", "bb-loc-holds");
    grid.append(el("span", "bb-loc-holds-h", "Holds"), el("span", "bb-loc-holds-h", "New downloads go here"));
    const handles = new Set(loc.handles || HOLDS.map(([k]) => k));
    const rows = HOLDS.map(([key, label, note]) => {
      const hold = el("label", "bb-storage-upload-toggle");
      const holdBox = el("input");
      holdBox.type = "checkbox";
      holdBox.checked = handles.has(key);
      hold.title = note;
      hold.append(holdBox, document.createTextNode(` ${label}`));
      let dlBox = null;
      const dl = el("label", "bb-storage-upload-toggle");
      if (key === "outputs") {
        dl.append(el("span", "bb-section-note", "Moved with Service outputs below"));
      } else {
        dlBox = el("input");
        dlBox.type = "checkbox";
        dlBox.checked = data.defaults?.[key] === loc.id;
        const current = data.defaults?.[key] && data.defaults[key] !== loc.id ? data.locations.find((l) => l.id === data.defaults[key])?.label || "ComfyUI models folder" : "";
        dl.title = current ? `Now: ${current}` : "";
        dl.append(dlBox, document.createTextNode(` New ${label}`));
        const sync = () => { dlBox.disabled = !holdBox.checked; if (!holdBox.checked) dlBox.checked = false; };
        holdBox.addEventListener("change", sync);
        sync();
      }
      grid.append(hold, dl);
      return { key, holdBox, dlBox };
    });
    const note = el("p", "bb-section-note", "Turning a kind off is refused while files of that kind still live here. Sending downloads here takes them away from wherever they went before.");
    const buttons = el("div", "bb-rules-actions");
    const cancel = el("button", "bb-button", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", () => { editing = ""; lastKey = ""; render(); });
    const save = el("button", "bb-button bb-button-primary", "Save");
    save.type = "button";
    const formStatus = el("span", "bb-status");
    save.addEventListener("click", async () => {
      const body = { id: loc.id, label: input.value.trim(), handles: rows.filter((r) => r.holdBox.checked).map((r) => r.key),
                     downloads: rows.filter((r) => r.dlBox?.checked).map((r) => r.key) };
      if (!body.handles.length) { setStatus("Pick at least one thing it holds", "error", formStatus); return; }
      save.disabled = true;
      try { data = await action("edit", body); editing = ""; lastKey = ""; render(); setStatus("Saved", "ok"); }
      catch (error) { setStatus(error.message, "error", formStatus); }
      finally { save.disabled = false; }
    });
    buttons.append(formStatus, cancel, save);
    form.append(name, grid, note, buttons);
    return form;
  }

  function renderApply() {
    const pending = data.apply?.pending || [];
    applyBox.hidden = !pending.length && !data.apply?.error;
    if (applyBox.hidden) return;
    const intro = el("div", "", data.apply.running ? `Refreshing ${data.apply.running}…` :
      "Connecting storage to the apps that read models. Each one is refreshed (a few seconds) as soon as it's idle; nothing running is interrupted.");
    const items = el("ul");
    pending.forEach((p) => items.append(el("li", "", `${p.name}: ${p.waiting ? `waiting (${p.waiting})` : "next"}`)));
    applyBox.replaceChildren(intro, items);
    if (data.apply.error) {
      const error = el("div", "bb-status", data.apply.error);
      error.dataset.kind = "error";
      applyBox.append(error);
    }
    const llama = pending.find((p) => p.service === "llama" && /model loaded/.test(p.waiting || ""));
    if (llama) {
      const now = el("button", "bb-button", "Refresh Llama now");
      now.type = "button";
      now.title = "Unloads the loaded LLM; it loads again on the next request";
      now.addEventListener("click", async () => {
        if (!confirm("Refresh Llama now? The loaded model is unloaded and loads again on the next request.")) return;
        try { await action("apply", { force_llama: true }); setStatus("Refreshing Llama…"); setTimeout(load, 2500); } catch (error) { setStatus(error.message, "error"); }
      });
      applyBox.append(now);
    }
  }

  function renderDefaults() {
    const options = [data.builtin, ...data.locations];
    defaultsBox.replaceChildren(...CATEGORIES.map(([key, label, note]) => {
      const field = el("label", "bb-field");
      field.append(document.createTextNode(label));
      const select = el("select");
      select.dataset.category = key;
      options.forEach((loc) => {
        const holds = loc.id === "comfy" || (loc.handles || []).includes(key);
        const option = el("option", "", loc.id === "comfy" ? "ComfyUI models folder" : `${loc.label}${holds ? "" : ` (not set to hold ${label})`}${loc.online ? (loc.ready ? "" : " (connecting)") : " (offline)"}`);
        option.disabled = !holds;
        option.value = loc.id;
        select.append(option);
      });
      select.value = data.defaults?.[key] || "comfy";
      select.title = note;
      select.addEventListener("change", async () => {
        try {
          data = await action("defaults", { [key]: select.value });
          setStatus("Saved", "ok", defaultsStatus);
        } catch (error) { setStatus(error.message, "error", defaultsStatus); select.value = data.defaults?.[key] || "comfy"; }
      });
      field.append(select, el("small", "bb-section-note", note));
      return field;
    }));
  }

  function render() {
    renderOutputs();
    const key = JSON.stringify([data.locations, data.builtin, data.defaults, data.apply, editing]);
    if (key === lastKey || (editing && lastKey && list.querySelector(".bb-loc-edit"))) return;
    lastKey = key;
    list.replaceChildren(card(data.builtin, true), ...data.locations.map((loc) => card(loc, false)));
    renderApply();
    if (!defaultsBox.contains(document.activeElement)) renderDefaults();
    other.replaceChildren(...(data.other || []).map((folder) => {
      const row = el("div", "bb-storage-root");
      const name = el("strong", "", folder.label);
      const where = el("span", "", `${tilde(folder.path)} · ${folder.disk?.online ? `${size(folder.disk.free)} free` : "not reachable"}`);
      row.append(name, where);
      return row;
    }));
  }

  const outRunning = () => OUT_RUNNING.includes(data?.outputs?.job?.state);
  const locName = (id) => id === "comfy" ? "Rocsy's own folders" : (data.locations.find((l) => l.id === id)?.label || id);
  function renderOutputs() {
    const out = data.outputs;
    if (!out) return;
    outBox.replaceChildren(...out.folders.map((f) => {
      const row = el("div", "bb-loc-out" + (f.where !== "missing" && !f.online ? " is-offline" : ""));
      const where = f.where === "missing" ? "Not created yet" : f.where === "" ? `Linked by hand to ${tilde(f.real)}`
        : !f.online ? `${locName(f.where)} · not reachable (drive unplugged?)` : `${locName(f.where)}${f.bytes != null ? ` · ${size(f.bytes)}` : ""}`;
      row.append(el("strong", "", `${f.label}`), el("span", "", `${where} · ${tilde(f.where === "comfy" || f.where === "missing" ? f.path : f.real)}`));
      row.title = `${f.note}: ${f.path}`;
      return row;
    }));
    if (!outTo.matches(":focus")) {
      const options = [data.builtin, ...data.locations];
      const keep = outTo.value;
      outTo.replaceChildren(...options.map((loc) => {
        const option = el("option", "", loc.id === "comfy" ? "Rocsy's own folders (where they started)" : `${loc.label}${loc.online ? "" : " (offline)"}`);
        option.value = loc.id;
        option.disabled = loc.id !== "comfy" && (!loc.online || !(loc.handles || []).includes("outputs"));
        if (loc.id !== "comfy" && !(loc.handles || []).includes("outputs")) option.textContent += " (not set to hold outputs)";
        return option;
      }));
      outTo.value = keep && options.some((l) => l.id === keep) ? keep : (out.to || out.at || "comfy");
    }
    const running = outRunning();
    outMove.disabled = running;
    outTo.disabled = running;
    const job = out.job;
    outJob.hidden = !job;
    if (!job) return;
    outJob.dataset.kind = job.state === "failed" ? "error" : "";
    const head = el("strong", "", `${OUT_STEPS[job.state] || job.state} → ${job.label}`);
    const parts = [head];
    if (job.total && ["copying", "verifying", "waiting", "switching"].includes(job.state)) {
      const bar = el("div", "bb-loc-bar");
      const fill = el("span");
      fill.style.width = `${Math.min(100, job.bytes / job.total * 100)}%`;
      bar.append(fill);
      parts.push(bar);
    }
    const facts = el("div", "bb-loc-facts");
    if (job.waiting) facts.append(el("span", "", job.waiting));
    if (job.total) facts.append(el("span", "", `${size(Math.min(job.bytes, job.total))} of ${size(job.total)}`));
    if (job.folders?.length) facts.append(el("span", "", job.folders.join(", ")));
    parts.push(facts);
    if (job.error) parts.push(el("div", "bb-status", job.error));
    if (job.note) parts.push(el("div", "bb-section-note", job.note));
    if (running && job.state !== "switching") {
      const stop = el("button", "bb-button", "Cancel");
      stop.type = "button";
      stop.addEventListener("click", async () => {
        try { data = await action("outputs-cancel", {}); renderOutputs(); } catch (error) { setStatus(error.message, "error", outStatus); }
      });
      parts.push(stop);
    }
    outJob.replaceChildren(...parts);
  }
  outMove.addEventListener("click", async () => {
    const to = outTo.value;
    const out = data?.outputs;
    if (!out) return;
    const moving = out.folders.filter((f) => f.where !== to && f.where !== "missing");
    if (!moving.length) { setStatus("They're already there", "ok", outStatus); return; }
    const total = moving.reduce((sum, f) => sum + (f.bytes || 0), 0);
    if (!confirm(`Move service outputs (${size(total)}) to “${locName(to)}”?\n\n${moving.map((f) => `• ${f.label}`).join("\n")}\n\nEverything is copied and checked first; the old copies are deleted only after the switch. At the switch (once nothing is rendering) Video, Image, Music, Voice and Gateway restart for a few seconds.${to !== "comfy" ? "\n\nKeep the drive plugged in: while it's away those apps can't start or save." : ""}`)) return;
    outMove.disabled = true;
    try { data = await action("outputs", { to }); renderOutputs(); setStatus("Started", "ok", outStatus); timer = setTimeout(load, 2000); }
    catch (error) { setStatus(error.message, "error", outStatus); outMove.disabled = false; }
  });

  async function load() {
    clearTimeout(timer);
    if (panel.hidden) return;
    try {
      data = await call("GET", "/api/storage-locations");
      render();
    } catch (error) {
      if (!data) list.replaceChildren(el("p", "bb-section-note", `Couldn't read storage: ${error.message}`));
      else setStatus(error.message, "error");
    }
    timer = setTimeout(load, outRunning() ? 2000 : data?.apply?.pending?.length ? 5000 : 20000);
  }

  // ---- folder picker ----
  async function browse(path) {
    try {
      browsing = await call("GET", `/api/storage-locations/browse?path=${encodeURIComponent(path || "")}`);
    } catch (error) { here.textContent = error.message; return; }
    pathInput.value = browsing.path;
    drives.replaceChildren(...browsing.shortcuts.map((drive) => {
      const button = el("button", "bb-loc-drive" + (browsing.path === drive.path || browsing.path.startsWith(drive.path + "/") && drive.path !== "/" ? " is-current" : ""));
      button.type = "button";
      button.append(el("strong", "", drive.label), el("span", "", drive.disk?.online ? `${size(drive.disk.free)} free${drive.removable ? " · removable" : ""}` : tilde(drive.path)));
      button.title = drive.path;
      button.addEventListener("click", () => browse(drive.path));
      return button;
    }));
    dirs.replaceChildren(...(browsing.dirs.length ? browsing.dirs.map((dir) => {
      const button = el("button", "bb-loc-dir", dir.name);
      button.type = "button";
      button.setAttribute("role", "listitem");
      button.addEventListener("click", () => browse(dir.path));
      return button;
    }) : [el("p", "bb-section-note", "No folders inside.")]));
    const disk = browsing.disk || {};
    const target = subfolder.checked && !/\/Rocsy models$/.test(browsing.path) ? `${tilde(browsing.path)}/Rocsy models` : tilde(browsing.path);
    here.textContent = browsing.refused || `Models will be stored in ${target} · ${disk.online ? `${size(disk.free)} free` : ""}${driveText(disk) ? ` · ${driveText(disk)}` : ""}${disk.fstype === "vfat" ? " · FAT32 can't hold files over 4 GB" : ""}`;
    here.dataset.kind = browsing.refused ? "error" : "";
    confirmButton.disabled = Boolean(browsing.refused);
    if (!labelInput.dataset.touched) labelInput.value = disk.label || disk.model || "";
  }
  addButton.addEventListener("click", () => {
    picker.hidden = !picker.hidden;
    if (!picker.hidden) { labelInput.dataset.touched = ""; browse(""); }
  });
  document.getElementById("loc-cancel").addEventListener("click", () => { picker.hidden = true; });
  document.getElementById("loc-up").addEventListener("click", () => browsing?.parent && browse(browsing.parent));
  document.getElementById("loc-go").addEventListener("click", () => browse(pathInput.value.trim()));
  pathInput.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); browse(pathInput.value.trim()); } });
  labelInput.addEventListener("input", () => { labelInput.dataset.touched = "1"; });
  subfolder.addEventListener("change", () => browsing && browse(browsing.path));
  confirmButton.addEventListener("click", async () => {
    if (!browsing) return;
    confirmButton.disabled = true;
    setStatus("Adding…");
    try {
      const handles = [...document.querySelectorAll("#loc-holds input:checked")].map((box) => box.value);
      if (!handles.length) { setStatus("Pick at least one thing it holds", "error"); return; }
      const downloads = document.getElementById("loc-add-downloads").checked ? handles.filter((h) => h !== "outputs") : [];
      data = await action("add", { path: browsing.path, label: labelInput.value.trim(), subfolder: subfolder.checked, handles, downloads });
      picker.hidden = true;
      lastKey = "";
      render();
      setStatus("Added · the apps pick it up as soon as each is idle", "ok");
      timer = setTimeout(load, 3000);
    } catch (error) { setStatus(error.message, "error"); }
    finally { confirmButton.disabled = false; }
  });

  // Load when the tab is shown (notifications.js switches panels via the hidden attribute).
  new MutationObserver(() => { if (!panel.hidden) load(); else clearTimeout(timer); })
    .observe(panel, { attributes: true, attributeFilter: ["hidden"] });
  document.addEventListener("visibilitychange", () => { if (document.hidden) clearTimeout(timer); else load(); });
  load();
})();
