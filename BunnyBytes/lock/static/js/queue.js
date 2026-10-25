(() => {
  const PREFIX = document.body.dataset.mountPrefix || "";
  const entriesEl = document.getElementById("entries");
  const emptyEl = document.getElementById("empty");
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
    for (const wrap of entriesEl.querySelectorAll("[data-progress-for]")) fillProgress(wrap, progressById[wrap.dataset.progressFor]);
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
      if (!quiet) showMessage(result.removed === "orphan"
        ? `${APP_LABELS[entry.app] || entry.app} no longer had this job, so it was removed from the queue.`
        : `Cancel request sent for ${APP_LABELS[entry.app] || entry.app}.`);
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
    const posEl = document.createElement("div");
    posEl.className = "queue-order";
    posEl.textContent = active ? "▶" : String(waiting[0]?.position ?? "?");
    row.appendChild(posEl);
    const body = document.createElement("div");
    body.className = "queue-card-body";
    const title = document.createElement("span");
    title.className = "queue-app";
    title.textContent = name;
    const count = document.createElement("span");
    count.className = "queue-label";
    count.textContent = `${members.length} job${members.length === 1 ? "" : "s"}` + (active ? " · 1 running" : "") + (waiting.length ? ` · ${waiting.length} waiting` : "");
    body.append(title, count);
    const meta = document.createElement("div");
    meta.className = "queue-meta";
    meta.textContent = active ? `Now: ${active.label}` : `Next: ${waiting[0]?.label || ""}`;
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
    entriesEl.appendChild(row);
  }

  function render(entries) {
    entriesEl.innerHTML = "";
    emptyEl.hidden = entries.length > 0;
    emptyEl.textContent = demoMode ? "All test jobs cancelled. Show the live queue or reload the test jobs." : "Nothing queued right now.";
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
      row.draggable = entry.state === "waiting" && !entry.is_locked && !entry.is_informational;
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
      body.appendChild(appSpan);
      body.appendChild(labelSpan);

      const meta = document.createElement("div");
      meta.className = "queue-meta";
      const metaParts = [entry.state === "active" ? "Running" : "Waiting for GPU"];
      if (entry.is_continuous) metaParts.push("Continuous");
      if (entry.is_informational) metaParts.push("Remote API · Llama loaded · Informational");
      if (entry.hermes) metaParts.push("Hermes" + (entry.hermes.runs > 1 ? ` · ${entry.hermes.runs} runs` : "") + (entry.hermes.loaded_model ? ` · ${entry.hermes.loaded_model} loaded` : ""));
      const countdown = fmtCountdown(entry.shutdown_at);
      if (countdown) metaParts.push(countdown);
      meta.textContent = metaParts.join(" · ");
      body.appendChild(meta);
      if (entry.state === "active" && !entry.is_informational) body.appendChild(progressBar(entry));

      row.appendChild(body);

      const stateEl = document.createElement("div");
      stateEl.className = "queue-status";
      stateEl.textContent = entry.state === "active" ? "active" : "waiting";
      row.appendChild(stateEl);

      const actions = document.createElement("div");
      actions.className = "queue-actions";
      if (entry.state === "waiting" && !entry.is_informational) {
        const lockBtn = document.createElement("button");
        lockBtn.className = "queue-lock";
        lockBtn.type = "button";
        lockBtn.textContent = entry.is_locked ? "Unlock position" : "Lock position";
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

      entriesEl.appendChild(row);
    }
  }

  function takeProgress(entries) {
    progressById = {};
    for (const entry of entries) if (entry.progress) progressById[entry.entry_id] = entry.progress;
  }

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

  function subscribe() {
    const source = new EventSource(`${PREFIX}/api/queue/events`);
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
  subscribe();
  setInterval(loadOnce, 30000);
  // A backgrounded tab (phone screen off, other machine) can miss events: catch up on return.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") loadOnce();
  });
})();
