// Rocsy Settings: Modules tab - show/hide each service's home card and
// drag rows (by the grip; arrow keys on the grip work too) to set their order
// on the home page, within a section or across Create / Tools. Saved in
// /api/settings as "hidden", "order" and "groups"; hidden modules keep working
// at their own URL.
(() => {
  const list = document.getElementById("module-list");
  const status = document.getElementById("modules-status");
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let hidden = [];
  let statusTimer = null;

  function setStatus(text, kind = "") {
    status.textContent = text;
    status.dataset.kind = kind;
    clearTimeout(statusTimer);
    if (kind === "ok") statusTimer = setTimeout(() => { status.textContent = ""; }, 2500);
  }

  async function put(body) {
    setStatus("Saving…");
    const response = await fetch("/api/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (response.status === 401) { location.href = "/login?next=/Settings%23modules"; throw new Error("login"); }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const saved = await response.json();
    setStatus("Saved", "ok");
    return saved;
  }

  const currentOrder = () => [...list.querySelectorAll(".bb-switch-row")].map((row) => row.dataset.module);
  const currentGroups = () => Object.fromEntries([...list.querySelectorAll(".bb-switch-row")]
    .map((row) => [row.dataset.module, row.closest("[data-group]").dataset.group]));
  const layoutKey = () => JSON.stringify([currentOrder(), currentGroups()]);

  async function saveOrder() {
    try { await put({ order: currentOrder(), groups: currentGroups() }); }
    catch (error) { if (error.message !== "login") setStatus(`Couldn't save the order (${error.message})`, "error"); }
  }

  // FLIP: rows that moved slide from their old spot instead of jumping.
  function animateFrom(rows, before) {
    if (reduceMotion) return;
    rows.forEach((row) => {
      const delta = before.get(row) - row.getBoundingClientRect().top;
      if (delta) row.animate([{ transform: `translateY(${delta}px)` }, { transform: "none" }], { duration: 160, easing: "ease-out" });
    });
  }

  // The section list before / after this row's own (Create <-> Tools).
  function otherList(row, direction) {
    const lists = [...list.querySelectorAll(".bb-switch-list")];
    return lists[lists.indexOf(row.parentElement) + direction] || null;
  }

  // Past the first / last row of a section the row moves into the next section.
  function crossSection(row, direction) {
    const target = otherList(row, direction);
    if (!target) return false;
    const rows = [...list.querySelectorAll(".bb-switch-row")];
    const before = new Map(rows.map((item) => [item, item.getBoundingClientRect().top]));
    if (direction < 0) target.append(row); else target.prepend(row);
    animateFrom(rows.filter((item) => item !== row), before);
    return true;
  }

  function move(row, direction) {
    const sibling = direction < 0 ? row.previousElementSibling : row.nextElementSibling;
    if (!sibling) return crossSection(row, direction);
    const rows = [...row.parentElement.children];
    const before = new Map(rows.map((item) => [item, item.getBoundingClientRect().top]));
    // Move the neighbour, not the dragged row: detaching the row would drop its pointer capture.
    if (direction < 0) row.after(sibling); else row.before(sibling);
    animateFrom(rows.filter((item) => item !== row), before);
    return true;
  }

  function startDrag(event, row) {
    if (event.button !== undefined && event.button !== 0) return;
    event.preventDefault();
    const handle = event.currentTarget;
    const startLayout = layoutKey();
    handle.setPointerCapture(event.pointerId);
    row.classList.add("is-dragging");
    list.classList.add("is-sorting");

    const onMove = (moveEvent) => {
      const y = moveEvent.clientY;
      const prev = row.previousElementSibling;
      const next = row.nextElementSibling;
      const own = row.getBoundingClientRect();
      if (prev) {
        const box = prev.getBoundingClientRect();
        if (y < box.top + box.height / 2) { move(row, -1); return; }
      } else if (y < own.top - 12 && otherList(row, -1)) {
        // Detaching the row drops its pointer capture; take it back.
        if (crossSection(row, -1)) try { handle.setPointerCapture(moveEvent.pointerId); } catch {}
        return;
      }
      if (next) {
        const box = next.getBoundingClientRect();
        if (y > box.top + box.height / 2) move(row, 1);
      } else if (y > own.bottom + 12 && otherList(row, 1)) {
        if (crossSection(row, 1)) try { handle.setPointerCapture(moveEvent.pointerId); } catch {}
      }
    };
    const onEnd = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onEnd);
      window.removeEventListener("pointercancel", onEnd);
      row.classList.remove("is-dragging");
      list.classList.remove("is-sorting");
      if (layoutKey() !== startLayout) saveOrder();
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onEnd);
    window.addEventListener("pointercancel", onEnd);
  }

  function row(module) {
    const label = document.createElement("label");
    label.className = "bb-switch-row bb-module-row";
    label.dataset.module = module.id;
    label.classList.toggle("is-hidden", hidden.includes(module.id));

    const grip = document.createElement("button");
    grip.type = "button";
    grip.className = "bb-grip";
    grip.title = "Drag to reorder or to move between Create and Tools (or use the arrow keys)";
    grip.setAttribute("aria-label", `Move ${module.label || module.id}. Use the up and down arrow keys; past the end it moves to the other section.`);
    grip.innerHTML = '<svg viewBox="0 0 12 18" width="12" height="18" aria-hidden="true"><g fill="currentColor">'
      + '<circle cx="3" cy="3" r="1.6"/><circle cx="9" cy="3" r="1.6"/><circle cx="3" cy="9" r="1.6"/>'
      + '<circle cx="9" cy="9" r="1.6"/><circle cx="3" cy="15" r="1.6"/><circle cx="9" cy="15" r="1.6"/></g></svg>';
    grip.addEventListener("pointerdown", (event) => startDrag(event, label));
    grip.addEventListener("click", (event) => event.preventDefault());  // never toggles the switch
    grip.addEventListener("keydown", (event) => {
      const direction = { ArrowUp: -1, ArrowDown: 1 }[event.key];
      if (!direction) return;
      event.preventDefault();
      if (move(label, direction)) { grip.focus(); saveOrder(); }
    });

    const text = document.createElement("span");
    text.className = "bb-module-text";
    const name = document.createElement("span");
    name.className = "bb-module-name";
    name.textContent = module.label || module.id;
    if (module.tag) {
      const tag = document.createElement("span");
      tag.className = "bb-module-tag";
      tag.textContent = module.tag;
      name.append(" ", tag);
    }
    const note = document.createElement("span");
    note.className = "bb-module-note";
    const link = document.createElement("a");
    const href = { Llama: "/Llama/ui/", Hermes: "/Hermes/", Voice: "/Voice", Vid: "/Video/", Krea: "/Image/", Mus: "/Music/" }[module.id] || `/${module.id}/`;
    link.href = href;
    link.textContent = href;
    note.append(`${module.note} · `, link);
    text.append(name, note);

    const input = document.createElement("input");
    input.type = "checkbox";
    input.className = "bb-switch";
    input.setAttribute("role", "switch");
    input.setAttribute("aria-label", `Show ${module.label || module.id} on the home page`);
    input.checked = !hidden.includes(module.id);
    input.addEventListener("change", async () => {
      const next = input.checked ? hidden.filter((id) => id !== module.id) : [...new Set([...hidden, module.id])];
      try {
        hidden = (await put({ hidden: next })).hidden;
      } catch (error) {
        input.checked = !input.checked;
        if (error.message !== "login") setStatus(`Couldn't save (${error.message})`, "error");
      }
      label.classList.toggle("is-hidden", hidden.includes(module.id));
    });

    label.append(grip, text, input);
    return label;
  }

  fetch("/api/settings")
    .then((response) => {
      if (response.status === 401) { location.href = "/login?next=/Settings%23modules"; throw new Error("login"); }
      return response.json();
    })
    .then((data) => {
      hidden = data.hidden || [];
      const order = data.order || [];
      const rank = (id) => { const index = order.indexOf(id); return index < 0 ? order.length : index; };
      const groups = { Create: [], Tools: [] };
      data.modules.forEach((module) => { (groups[(data.groups || {})[module.id] || module.group] ||= []).push(module); });
      list.replaceChildren(...Object.entries(groups).map(([group, modules]) => {
        const section = document.createElement("section");
        section.className = "bb-section";
        const head = document.createElement("div");
        head.className = "bb-section-head";
        const title = document.createElement("h2");
        title.textContent = group;
        const hint = document.createElement("span");
        hint.className = "bb-status";
        hint.textContent = "Drag ⠿ to reorder or move between sections";
        head.append(title, hint);
        const rows = document.createElement("div");
        rows.className = "bb-switch-list";
        rows.dataset.group = group;
        rows.dataset.empty = `Nothing in ${group} - drag a module here`;
        rows.append(...modules.sort((a, b) => rank(a.id) - rank(b.id)).map(row));
        section.append(head, rows);
        return section;
      }));
    })
    .catch((error) => { if (error.message !== "login") setStatus("Couldn't load modules", "error"); });
})();
