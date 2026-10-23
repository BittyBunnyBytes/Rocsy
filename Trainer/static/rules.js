(() => {
  const base = window.TRAINER_BASE || "/Trainer";
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  let rules = [], current = null, loaded = false;

  async function api(path, options = {}) {
    const r = await fetch(base + path, {credentials: "same-origin", headers: {"Content-Type": "application/json"}, ...options});
    let body = {}; try { body = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error(body.detail || body.message || `Request failed (${r.status})`);
    return body;
  }
  const rule = () => rules.find((r) => r.key === current);
  const dirty = () => !!rule() && $("#rule-text").value !== rule().text;
  function msg(text, bad = false) { const m = $("#rule-msg"); m.textContent = text || ""; m.style.color = bad ? "var(--red)" : "var(--muted)"; }

  function renderList() {
    const host = $("#rules-list"); host.replaceChildren();
    let group = null;
    for (const r of rules) {
      if (r.group !== group) { group = r.group; host.append(el("div", "rules-group", group)); }
      const b = el("button", "rules-item" + (r.key === current ? " active" : ""));
      b.type = "button";
      b.append(el("strong", null, r.title), el("span", r.is_default ? "rules-tag" : "rules-tag edited", r.is_default ? "default" : "edited"));
      b.addEventListener("click", () => open(r.key));
      host.append(b);
    }
  }
  function renderState() {
    const r = rule(); if (!r) return;
    const st = $("#rule-state");
    st.textContent = dirty() ? "unsaved" : r.is_default ? "default" : "saved";
    st.dataset.state = dirty() ? "" : "saved";
    $("#rule-save").disabled = !dirty();
    $("#rule-revert").disabled = !dirty();
    $("#rule-reset").disabled = !r.default || ($("#rule-text").value === r.default && r.is_default);
  }
  function open(key) {
    if (current && key !== current && dirty() && !confirm("You have unsaved changes to this rule. Throw them away?")) return;
    current = key;
    const r = rule();
    $("#rule-title").textContent = r.title;
    $("#rule-help").textContent = r.help;
    $("#rule-path").textContent = `Stored in ${r.path}`;
    $("#rule-text").value = r.text;
    const h = $("#rule-history"); h.replaceChildren(new Option(r.history.length ? `History (${r.history.length})…` : "No earlier versions", ""));
    for (const v of r.history) h.append(new Option(`${new Date(v.saved_at * 1000).toLocaleString()} · ${v.chars} chars`, v.id));
    h.disabled = !r.history.length;
    msg("");
    try { localStorage.setItem("trainer-rule", key); } catch (_) {}
    renderList(); renderState();
  }
  async function load(keep) {
    try {
      rules = (await api("/api/rules")).rules;
      let pick = keep || current;
      if (!pick) { try { pick = localStorage.getItem("trainer-rule"); } catch (_) {} }
      if (!rules.some((r) => r.key === pick)) pick = rules[0]?.key;
      current = null; open(pick);
      loaded = true;
    } catch (e) { $("#rules-list").textContent = e.message; }
  }
  function replace(updated) { rules = rules.map((r) => (r.key === updated.key ? updated : r)); }

  async function save() {
    if (!dirty()) return;
    try {
      const updated = await api(`/api/rules/${current}`, {method: "PUT", body: JSON.stringify({text: $("#rule-text").value})});
      replace(updated); const k = current; current = null; open(k); msg(`Saved. The next ${updated.group === "Hermes" ? "Hermes session" : "job"} uses it.`);
    } catch (e) { msg(e.message, true); }
  }
  $("#rule-save").addEventListener("click", save);
  $("#rule-text").addEventListener("input", renderState);
  $("#rule-text").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save(); }
    if (e.key === "Tab") {   // insert spaces instead of leaving the editor
      e.preventDefault(); const t = e.target, a = t.selectionStart;
      t.setRangeText("  ", a, t.selectionEnd, "end"); renderState();
    }
  });
  $("#rule-revert").addEventListener("click", () => { $("#rule-text").value = rule().text; renderState(); msg("Unsaved edits thrown away."); });
  $("#rule-reset").addEventListener("click", async () => {
    if (!confirm(`Reset "${rule().title}" to its default? The current text is kept in History.`)) return;
    try { const updated = await api(`/api/rules/${current}/reset`, {method: "POST"}); replace(updated); const k = current; current = null; open(k); msg("Back to the default."); }
    catch (e) { msg(e.message, true); }
  });
  $("#rule-history").addEventListener("change", async (e) => {
    const v = e.target.value; e.target.value = "";
    if (!v) return;
    if (dirty() && !confirm("Replace your unsaved edits with this older version?")) return;
    try { const r = await api(`/api/rules/${current}/history/${v}`); $("#rule-text").value = r.text; renderState(); msg("Older version loaded into the editor. Press Save to make it current."); }
    catch (err) { msg(err.message, true); }
  });
  window.addEventListener("beforeunload", (e) => { if (dirty()) { e.preventDefault(); e.returnValue = ""; } });
  document.addEventListener("rules-tab-shown", () => { if (!loaded) load(); else if (!dirty()) load(current); });
  if (!$("#tab-rules").hidden) load();
})();
