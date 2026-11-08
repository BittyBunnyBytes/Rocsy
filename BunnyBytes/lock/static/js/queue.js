(() => {
  const PREFIX = document.body.dataset.mountPrefix || "";
  const entriesEl = document.getElementById("entries");
  const emptyEl = document.getElementById("empty");
  const activeEl = document.getElementById("active-entries");
  const activeEmptyEl = document.getElementById("active-empty");
  const statsEl = document.getElementById("stats");
  const runningCountEl = document.getElementById("running-count");
  const waitingCountEl = document.getElementById("waiting-count");
  const gpuPanel = document.getElementById("gpu-panel");
  const gpuMode = document.getElementById("gpu-mode");
  const gpuNumbers = document.getElementById("gpu-numbers");
  const gpuBar = document.getElementById("gpu-bar");
  const gpuLegend = document.getElementById("gpu-legend");
  const setConcurrent = document.getElementById("set-concurrent");
  const setPrepare = document.getElementById("set-prepare");
  const setReserve = document.getElementById("set-reserve");
  const setTotal = document.getElementById("set-total");
  const setTotalHelp = document.getElementById("set-total-help");
  const peaksBody = document.getElementById("peaks-body");
  const peaksEmpty = document.getElementById("peaks-empty");
  let vram = null;
  let settings = null;
  const messageEl = document.getElementById("queue-message");
  const demoToggle = document.getElementById("demo-toggle");
  const restartComfyui = document.getElementById("restart-comfyui");
  const modeIndicator = document.getElementById("mode-indicator");

  const APP_LABELS = { Vid: "Video", Krea: "Image", Mus: "Music", Trainer: "Trainer", Hermes: "Hermes", Agent: "Master Agent" };
  const TEST_JOBS = [
    ["Krea", "Product concept images"],
    ["Mus", "Soundtrack draft"],
    ["Vid", "H3 character animation"],
    ["Trainer", "Portrait LoRA training"],
    ["Vid", "Short video render"],
    ["Krea", "Poster variations"],
    ["Mus", "Ambient music mix"],
    ["Vid", "Continuous LTX batch"],
    ["Trainer", "Style LoRA training"],
    ["Krea", "inkw4sh_fox-000003 · seed 410716218: test prompt", "IMAGE - EPOCH TESTING"],
    ["Krea", "inkw4sh_fox-000004 · seed 410716218: test prompt", "IMAGE - EPOCH TESTING"],
    ["Krea", "inkw4sh_fox-000005 · seed 410716218: test prompt", "IMAGE - EPOCH TESTING"],
  ];
  let demoMode = new URLSearchParams(location.search).has("demo");
  let demoEntries = [];
  let liveEntries = [];
  // entry_id -> {percent, remaining_seconds, stage}: the running job's render progress, from "progress" events.
  let progressById = {};
  const cancelling = new Set();
  const locking = new Set();
  const openGroups = new Set();
  let draggingId = null;
  let reorderPending = false;

  function showMessage(message, isError = false) {
    messageEl.textContent = message;
    messageEl.classList.toggle("is-error", isError);
    messageEl.hidden = !message;
  }

  function makeDemoEntries() {
    return TEST_JOBS.map(([app, label, group], index) => ({
      entry_id: `test-${index + 1}`,
      app,
      label,
      group: group || null,
      state: index === 0 ? "active" : "waiting",
      is_continuous: index === 0 || index === 8,
      is_locked: false,
      shutdown_at: null,
      position: index === 0 ? null : index,
      progress: index === 0 ? { percent: 42, remaining_seconds: 155, stage: "render" } : null,
      vram_mb: index % 4 === 3 ? null : 2048 + ((index * 1531) % 9000),
    }));
  }

  function displayEntries() {
    return demoMode ? demoEntries.map((entry, index) => ({
      ...entry,
      position: entry.state === "active" ? null : index + (demoEntries[0]?.state === "active" ? 0 : 1),
    })) : liveEntries;
  }

  function refresh() {
    demoToggle.textContent = demoMode ? "Show live queue" : "Load test jobs";
    modeIndicator.innerHTML = demoMode ? "Test jobs only" : "<i></i> Live updates";
    render(displayEntries());
  }

  async function moveWaiting(sourceId, targetId) {
    const entries = displayEntries();
    const waiting = entries.filter((entry) => entry.state === "waiting");
    const movable = waiting.filter((entry) => !entry.is_locked);
    const from = movable.findIndex((entry) => entry.entry_id === sourceId);
    const to = movable.findIndex((entry) => entry.entry_id === targetId);
    if (from < 0 || to < 0 || from === to) return;
    const [moved] = movable.splice(from, 1);
    movable.splice(to, 0, moved);
    let cursor = 0;
    const ordered = waiting.map((entry) => entry.is_locked ? entry : movable[cursor++]);
    const active = entries.filter((entry) => entry.state === "active");
    const next = [...active, ...ordered];
    if (demoMode) {
      demoEntries = next;
      refresh();
      showMessage("Test queue order updated.");
      return;
    }
    const previous = liveEntries;
    reorderPending = true;
    liveEntries = next;
    refresh();
    try {
      const response = await fetch(`${PREFIX}/api/queue/reorder`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ entry_ids: ordered.map((entry) => entry.entry_id) }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.message || `Reorder failed (HTTP ${response.status}).`);
      liveEntries = result.entries;
      showMessage("Queue order saved.");
    } catch (err) {
      liveEntries = previous;
      showMessage(err.message || "Unable to reorder the queue.", true);
      await loadOnce();
    } finally {
      reorderPending = false;
      refresh();
    }
  }

  async function toggleLock(entry) {
    if (demoMode) {
      demoEntries = demoEntries.map((job) => job.entry_id === entry.entry_id ? { ...job, is_locked: !job.is_locked } : job);
      refresh();
      return;
    }
    if (locking.has(entry.entry_id)) return;
    locking.add(entry.entry_id);
    refresh();
    try {
      const response = await fetch(`${PREFIX}/api/queue/${encodeURIComponent(entry.entry_id)}/lock`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_locked: !entry.is_locked }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.message || `Lock failed (HTTP ${response.status}).`);
      liveEntries = liveEntries.map((job) => job.entry_id === entry.entry_id ? { ...job, is_locked: result.is_locked } : job);
      showMessage(result.is_locked ? "Card position locked." : "Card position unlocked.");
    } catch (err) {
      showMessage(err.message || "Unable to change this lock.", true);
    } finally {
      locking.delete(entry.entry_id);
      refresh();
    }
  }

  function fmtCountdown(shutdownAt) {
    if (!shutdownAt) return null;
    const remainingMs = new Date(shutdownAt).getTime() - Date.now();
    if (remainingMs <= 0) return "stopping...";
    const totalMinutes = Math.round(remainingMs / 60000);
    if (totalMinutes < 60) return `${totalMinutes}m remaining`;
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    return `${hours}h ${minutes}m remaining`;
  }

  function fmtLeft(seconds) {
    if (seconds == null) return "";
    if (seconds < 60) return "<1m left";
    const minutes = Math.round(seconds / 60);
    return minutes < 60 ? `~${minutes}m left` : `~${Math.floor(minutes / 60)}h ${minutes % 60}m left`;
  }

  function progressFor(entry) {
    return demoMode ? entry.progress : progressById[entry.entry_id];
  }

  // The bar is updated in place by "progress" events, so a drag or an open group isn't redrawn every tick.
  function progressBar(entry) {
    const wrap = document.createElement("div");
    wrap.className = "queue-progress";
    wrap.dataset.progressFor = entry.entry_id;
    wrap.innerHTML = '<div class="queue-progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100"><i></i></div><span class="queue-progress-text"></span>';
    fillProgress(wrap, progressFor(entry));
    return wrap;
  }

  function fillProgress(wrap, progress) {
    wrap.hidden = !progress;
    if (!progress) return;
    const percent = Math.max(0, Math.min(100, Number(progress.percent) || 0));
    wrap.querySelector("i").style.width = `${percent}%`;
    wrap.querySelector(".queue-progress-track").setAttribute("aria-valuenow", String(Math.round(percent)));
    const parts = [`${Math.floor(percent)}%`];
    if (progress.stage) parts.push(progress.stage);
    const left = percent < 100 ? fmtLeft(progress.remaining_seconds) : "finishing…";
    if (left) parts.push(left);
    wrap.querySelector(".queue-progress-text").textContent = parts.join(" · ");
  }

  function updateProgressBars() {
    for (const wrap of document.querySelectorAll("[data-progress-for]")) fillProgress(wrap, progressById[wrap.dataset.progressFor]);
  }

  async function cancelEntry(entry, quiet = false) {
    if (demoMode) {
      demoEntries = demoEntries.filter((job) => job.entry_id !== entry.entry_id);
      if (!quiet) showMessage(`${APP_LABELS[entry.app] || entry.app} test job cancelled.`);
      refresh();
      return;
    }
    if (cancelling.has(entry.entry_id)) return;
    cancelling.add(entry.entry_id);
    refresh();
    if (!quiet) showMessage("");
    try {
      const response = await fetch(`${PREFIX}/api/queue/${encodeURIComponent(entry.entry_id)}/cancel`, { method: "POST" });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.message || result.detail || `Cancel failed (HTTP ${response.status}).`);
      const appName = APP_LABELS[entry.app] || entry.app;
      if (!quiet) showMessage(result.removed === "orphan"
        ? `${appName} no longer had this job, so it was removed from the queue.`
        : result.removed === "app_down"
          ? `${appName} isn't running, so its job was removed from the queue.`
          : `Cancel request sent for ${appName}.`);
      if (!quiet) await loadOnce();
    } catch (err) {
      console.error("Cancel failed", err);
      showMessage(err.message || "Unable to cancel this job. Please try again.", true);
    } finally {
      cancelling.delete(entry.entry_id);
      refresh();
    }
  }

  // Entries sharing a `group` (e.g. the Trainer's epoch-test renders) are one card, placed where the group's
  // first entry sits; it opens into the list of its jobs, each still cancellable.
  function renderGroup(name, members) {
    const active = members.find((entry) => entry.state === "active");
    const waiting = members.filter((entry) => entry.state === "waiting");
    const open = openGroups.has(name);
    const row = document.createElement("div");
    row.className = "queue-card queue-group" + (active ? " is-active" : " is-waiting") + (open ? " is-open" : "");
    row.dataset.app = (active || members[0]).app;
    const posEl = document.createElement("div");
    posEl.className = "queue-order";
    posEl.textContent = active ? "▶" : String(waiting[0]?.position ?? "?");
    row.appendChild(posEl);
    const body = document.createElement("div");
    body.className = "queue-card-body";
    const head = document.createElement("div");
    head.className = "queue-card-head";
    const title = document.createElement("span");
    title.className = "queue-app";
    title.textContent = name;
    head.append(title, badge(`${members.length} job${members.length === 1 ? "" : "s"}`));
    const count = document.createElement("span");
    count.className = "queue-label";
    count.textContent = active ? active.label : (waiting[0]?.label || "");
    body.append(head, count);
    const meta = document.createElement("div");
    meta.className = "queue-meta";
    meta.textContent = (active ? "1 running" : "") + (active && waiting.length ? " · " : "") + (waiting.length ? `${waiting.length} waiting` : "");
    body.appendChild(meta);
    if (active) body.appendChild(progressBar(active));
    if (open) {
      const list = document.createElement("ol");
      list.className = "group-list";
      for (const entry of members) {
        const item = document.createElement("li");
        item.className = entry.state === "active" ? "is-active" : "";
        const text = document.createElement("span");
        text.textContent = entry.label;
        text.title = entry.label;
        const tag = document.createElement("em");
        tag.textContent = entry.state === "active" ? "running" : `#${entry.position ?? "?"}`;
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "queue-cancel";
        cancel.textContent = cancelling.has(entry.entry_id) ? "Cancelling…" : "Cancel";
        cancel.disabled = cancelling.has(entry.entry_id);
        cancel.setAttribute("aria-label", `Cancel ${entry.label}`);
        cancel.addEventListener("click", () => cancelEntry(entry));
        item.append(tag, text, cancel);
        list.appendChild(item);
      }
      body.appendChild(list);
    }
    row.appendChild(body);
    const actions = document.createElement("div");
    actions.className = "queue-actions";
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "queue-lock";
    toggle.textContent = open ? "Hide jobs" : `Show ${members.length} job${members.length === 1 ? "" : "s"}`;
    toggle.addEventListener("click", () => { open ? openGroups.delete(name) : openGroups.add(name); refresh(); });
    const cancelAll = document.createElement("button");
    cancelAll.type = "button";
    cancelAll.className = "queue-cancel";
    cancelAll.textContent = "Cancel all";
    cancelAll.disabled = members.every((entry) => cancelling.has(entry.entry_id));
    cancelAll.addEventListener("click", async () => {
      if (!confirm(`Cancel all ${members.length} ${name} job${members.length === 1 ? "" : "s"}?`)) return;
      // Waiting ones first, so the queue doesn't hand the GPU to the next member while the running one stops.
      for (const entry of [...waiting, ...(active ? [active] : [])]) await cancelEntry(entry, true);
      showMessage(`Cancel requests sent for ${members.length} ${name} job${members.length === 1 ? "" : "s"}.`);
      await loadOnce();
    });
    actions.append(toggle, cancelAll);
    row.appendChild(actions);
    (active ? activeEl : entriesEl).appendChild(row);
  }

  const gb = (mb) => `${(mb / 1024).toFixed(mb >= 10240 ? 0 : 1)} GB`;

  // The job's planned VRAM (recorded peak / override); shown once the queue measures VRAM.
  function vramTag(entry) {
    if (entry.is_informational || !(demoMode || vram?.available)) return null;
    const known = entry.vram_mb != null;
    const el = badge(known ? `≈ ${gb(entry.vram_mb)}` : "VRAM unmeasured", "vram");
    if (!known) el.classList.add("is-unknown");
    el.title = known
      ? `Planned VRAM for ${entry.vram_key || entry.app} (its recorded peak)`
      : `${entry.vram_key || entry.app} has not been measured yet: it runs alone the first time`;
    return el;
  }

  function badge(text, kind = "") {
    const el = document.createElement("span");
    el.className = "badge" + (kind ? ` badge-${kind}` : "");
    el.textContent = text;
    return el;
  }

  function renderStats(entries) {
    const running = entries.filter((entry) => entry.state === "active").length;
    const waiting = entries.length - running;
    const locked = entries.filter((entry) => entry.state === "waiting" && entry.is_locked).length;
    const stats = [["Running", running, "is-running"], ["Waiting", waiting, ""], ["Locked", locked, ""]];
    statsEl.innerHTML = "";
    for (const [label, value, cls] of stats) {
      const item = document.createElement("div");
      item.className = "stat" + (cls ? ` ${cls}` : "");
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = String(value);
      item.append(dt, dd);
      statsEl.appendChild(item);
    }
    runningCountEl.textContent = running ? String(running) : "";
    waitingCountEl.textContent = waiting ? String(waiting) : "";
  }

  function render(entries) {
    entriesEl.innerHTML = "";
    activeEl.innerHTML = "";
    renderStats(entries);
    const hasActive = entries.some((entry) => entry.state === "active");
    const hasWaiting = entries.some((entry) => entry.state !== "active");
    activeEmptyEl.hidden = hasActive;
    emptyEl.hidden = hasWaiting;
    emptyEl.textContent = demoMode && !entries.length ? "All test jobs cancelled. Show the live queue or reload the test jobs." : "Nothing waiting.";
    const drawn = new Set();
    for (const entry of entries) {
      if (entry.group) {
        if (!drawn.has(entry.group)) {
          drawn.add(entry.group);
          renderGroup(entry.group, entries.filter((other) => other.group === entry.group));
        }
        continue;
      }
      const row = document.createElement("div");
      row.className = "queue-card" + (entry.state === "active" ? " is-active" : " is-waiting") + (entry.is_continuous ? " is-continuous" : "") + (entry.is_locked ? " is-locked" : "") + (entry.is_informational ? " is-informational" : "");
      row.dataset.app = entry.app;
      row.draggable = entry.state === "waiting" && !entry.is_locked && !entry.is_informational;
      if (row.draggable) row.title = "Drag to reorder";
      if (row.draggable) {
        row.addEventListener("dragstart", (event) => {
          if (event.target?.tagName === "BUTTON" || event.target?.tagName === "INPUT") {
            event.preventDefault();
            return;
          }
          draggingId = entry.entry_id;
          event.dataTransfer.effectAllowed = "move";
          event.dataTransfer.setData("text/plain", entry.entry_id);
          row.classList.add("is-dragging");
        });
        row.addEventListener("dragover", (event) => {
          if (!draggingId || draggingId === entry.entry_id) return;
          event.preventDefault();
          event.dataTransfer.dropEffect = "move";
          row.classList.add("is-drop-target");
        });
        row.addEventListener("dragleave", () => row.classList.remove("is-drop-target"));
        row.addEventListener("drop", async (event) => {
          event.preventDefault();
          const sourceId = draggingId || event.dataTransfer.getData("text/plain");
          draggingId = null;
          row.classList.remove("is-drop-target");
          await moveWaiting(sourceId, entry.entry_id);
        });
        row.addEventListener("dragend", () => {
          draggingId = null;
          row.classList.remove("is-dragging");
          row.classList.remove("is-drop-target");
          if (!reorderPending) refresh();
        });
      }

      const posEl = document.createElement("div");
      if (entry.is_continuous) {
        posEl.className = "queue-order infinity";
        posEl.textContent = "∞";
      } else {
        posEl.className = "queue-order";
        posEl.textContent = entry.state === "active" ? "▶" : String(entry.position ?? "?");
      }
      row.appendChild(posEl);

      const body = document.createElement("div");
      body.className = "queue-card-body";
      const appSpan = document.createElement("span");
      appSpan.className = "queue-app";
      appSpan.textContent = (APP_LABELS[entry.app] || entry.app) + (entry.hermes ? " + Hermes" : "");
      const labelSpan = document.createElement("span");
      labelSpan.className = "queue-label";
      labelSpan.textContent = entry.label;
      const head = document.createElement("div");
      head.className = "queue-card-head";
      head.appendChild(appSpan);
      if (entry.is_continuous) head.appendChild(badge("Continuous", "continuous"));
      if (entry.is_locked) head.appendChild(badge("🔒 Locked", "locked"));
      const countdownBadge = fmtCountdown(entry.shutdown_at);
      if (countdownBadge) head.appendChild(badge(countdownBadge, "time"));
      if (entry.is_informational) head.appendChild(badge("Informational", "info"));
      const vramBadge = vramTag(entry);
      if (vramBadge) head.appendChild(vramBadge);
      body.appendChild(head);
      body.appendChild(labelSpan);

      const meta = document.createElement("div");
      meta.className = "queue-meta";
      const metaParts = [];
      if (entry.is_informational) metaParts.push("Remote API · Llama loaded");
      if (entry.hermes) metaParts.push("Hermes" + (entry.hermes.runs > 1 ? ` · ${entry.hermes.runs} runs` : "") + (entry.hermes.loaded_model ? ` · ${entry.hermes.loaded_model} loaded` : ""));
      meta.textContent = metaParts.join(" · ");
      body.appendChild(meta);
      if (entry.state === "active" && !entry.is_informational) body.appendChild(progressBar(entry));

      row.appendChild(body);

      const actions = document.createElement("div");
      actions.className = "queue-actions";
      if (entry.state === "waiting" && !entry.is_informational) {
        const lockBtn = document.createElement("button");
        lockBtn.className = "queue-lock";
        lockBtn.type = "button";
        lockBtn.textContent = entry.is_locked ? "Unlock" : "Lock";
        lockBtn.setAttribute("aria-label", `${entry.is_locked ? "Unlock" : "Lock"} position of ${APP_LABELS[entry.app] || entry.app} job`);
        lockBtn.disabled = locking.has(entry.entry_id);
        lockBtn.addEventListener("click", () => toggleLock(entry));
        actions.appendChild(lockBtn);
      }
      if (!entry.is_informational) {
        const cancelBtn = document.createElement("button");
        cancelBtn.className = "queue-cancel";
        cancelBtn.textContent = "Cancel";
        cancelBtn.type = "button";
        cancelBtn.setAttribute("aria-label", `Cancel ${APP_LABELS[entry.app] || entry.app} job`);
        cancelBtn.disabled = cancelling.has(entry.entry_id);
        if (cancelBtn.disabled) cancelBtn.textContent = "Cancelling…";
        cancelBtn.addEventListener("click", () => cancelEntry(entry));
        actions.appendChild(cancelBtn);
      }
      if (actions.childElementCount) row.appendChild(actions);

      (entry.state === "active" ? activeEl : entriesEl).appendChild(row);
    }
  }

  function takeProgress(entries) {
    progressById = {};
    for (const entry of entries) if (entry.progress) progressById[entry.entry_id] = entry.progress;
  }

  // ---------------------------------------------------------------- GPU memory

  function renderGpu() {
    gpuPanel.hidden = !vram || !vram.available;
    if (gpuPanel.hidden) return;
    const total = vram.total_mb || 1;
    const pct = (mb) => `${Math.max(0, Math.min(100, (mb / total) * 100))}%`;
    const floor = vram.floor_mb || 0;
    const planned = vram.running.reduce((sum, job) => sum + (job.estimate_mb || 0), 0);
    const beyond = vram.used_mb - floor - planned;
    const extra = beyond >= 256 ? beyond : 0;   // smaller is reading noise
    gpuMode.textContent = vram.active ? "Jobs share the GPU" : "One job at a time";
    gpuMode.className = "badge" + (vram.active ? " badge-on" : "");
    gpuNumbers.textContent = `${gb(vram.used_mb)} used of ${gb(total)}` + (vram.active ? ` · ${gb(vram.free_for_jobs_mb)} free for another job` : "");
    gpuBar.innerHTML = "";
    const segment = (cls, mb, title, app) => {
      if (mb <= 0) return;
      const el = document.createElement("span");
      el.className = cls;
      el.style.width = pct(mb);
      el.title = `${title}: ${gb(mb)}`;
      if (app) el.dataset.app = app;
      gpuBar.appendChild(el);
    };
    segment("seg-floor", floor, "Desktop (lowest reading)");
    for (const job of vram.running) segment("seg-job", job.estimate_mb || 0, `${job.label} (planned)`, job.app);
    segment("seg-extra", extra, "Beyond the plan (cached models, other programs)");
    segment("seg-reserve", vram.reserve_mb, "Always kept free");
    const mark = document.createElement("span");
    mark.className = "used-mark";
    mark.style.left = pct(vram.used_mb);
    mark.title = `Used now: ${gb(vram.used_mb)}`;
    gpuBar.appendChild(mark);
    gpuBar.setAttribute("aria-label", gpuNumbers.textContent);
    gpuLegend.innerHTML = "";
    const legend = (cls, text, app) => {
      const item = document.createElement("span");
      const swatch = document.createElement("i");
      if (cls) swatch.className = cls;
      if (app) swatch.dataset.app = app;
      item.append(swatch, text);
      gpuLegend.appendChild(item);
    };
    legend("k-floor", `Desktop ${gb(floor)}`);
    for (const job of vram.running) legend("", `${job.key} ${job.estimate_mb ? gb(job.estimate_mb) : "(unmeasured)"}`, job.app);
    if (extra > 0) legend("k-extra", `Beyond plan ${gb(extra)}`);
    legend("k-reserve", `Reserve ${gb(vram.reserve_mb)}`);
    legend("k-used", "Used now");
  }

  function renderSettings() {
    if (settings) {
      setConcurrent.checked = !!settings.concurrent;
      setPrepare.checked = settings.prepare_early !== false;
      if (document.activeElement !== setReserve) setReserve.value = +(settings.vram_reserve_mb / 1024).toFixed(2);
      if (document.activeElement !== setTotal) setTotal.value = +((settings.vram_total_mb || 0) / 1024).toFixed(2);
    }
    if (!vram) return;
    setConcurrent.disabled = !vram.available;
    setTotalHelp.textContent = vram.available
      ? `Leave at 0 to use the size the card reports (${gb(vram.detected_total_mb)}).`
      : "The GPU's memory counters can't be read, so jobs always run one at a time.";
    renderPeaks();
  }

  function renderPeaks() {
    if (peaksBody.contains(document.activeElement)) return;   // don't redraw under the cursor
    peaksBody.innerHTML = "";
    peaksEmpty.hidden = vram.profiles.length > 0;
    for (const profile of vram.profiles) {
      const row = document.createElement("tr");
      row.classList.toggle("is-overridden", profile.override_mb != null);
      const name = document.createElement("td");
      name.textContent = profile.key;
      const peak = document.createElement("td");
      peak.className = "peak";
      peak.textContent = profile.peak_mb != null ? gb(profile.peak_mb) : "–";
      const recent = document.createElement("td");
      recent.className = "recent";
      recent.textContent = profile.recent.length ? profile.recent.map(gb).join(", ") : "–";
      recent.title = `${profile.runs} run${profile.runs === 1 ? "" : "s"} measured`;
      const overrideCell = document.createElement("td");
      const input = document.createElement("input");
      input.type = "number";
      input.min = "0";
      input.step = "0.5";
      input.placeholder = "GB";
      input.setAttribute("aria-label", `VRAM to plan for ${profile.key}, GB`);
      if (profile.override_mb != null) input.value = +(profile.override_mb / 1024).toFixed(2);
      input.addEventListener("change", () => {
        const value = parseFloat(input.value);
        saveSettings({ vram_overrides: { [profile.key]: value > 0 ? Math.round(value * 1024) : null } });
      });
      overrideCell.appendChild(input);
      const actions = document.createElement("td");
      if (profile.peak_mb != null) {
        const forget = document.createElement("button");
        forget.type = "button";
        forget.className = "queue-cancel";
        forget.textContent = "Forget";
        forget.addEventListener("click", () => forgetPeak(profile.key));
        actions.appendChild(forget);
      }
      row.append(name, peak, recent, overrideCell, actions);
      peaksBody.appendChild(row);
    }
  }

  async function loadSettings() {
    try {
      const [s, v] = await Promise.all([
        fetch(`${PREFIX}/api/settings`).then((r) => r.ok ? r.json() : null),
        fetch(`${PREFIX}/api/vram`).then((r) => r.ok ? r.json() : null),
      ]);
      if (s) settings = s;
      if (v) vram = v;
      renderSettings();
      renderGpu();
      refresh();
    } catch (err) {
      console.error("Failed to load queue settings", err);
    }
  }

  async function saveSettings(patch) {
    try {
      const response = await fetch(`${PREFIX}/api/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.message || `Saving failed (HTTP ${response.status}).`);
      settings = result;
      showMessage("Queue settings saved.");
      await loadSettings();
    } catch (err) {
      showMessage(err.message || "Unable to save the queue settings.", true);
      renderSettings();
    }
  }

  async function forgetPeak(key) {
    if (!confirm(`Forget the recorded VRAM peak of ${key}? It will run alone until it is measured again.`)) return;
    try {
      const response = await fetch(`${PREFIX}/api/vram/forget`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.message || `Forget failed (HTTP ${response.status}).`);
      vram = result;
      renderSettings();
      renderGpu();
    } catch (err) {
      showMessage(err.message || "Unable to forget that peak.", true);
    }
  }

  setConcurrent.addEventListener("change", () => saveSettings({ concurrent: setConcurrent.checked }));
  setPrepare.addEventListener("change", () => saveSettings({ prepare_early: setPrepare.checked }));
  setReserve.addEventListener("change", () => {
    const value = parseFloat(setReserve.value);
    if (value >= 0) saveSettings({ vram_reserve_mb: Math.round(value * 1024) });
  });
  setTotal.addEventListener("change", () => {
    const value = parseFloat(setTotal.value);
    if (value >= 0) saveSettings({ vram_total_mb: Math.round(value * 1024) });
  });

  async function loadOnce() {
    try {
      const response = await fetch(`${PREFIX}/api/queue`);
      if (!response.ok) throw new Error(`Queue request failed (HTTP ${response.status}).`);
      const data = await response.json();
      liveEntries = data.entries || [];
      takeProgress(liveEntries);
      if (!demoMode && !draggingId && !reorderPending) refresh();
    } catch (err) {
      console.error("Failed to load queue", err);
    }
  }

  let source = null;
  function subscribe() {
    source = new EventSource(`${PREFIX}/api/queue/events`);
    source.addEventListener("queue", (event) => {
      try {
        const data = JSON.parse(event.data);
        liveEntries = data.entries || [];
        takeProgress(liveEntries);
        if (!demoMode && !draggingId && !reorderPending) refresh();
      } catch (err) {
        console.error("Bad queue event", err);
      }
    });
    source.addEventListener("vram", (event) => {
      try {
        vram = JSON.parse(event.data);
        renderGpu();
        renderSettings();
      } catch (err) {
        console.error("Bad vram event", err);
      }
    });
    source.addEventListener("progress", (event) => {
      try {
        progressById = JSON.parse(event.data).progress || {};
        if (!demoMode) updateProgressBars();
      } catch (err) {
        console.error("Bad progress event", err);
      }
    });
    source.onerror = () => {
      // EventSource auto-reconnects; nothing to do here.
    };
  }

  demoToggle.addEventListener("click", () => {
    demoMode = !demoMode;
    if (demoMode) demoEntries = makeDemoEntries();
    const url = new URL(location.href);
    if (demoMode) url.searchParams.set("demo", "1");
    else url.searchParams.delete("demo");
    history.replaceState(null, "", url);
    showMessage(demoMode ? "Test mode: these jobs are examples and do not use the GPU." : "");
    refresh();
  });

  restartComfyui.addEventListener("click", async () => {
    if (!confirm("Restart ComfyUI and its supervisor? Any running generation will be interrupted.")) return;
    restartComfyui.disabled = true;
    restartComfyui.textContent = "Restarting…";
    showMessage("Restarting ComfyUI and its supervisor…");
    try {
      const response = await fetch(`${PREFIX}/api/comfyui/restart`, { method: "POST" });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.message || `Restart failed (HTTP ${response.status}).`);
      showMessage(result.message || "ComfyUI restarted; supervisor restart requested.");
    } catch (err) {
      showMessage(err.message || "Unable to restart ComfyUI.", true);
    } finally {
      restartComfyui.disabled = false;
      restartComfyui.textContent = "Restart ComfyUI + supervisor";
    }
  });

  if (demoMode) {
    demoEntries = makeDemoEntries();
    showMessage("Test mode: these jobs are examples and do not use the GPU.");
  }
  refresh();
  loadOnce();
  loadSettings();
  subscribe();
  // Only a fallback: the live stream already carries every change.
  setInterval(() => { if (!source || source.readyState !== EventSource.OPEN) loadOnce(); }, 30000);
  // A backgrounded tab (phone screen off, other machine) can miss events: catch up on return.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") { loadOnce(); loadSettings(); }
  });
})();
