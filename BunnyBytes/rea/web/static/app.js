// Rocsy REA page: files on the left, one open file on the right (overview, functions, strings).
const $ = (id) => document.getElementById(id);
const API = "/REA/api";
const CHUNK = 8 * 1024 * 1024;
const PAGE = 300;
const state = { targets: [], current: null, tab: "overview", data: {}, fnLimit: PAGE, strLimit: PAGE, fn: null, codeView: "pseudo", fileSort: "modified" };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function toast(message, kind = "") {
  const box = $("toast");
  box.textContent = message;
  box.className = `toast ${kind}`;
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, 5000);
}

async function api(path, options = {}) {
  const headers = { "X-REA": "1", ...(options.headers || {}) };
  if (options.json !== undefined) { headers["Content-Type"] = "application/json"; options.body = JSON.stringify(options.json); }
  const response = await fetch(API + path, { ...options, headers });
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = {}; }
  if (!response.ok) throw new Error(typeof data.detail === "string" && data.detail.trim() && !data.detail.trimStart().startsWith("<")
    ? data.detail : `HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ""}`);
  if (text && !response.headers.get("content-type")?.includes("json")) throw new Error("Unexpected response from REA");
  return data;
}

const size = (bytes) => bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GB` : bytes >= 1024 ** 2 ?
  `${(bytes / 1024 ** 2).toFixed(1)} MB` : bytes >= 1024 ? `${(bytes / 1024).toFixed(0)} KB` : `${bytes} B`;
const since = (ms) => { const s = Math.max(0, Math.round((Date.now() - ms) / 1000)); return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`; };
const current = () => state.targets.find((t) => t.id === state.current);

// ------------------------------------------------------------------------- file list --
let pollTimer;
let targetFailureSince = 0;
async function loadTargets() {
  clearTimeout(pollTimer);
  try {
    const data = await api("/targets");
    targetFailureSince = 0;
    state.targets = data.targets;
    const busy = data.targets.filter((t) => t.state === "analyzing").length;
    const open = data.targets.filter((t) => t.state === "ready").length;
    $("status-chip").className = `chip ${busy ? "busy" : "ok"}`;
    $("status-text").textContent = `REA ${data.version}${busy ? ` · analysing ${busy}` : open ? ` · ${open} open` : " · ready"}`;
    renderFiles();
    renderTarget();
  } catch (error) {
    if (!targetFailureSince) targetFailureSince = Date.now();
    const reconnecting = Date.now() - targetFailureSince < 15000;
    $("status-chip").className = `chip ${reconnecting ? "busy" : "bad"}`;
    $("status-text").textContent = reconnecting ? "REA reconnecting…" : `REA unavailable: ${error.message}`;
  }
  pollTimer = setTimeout(loadTargets, targetFailureSince || state.targets.some((t) => t.state === "analyzing") ? 2000 : 15000);
}

const ICONS = {
  folder: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  file: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/></svg>',
};
// Static SVG strings only (never data from the API), parsed once per use.
function icon(name, className = "") {
  const box = el("span", className);
  box.setAttribute("aria-hidden", "true");
  box.append(new DOMParser().parseFromString(ICONS[name], "image/svg+xml").documentElement);
  return box;
}
const shortDate = (ms) => {
  if (!ms) return "";
  const d = new Date(ms);
  return d.toLocaleDateString([], d.getFullYear() === new Date().getFullYear() ? { month: "short", day: "numeric" } : { dateStyle: "medium" });
};

function renderFiles() {
  const list = $("file-list");
  const folders = state.targets.filter((t) => t.type === "folder").length;
  $("files-count").textContent = state.targets.length ? String(state.targets.length) : "";
  renderIntroGames();
  if (!state.targets.length) { list.replaceChildren(el("li", "empty", "Nothing here yet. Add a game folder or upload a file.")); return; }
  // Files opened from a folder sit under it, most worth reverse engineering first.
  const ids = new Set(state.targets.map((t) => t.id));
  const children = new Map();
  for (const t of state.targets) if (t.parent && ids.has(t.parent)) (children.get(t.parent) || children.set(t.parent, []).get(t.parent)).push(t);
  for (const kids of children.values()) kids.sort((a, b) => (a.rank ?? 99) - (b.rank ?? 99) || b.size - a.size || a.rel.localeCompare(b.rel));
  const query = ($("file-filter").value || "").trim().toLowerCase();
  const hit = (t) => !query || `${t.name} ${t.rel || ""} ${t.path}`.toLowerCase().includes(query);
  const roots = state.targets.filter((t) => !(t.parent && ids.has(t.parent)) && (hit(t) || (children.get(t.id) || []).some(hit)));
  const byName = (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });
  roots.sort((a, b) => state.fileSort === "name" ? byName(a, b)
    : state.fileSort === "added" ? (b.added || 0) - (a.added || 0) || byName(a, b)
    : (b.mtime || 0) - (a.mtime || 0) || byName(a, b));
  const item = (target) => {
    const child = Boolean(target.parent && ids.has(target.parent));
    const isFolder = target.type === "folder";
    const li = el("li", `file${child ? " child" : ""}${isFolder ? " folder" : ""}${target.id === state.current ? " active" : ""}`);
    const ic = icon(isFolder ? "folder" : "file", `file-ic ${isFolder ? "folder" : "bin"}`);
    if (!isFolder) ic.append(el("span", `state-dot ${target.state}`));
    const body = el("span", "file-body");
    const name = el("span", "file-name");
    name.append(el("span", "file-label", isFolder ? target.name : child ? target.rel : target.name));
    if (isFolder && children.get(target.id)?.length) name.append(el("span", "rank", `${children.get(target.id).length} open`));
    const meta = isFolder ? [target.mtime && `modified ${shortDate(target.mtime)}`, !target.exists && "not mounted"]
      : [size(target.size), target.format && `${target.format}${target.arch ? ` · ${target.arch}` : ""}`,
        !child && (target.source === "upload" ? "uploaded" : `from ${target.source}`), !target.exists && "missing"];
    body.append(name, el("span", "file-meta", meta.filter(Boolean).join(" · ")));
    li.append(ic, body);
    li.title = target.path;
    li.addEventListener("click", () => select(target.id));
    return li;
  };
  const section = (label, targets) => {
    if (!targets.length) return [];
    const out = [el("li", "group-head", `${label} · ${targets.length}`)];
    for (const t of targets) {
      out.push(item(t));
      for (const kid of children.get(t.id) || []) if (hit(kid) || hit(t)) out.push(item(kid));
    }
    return out;
  };
  const rows = [...section("Games & folders", roots.filter((t) => t.type === "folder")), ...section("Files", roots.filter((t) => t.type !== "folder"))];
  list.replaceChildren(...(rows.length ? rows : [el("li", "empty", `Nothing matches “${query}”.`)]));
  list.dataset.folders = String(folders);
}

// Home screen: the mounted games as cards, so the first click opens one.
function renderIntroGames() {
  const games = state.targets.filter((t) => t.type === "folder" && !t.parent)
    .sort((a, b) => (b.mtime || 0) - (a.mtime || 0) || a.name.localeCompare(b.name));
  $("intro-games").hidden = !games.length;
  $("intro-games-count").textContent = games.length ? `${games.length} mounted read-only` : "";
  $("intro-game-list").replaceChildren(...games.map((game) => {
    const li = el("li");
    const button = el("button", "game-card");
    button.type = "button";
    button.title = game.path;
    const text = el("span", "game-text");
    text.append(el("strong", "", game.name), el("span", "hint", game.exists ? `modified ${shortDate(game.mtime)}` : "not mounted"));
    button.append(icon("folder", "game-ic"), text);
    button.addEventListener("click", () => select(game.id));
    li.append(button);
    return li;
  }));
}

$("file-filter").addEventListener("input", () => renderFiles());
for (const b of document.querySelectorAll("[data-proxy]")) b.addEventListener("click", () => $(b.dataset.proxy).click());

$("file-sort").addEventListener("change", () => { state.fileSort = $("file-sort").value; renderFiles(); });

async function select(id, { open = true } = {}) {
  setTopTab("work");
  if (state.current !== id) {
    state.current = id;
    state.data = {};
    state.fn = null;
    state.fnLimit = state.strLimit = PAGE;
    setTab("overview");
    $("code-col").replaceChildren(el("p", "empty", "Pick a function."));
    $("xref-col").replaceChildren(el("p", "empty", "Pick a string to see what uses it."));
    state.inv = null;
    state.mod = null;
    state.rewrite = null;
    state.fvTab = "files";
    state.invKind = "all";
    state.invLimit = PAGE;
    $("fv-filter").value = "";
    try { localStorage.setItem("rea.current", id); } catch {}
  }
  renderFiles();
  const target = current();
  if (open && target && target.exists && target.state === "closed") await openTarget(target);
  renderTarget();
  if (target?.type === "folder") { loadInventory(); setFolderTab(state.fvTab || "files"); if (state.fvTab !== "notes" && state.fvTab !== "arch") loadKb(); }
}

async function openTarget(target) {
  try {
    Object.assign(target, await api(`/targets/${target.id}/open`, { method: "POST", json: {} }));
    loadTargets();
  } catch (error) { toast(error.message, "error"); }
}

// ------------------------------------------------------------------------ one target --
function renderTarget() {
  const target = current();
  $("library-view").hidden = !state.library;
  if (state.library) { for (const id of ["intro", "target", "mod-view", "rewrite-view"]) $(id).hidden = true; return; }
  $("intro").hidden = Boolean(target) || Boolean(state.mod) || Boolean(state.rewrite);
  $("target").hidden = !target || Boolean(state.mod) || Boolean(state.rewrite);
  $("mod-view").hidden = !state.mod;
  $("rewrite-view").hidden = !state.rewrite;
  if (!target || state.mod || state.rewrite) return;
  $("t-name").textContent = target.name;
  $("t-kind").className = `t-kind ${target.type === "folder" ? "folder" : "bin"}`;
  $("t-kind").replaceChildren(icon(target.type === "folder" ? "folder" : "file"));
  if (target.type === "folder") $("t-meta").textContent = state.inv ? `${state.inv.total.toLocaleString()} files · ${size(state.inv.bytes)} · ${target.path} (read-only)` : target.path;
  else $("t-meta").textContent = [target.format, target.kind, target.arch, size(target.size),
    target.sha256 && `sha256 ${target.sha256.slice(0, 16)}…`, target.path].filter(Boolean).join(" · ");
  $("close-button").hidden = target.state === "closed" || target.type === "folder";
  $("open-button").hidden = !state.local || !target.exists;
  $("open-button").textContent = target.type === "folder" ? "Open folder" : "Show in folder";
  $("folder-view").hidden = target.type !== "folder" || !target.exists;
  const box = $("t-state");
  box.className = "state";
  if (!target.exists) { box.className = "state error"; box.textContent = target.type === "folder" ?
    "This folder is not mounted any more (Add folder lists the mounted ones)." : "This file no longer exists."; }
  else if (target.type === "folder") box.replaceChildren();
  else if (target.state === "analyzing") {
    box.replaceChildren(el("span", "spinner"), el("span", "", `Ghidra is analysing the file… ${since(target.started)}. Big programs can take several minutes; later questions answer instantly.`));
  } else if (target.state === "error") {
    box.className = "state error";
    const retry = el("button", "button", "Try again");
    retry.type = "button";
    retry.addEventListener("click", () => openTarget(target));
    box.replaceChildren(el("span", "", `REA couldn't open this file: ${target.error} — the Master Agent has more tools (APKs, JS apps, .NET, firmware…).`), retry);
  } else if (target.state === "closed") {
    const open = el("button", "button primary", "Open with Ghidra");
    open.type = "button";
    open.addEventListener("click", () => openTarget(target));
    box.replaceChildren(el("span", "", "Not open."), open);
  } else box.replaceChildren();
  $("tabs").hidden = target.state !== "ready";
  for (const panel of document.querySelectorAll("[data-panel]")) panel.hidden = target.state !== "ready" || panel.dataset.panel !== state.tab;
  if (target.state === "ready") {
    renderOverview(target);
    if (state.tab === "functions") ensureFunctions();
    if (state.tab === "strings") ensureStrings();
  }
}

function renderOverview(target) {
  const o = target.overview || {};
  const stats = el("div", "stats");
  for (const [value, label] of [[o.procedure_count, "functions"], [o.string_count, "strings"], [o.segment_count, "segments"],
    [target.format?.toUpperCase(), "format"], [target.arch, "architecture"]]) {
    if (value === undefined || value === null) continue;
    const stat = el("div", "stat");
    stat.append(el("b", "", String(value)), el("span", "", label));
    stats.append(stat);
  }
  const nodes = [stats];
  if (o.segments?.length) {
    const table = el("table", "segs");
    const head = el("tr");
    for (const h of ["Segment", "Start", "End", "Size"]) head.append(el("th", "", h));
    table.append(head);
    for (const s of o.segments) {
      const row = el("tr");
      row.append(el("td", "", s.name), el("td", "", s.start), el("td", "", s.end), el("td", "", size(s.length)));
      table.append(row);
    }
    nodes.push(table);
  }
  $("overview").replaceChildren(...nodes);
}

function setTab(tab) {
  state.tab = tab;
  for (const button of document.querySelectorAll("#tabs button")) button.setAttribute("aria-pressed", String(button.dataset.tab === tab));
  renderTarget();
}

async function ask(tool, args = {}) {
  const target = current();
  return api(`/targets/${target.id}/call`, { method: "POST", json: { tool, args } });
}

// -------------------------------------------------------------------------- functions --
async function ensureFunctions() {
  const id = state.current;
  if (state.data.procs || state.data.procsLoading) return renderFunctions();
  state.data.procsLoading = true;
  $("fn-count").textContent = "Loading…";
  try {
    const { result } = await ask("list_procedures");
    if (state.current !== id) return;
    state.data.procs = (result || []).map((p) => ({ address: p.address, name: String(p.value || p.address).replace(/^<EXTERNAL>::/, ""),
      external: Boolean(p.procedure?.external), thunk: Boolean(p.procedure?.thunk) }));
    state.data.byName = new Map(state.data.procs.map((p) => [p.name, p]));
    state.data.byAddr = new Map(state.data.procs.map((p) => [p.address, p]));
  } catch (error) { $("fn-count").textContent = error.message; }
  finally { state.data.procsLoading = false; }
  renderFunctions();
}

function renderFunctions() {
  const procs = state.data.procs;
  if (!procs) return;
  const query = $("fn-filter").value.trim().toLowerCase();
  const imports = $("fn-external").checked;
  const shown = procs.filter((p) => (imports || !p.external) && (!query || p.name.toLowerCase().includes(query) || p.address.toLowerCase().includes(query)));
  $("fn-count").textContent = `${shown.length} of ${procs.length} functions${imports ? "" : " (imports hidden)"}`;
  const rows = shown.slice(0, state.fnLimit).map((p) => {
    const item = el("li", state.fn?.address === p.address ? "active" : "");
    item.append(el("span", "val", p.name), el("span", "addr", `${p.address}${p.external ? " · import" : p.thunk ? " · thunk" : ""}`));
    item.addEventListener("click", () => showFunction(p));
    return item;
  });
  if (shown.length > state.fnLimit) {
    const more = el("li", "more", `Show ${Math.min(PAGE, shown.length - state.fnLimit)} more`);
    more.addEventListener("click", () => { state.fnLimit += PAGE; renderFunctions(); });
    rows.push(more);
  }
  $("fn-list").replaceChildren(...rows);
}

const KEYWORDS = new Set("if else while do for return break continue switch case default goto sizeof struct union enum typedef".split(" "));
const TYPES = /^(void|char|short|int|long|float|double|bool|unsigned|signed|const|undefined\d*|byte|word|dword|qword|uint\d*|int\d*|u?int\d+_t|size_t|code|ulong|uint|ushort|uchar)$/;

// Pseudocode with colours, and every known function name as a link.
function highlight(code) {
  const frag = document.createDocumentFragment();
  const token = /(\/\*[\s\S]*?\*\/|\/\/[^\n]*)|("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|\b(0x[0-9a-fA-F]+|\d+)\b|([A-Za-z_][A-Za-z0-9_]*)/g;
  let last = 0;
  for (const m of code.matchAll(token)) {
    if (m.index > last) frag.append(code.slice(last, m.index));
    last = m.index + m[0].length;
    const [text, comment, string, number, ident] = m;
    if (comment) frag.append(el("span", "cm", text));
    else if (string) frag.append(el("span", "str", text));
    else if (number) frag.append(el("span", "num", text));
    else if (ident && state.data.byName?.has(ident)) {
      const link = el("a", "", text);
      link.addEventListener("click", () => showFunction(state.data.byName.get(ident)));
      frag.append(link);
    } else if (ident && KEYWORDS.has(ident)) frag.append(el("span", "kw", text));
    else if (ident && TYPES.test(ident)) frag.append(el("span", "ty", text));
    else frag.append(text);
  }
  frag.append(code.slice(last));
  return frag;
}

function procLink(address) {
  const known = state.data.byAddr?.get(address);
  const link = el("a", "", known ? known.name : address);
  link.title = address;
  link.addEventListener("click", () => known ? showFunction(known) : showAddress(address));
  return link;
}

async function showAddress(address) {
  try {
    const { result } = await ask("resolve_containing_procedure", { address });
    if (!result?.found) { toast(`No function contains ${address}`); return; }
    setTab("functions");
    await ensureFunctions();
    showFunction(state.data.byAddr.get(result.procedure.address) || { address: result.procedure.address, name: result.procedure.name });
  } catch (error) { toast(error.message, "error"); }
}

async function showFunction(proc) {
  if (state.tab !== "functions") setTab("functions");
  state.fn = proc;
  renderFunctions();
  const col = $("code-col");
  const head = el("div", "code-head");
  head.append(el("h2", "", proc.name));
  const seg = el("div", "seg");
  for (const [view, label] of [["pseudo", "Pseudocode"], ["asm", "Assembly"]]) {
    const button = el("button", "", label);
    button.type = "button";
    button.setAttribute("aria-pressed", String(state.codeView === view));
    button.addEventListener("click", () => { state.codeView = view; showFunction(proc); });
    seg.append(button);
  }
  head.append(seg);
  const pre = el("pre", "code", "Loading…");
  const callers = el("div", "links-row");
  const callees = el("div", "links-row");
  col.replaceChildren(head, el("p", "hint", proc.address), callers, callees, pre);
  if (proc.external) { pre.textContent = "Imported from a library: there is no code in this file."; return; }
  const view = state.codeView;
  const [code, from, to] = await Promise.allSettled([
    ask(view === "pseudo" ? "procedure_pseudo_code" : "procedure_assembly", { procedure: proc.address }),
    ask("procedure_callers", { procedure: proc.address }), ask("procedure_callees", { procedure: proc.address })]);
  if (state.fn !== proc || state.codeView !== view) return;
  pre.replaceChildren();
  if (code.status === "fulfilled") {
    const text = typeof code.value.result === "string" ? code.value.result : JSON.stringify(code.value.result, null, 2);
    pre.append(view === "pseudo" ? highlight(text.trim()) : text);
    if (code.value.cached) pre.before(el("p", "code-note", "Already decompiled earlier: loaded from this game's decompiled/ folder (see the Decompiled tab)."));
  } else { pre.textContent = code.reason.message; pre.classList.add("error"); }
  for (const [box, result, label] of [[callers, from, "Called by"], [callees, to, "Calls"]]) {
    const list = result.status === "fulfilled" && Array.isArray(result.value.result) ? result.value.result : [];
    const links = el("div", "links");
    if (!list.length) links.append(el("span", "hint", "none found"));
    for (const address of list.slice(0, 60)) links.append(procLink(typeof address === "string" ? address : address.address));
    if (list.length > 60) links.append(el("span", "hint", `+${list.length - 60} more`));
    box.replaceChildren(el("span", "", label), links);
  }
}

// ---------------------------------------------------------------------------- strings --
async function ensureStrings() {
  const id = state.current;
  if (state.data.strings || state.data.stringsLoading) return renderStrings();
  state.data.stringsLoading = true;
  $("str-count").textContent = "Loading…";
  try {
    const { result } = await ask("list_strings");
    if (state.current !== id) return;
    state.data.strings = (result || []).map((s) => ({ address: s.address, value: String(s.value ?? "") }));
  } catch (error) { $("str-count").textContent = error.message; }
  finally { state.data.stringsLoading = false; }
  renderStrings();
}

function renderStrings() {
  const strings = state.data.strings;
  if (!strings) return;
  const query = $("str-filter").value.toLowerCase();
  const shown = query ? strings.filter((s) => s.value.toLowerCase().includes(query)) : strings;
  $("str-count").textContent = `${shown.length} of ${strings.length} strings`;
  const rows = shown.slice(0, state.strLimit).map((s) => {
    const item = el("li");
    item.append(el("span", "val", s.value.length > 240 ? s.value.slice(0, 240) + "…" : s.value), el("span", "addr", s.address));
    item.addEventListener("click", () => {
      for (const row of $("str-list").children) row.classList.remove("active");
      item.classList.add("active");
      showXrefs(s);
    });
    return item;
  });
  if (shown.length > state.strLimit) {
    const more = el("li", "more", `Show ${Math.min(PAGE, shown.length - state.strLimit)} more`);
    more.addEventListener("click", () => { state.strLimit += PAGE; renderStrings(); });
    rows.push(more);
  }
  $("str-list").replaceChildren(...rows);
}

async function showXrefs(string) {
  const col = $("xref-col");
  const pre = el("pre", "code", string.value);
  const refs = el("div", "links-row");
  refs.append(el("span", "", "Used by"), el("span", "hint", "Looking…"));
  col.replaceChildren(el("div", "code-head"), refs, pre);
  col.firstChild.append(el("h2", "", string.address));
  try {
    const { result } = await ask("xrefs", { address: string.address });
    const addresses = Array.isArray(result) ? result.map((r) => (typeof r === "string" ? r : r.address || r.from)).filter(Boolean) : [];
    const links = el("div", "links");
    if (!addresses.length) links.append(el("span", "hint", "No references found."));
    for (const address of addresses.slice(0, 40)) {
      const link = el("a", "", address);
      link.addEventListener("click", () => showAddress(address));
      links.append(link);
      // Show which function each reference sits in.
      ask("resolve_containing_procedure", { address }).then(({ result: r }) => {
        if (r?.found) link.textContent = `${r.procedure.name} (${address})`;
      }).catch(() => {});
    }
    refs.replaceChildren(el("span", "", "Used by"), links);
  } catch (error) { refs.replaceChildren(el("span", "", "Used by"), el("span", "hint error", error.message)); }
}

// ------------------------------------------------------------------------ folder view --
const KIND_LABELS = { executable: "Executables", library: "Libraries", dotnet: ".NET assemblies", package: "Packages & game data",
  script: "Scripts & WebAssembly", archive: "Archives", other: "Other files" };
const NATIVE = new Set(["ELF", "PE EXE", "PE DLL", "Mach-O"]);

async function loadInventory(refresh = false) {
  const target = current();
  if (!target || target.type !== "folder" || !target.exists) return;
  $("fv-count").textContent = refresh ? "Rescanning…" : "Scanning the folder…";
  try {
    const inv = await api(`/targets/${target.id}/inventory${refresh ? "?refresh=1" : ""}`);
    if (state.current !== target.id) return;
    state.inv = inv;
    state.invLimit = PAGE;
    renderInventory();
    if (state.modData?.folder === target.id) renderMod();
  } catch (error) { $("fv-count").textContent = error.message; }
}

const languageNames = (languages) => languages?.length ? languages.map((x) => x.name) : ["Lang Unkown"];

function renderLanguages(languages) {
  const entries = languages?.length ? languages : [{ name: "Unknown", evidence: [] }];
  for (const id of ["fv-languages", "decomp-languages"]) {
    const chips = entries.map((entry) => {
      const chip = el("span", "engine", entry.name);
      chip.title = entry.evidence?.length ? `${entry.source}: ${entry.evidence.join(", ")}` : "No language evidence found yet";
      return chip;
    });
    $(id).replaceChildren(el("span", "tag-label", "Languages"), ...chips);
  }
}

function renderInventory() {
  const inv = state.inv;
  const target = current();
  if (!inv || !target) return;
  $("t-meta").textContent = `${inv.total.toLocaleString()} files · ${size(inv.bytes)} · ${target.path} (read-only)`;
  const stats = [];
  for (const kind of ["executable", "library", "dotnet", "package", "script"]) {
    if (!inv.kinds[kind]) continue;
    const stat = el("div", "stat");
    stat.append(el("b", "", inv.kinds[kind].count.toLocaleString()), el("span", "", KIND_LABELS[kind]));
    stats.push(stat);
  }
  $("fv-stats").replaceChildren(...stats);
  $("fv-engines").replaceChildren(el("span", "tag-label", "Engine"), ...(inv.engines.length ? inv.engines.map((e) => {
    const chip = el("span", "engine", e.version ? `${e.name} ${e.version}` : e.name);
    chip.title = `Found: ${e.evidence.join(", ")}${e.version ? `\nVersion ${e.version} from ${e.versionSource}` : ""}`;
    return chip;
  }) : [el("span", "hint", "No known game engine or framework spotted.")]));
  renderLanguages(inv.languages);
  const seg = $("fv-kinds");
  const kinds = [["all", `All ${inv.total}`], ...Object.keys(KIND_LABELS).filter((k) => inv.kinds[k]).map((k) => [k, `${KIND_LABELS[k]} ${inv.kinds[k].count}`]),
    ...(inv.re?.text ? [["re:text", `Text & config ${inv.re.text}`]] : []), ...(inv.re?.none ? [["re:none", `Can't reverse engineer ${inv.re.none}`]] : [])];
  seg.replaceChildren(...kinds.map(([kind, label]) => {
    const button = el("button", "", label);
    button.type = "button";
    button.setAttribute("aria-pressed", String(state.invKind === kind));
    button.addEventListener("click", () => { state.invKind = kind; state.invLimit = PAGE; renderInventory(); });
    return button;
  }));
  const query = $("fv-filter").value.trim().toLowerCase();
  const match = (f) => state.invKind === "all" || (state.invKind.startsWith("re:") ? f.re === state.invKind.slice(3) : f.kind === state.invKind);
  const shown = inv.files.filter((f) => match(f) && (!query || f.rel.toLowerCase().includes(query)));
  $("fv-count").textContent = `${shown.length.toLocaleString()} files${inv.truncated ? ` (folder has more than ${inv.total.toLocaleString()} files; only those were scanned)` : ""} · ranked: most worth reverse engineering first`;
  const rows = shown.slice(0, state.invLimit).map((f) => {
    const item = el("li", f.re === "none" ? "cant" : "");
    item.append(el("span", "val", f.rel), el("span", "addr", [f.format, size(f.size)].filter(Boolean).join(" · ")));
    if (f.re === "none" || f.re === "text") item.append(el("span", "why", f.reason));
    const acts = el("span", "acts");
    acts.append(el("span", `re ${f.re}`, { ghidra: "Ghidra", agent: "Agent", text: "Text", none: "Can't" }[f.re] || f.re));
    acts.lastChild.title = f.reason;
    if (NATIVE.has(f.format)) {
      const open = el("button", "button", "Open");
      open.type = "button";
      open.title = "Analyse with Ghidra";
      open.addEventListener("click", (event) => { event.stopPropagation(); openInside(target, f.rel); });
      acts.append(open);
    }
    if (state.local) {
      const show = el("button", "button", "Show");
      show.type = "button";
      show.title = "Show in this computer's file manager";
      show.addEventListener("click", (event) => { event.stopPropagation(); openOnHost(`${target.path}/${f.rel}`); });
      acts.append(show);
    }
    const askButton = el("button", "button", "Ask agent");
    askButton.type = "button";
    if (f.re === "none") askButton.hidden = true;
    askButton.addEventListener("click", (event) => { event.stopPropagation(); askAbout({ ...target, path: `${target.path}/${f.rel}`, name: f.rel, format: f.format, type: "file" }); });
    acts.append(askButton);
    item.append(acts);
    return item;
  });
  if (shown.length > state.invLimit) {
    const more = el("li", "more", `Show ${Math.min(PAGE, shown.length - state.invLimit)} more`);
    more.addEventListener("click", () => { state.invLimit += PAGE; renderInventory(); });
    rows.push(more);
  }
  $("fv-list").replaceChildren(...rows);
  const skipped = inv.skipped || [];
  $("fv-skipped").hidden = !skipped.length;
  $("fv-skipped-sum").textContent = `${skipped.length} folder${skipped.length === 1 ? "" : "s"}, links or files REA couldn't look into`;
  $("fv-skipped-list").replaceChildren(...skipped.map((x) => {
    const item = el("li", "cant");
    item.append(el("span", "val", x.type === "folder" ? `${x.rel}/` : x.rel), el("span", "why", x.reason));
    const acts = el("span", "acts");
    acts.append(el("span", "re none", x.type));
    if (state.local && x.type === "folder") {
      const show = el("button", "button", "Show");
      show.type = "button";
      show.addEventListener("click", () => openOnHost(`${target.path}/${x.rel}`));
      acts.append(show);
    }
    item.append(acts);
    return item;
  }));
}

// Host-only: open a path in this computer's file manager (gateway → hostctl). The button only
// shows when the gateway says this browser runs on the host itself.
async function openOnHost(containerPath) {
  try { await gw("/api/rea/open", { method: "POST", json: { path: containerPath } }); toast("Opened in the file manager"); }
  catch (error) { toast(error.message, "error"); }
}
$("open-button").addEventListener("click", () => openOnHost(current().path));

async function openInside(folder, rel) {
  try {
    const target = await api("/targets", { method: "POST", json: { folder: folder.id, rel } });
    await loadTargets();
    select(target.id);
  } catch (error) { toast(error.message, "error"); }
}
$("fv-filter").addEventListener("input", () => { state.invLimit = PAGE; renderInventory(); });
$("fv-refresh").addEventListener("click", () => loadInventory(true));

// ------------------------------------------------------------------- REA's model --
// One Master Agent model + thinking level for every REA task (server: /REA/api/settings; the Agent
// service accepts {model} on session create/message). Model "" = the Agentic settings' model.
let modelSettings = { model: { model: "", provider: "", reasoning: "medium" }, tasks: {} };
async function loadModelSettings() {
  try { modelSettings = await api("/settings"); } catch {}
}
// Every task sends the same choice; an empty one resets a session to the Agentic settings.
const modelFor = () => ({ model: modelSettings.model || { model: "" } });

$("models-button").addEventListener("click", async () => {
  $("models").showModal();
  const rows = $("model-rows");
  rows.replaceChildren(el("p", "hint", "Loading the model list…"));
  await loadModelSettings();
  let hermes;
  try { hermes = (await fetch("/Agent/api/options").then((r) => r.json())).hermes; } catch {}
  if (!hermes?.providers) { rows.replaceChildren(el("p", "hint error", "Couldn't load the Master Agent's model list (is the Agent service running?).")); return; }
  const def = hermes.default?.model ? ` (${hermes.default.model})` : "";
  const row = el("div", "model-row");
  const pick = el("select");
  pick.id = "rea-model";
  pick.append(new Option(`Agentic default${def}`, ""));
  for (const pr of hermes.providers) {
    const group = document.createElement("optgroup");
    group.label = `${pr.name}${pr.local ? " · local GPU" : ""}`;
    for (const m of pr.models) group.append(new Option(m, JSON.stringify([pr.slug, m])));
    pick.append(group);
  }
  const think = el("select");
  think.id = "rea-thinking";
  think.append(new Option("Thinking: Agentic default", ""));
  for (const r of hermes.reasoning || ["default"]) think.append(new Option(r === "default" ? "Thinking: model default" : `Thinking: ${r}`, r));
  const cur = modelSettings.model || {};
  if (cur.model) pick.value = JSON.stringify([cur.provider, cur.model]);
  think.value = cur.reasoning ?? "medium";
  row.append(el("span", "", "All REA tasks"), pick, think);
  const used = el("p", "hint", `Used for: ${Object.values(modelSettings.tasks || {}).join(" · ")}.`);
  rows.replaceChildren(row, used);
});
$("models-save").addEventListener("click", async () => {
  const pick = $("rea-model"), think = $("rea-thinking");
  if (!pick) return;
  const [provider, model] = pick.value ? JSON.parse(pick.value) : ["", ""];
  try {
    modelSettings = await api("/settings", { method: "PUT", json: { model: { model, provider, reasoning: think.value } } });
    $("models").close();
    toast("Saved: REA's model and thinking apply from the next message");
  } catch (error) { toast(error.message, "error"); }
});

// -------------------------------------------------------------------------------- mods --
// Guided modding (mods.mjs on the server, hostctl/reamods.py for installs, Hermes skill
// rocsy/rea-modding for the agent). Each step the user takes becomes a message in one
// Master Agent session, so the agent keeps the context of the whole mod.
function setFolderTab(tab) {
  if (tab !== "sessions") clearTimeout(sessTimer);
  if (tab !== "decomp") $("decomp-viewer").hidden = true;
  state.fvTab = tab;
  for (const b of document.querySelectorAll("#fv-tabs button")) b.setAttribute("aria-pressed", String(b.dataset.fvtab === tab));
  $("fv-files").hidden = tab !== "files";
  $("fv-builds").hidden = tab !== "builds";
  $("fv-notes").hidden = tab !== "notes";
  $("fv-arch").hidden = tab !== "arch";
  $("fv-rules").hidden = tab !== "rules";
  $("fv-sessions").hidden = tab !== "sessions";
  $("fv-decomp").hidden = tab !== "decomp";
  $("fv-logs").hidden = tab !== "logs";
  if (tab !== "logs") $("logs-viewer").hidden = true;
  if (tab === "builds") setBuildTab(state.buildTab || "mods");
  if (tab === "files" && state.inv) loadInventory();
  if (tab === "sessions") loadSessions();
  if (tab === "decomp") loadDecomp();
  if (tab === "logs") loadLogs();
  if (tab === "notes" || tab === "arch") loadKb();
  if (tab === "rules") loadRules();
}
for (const b of document.querySelectorAll("#fv-tabs button")) b.addEventListener("click", () => setFolderTab(b.dataset.fvtab));

// Rules tab (rules.mjs): per-engine hot files the mod agent works on, and what it leaves alone.
// Opens on this game's engine and shows which of its files each rule matches.
async function loadRules(engine) {
  const target = current();
  try {
    const [{ engines }, here] = await Promise.all([api("/rules"),
      target?.type === "folder" ? api(`/rules/for?folder=${target.id}`).catch(() => null) : null]);
    state.rules = { engines, here, engine: engine || (state.rules?.target === target?.id && state.rules.engine) || here?.engine || "unity-mono", target: target?.id };
    renderRules();
  } catch (error) { toast(error.message, "error"); }
}

function renderRules() {
  const { engines, here, engine } = state.rules;
  const r = engine === here?.engine ? here.rules : engines.find((e) => e.id === engine);
  $("rules-engine").replaceChildren(...engines.map((e) => {
    const o = el("option", "", `${e.label}${e.id === here?.engine ? " · this game" : ""}${e.custom ? " · edited" : ""}`);
    o.value = e.id;
    return o;
  }));
  $("rules-engine").value = engine;
  $("rules-meta").textContent = `${r.custom ? "Your edited rules" : "Default rules"} for ${r.label}` +
    (engine === here?.engine ? ` · matched against this game's folder (${here.label})` : "") +
    (engine === "all" ? " · sent with every engine's rules" : " · plus the All engines rules");
  $("rules-reset").hidden = !r.custom;
  const rows = (list, empty) => list.length ? list.map((x) => {
    const item = el("li");
    const row = el("div", "kb-row");
    row.append(el("code", "", x.pattern));
    if (x.files) row.append(x.files.length ? el("span", "badge works", `${x.files.length}${x.files.length === 20 ? "+" : ""} in this game`) : el("span", "badge", "not in this game"));
    item.append(row);
    if (x.why) item.append(el("span", "hint", x.why));
    if (x.files?.length) item.title = x.files.join("\n");
    return item;
  }) : [el("li", "empty", empty)];
  $("rules-focus").replaceChildren(...rows(r.focus, "No hot files for this engine yet. Edit to add the files that hold the game's code."));
  $("rules-skip").replaceChildren(...rows(r.skip, "Nothing to leave alone."));
  $("rules-notes").replaceChildren(...(r.notes ? [markdown(r.notes)] : []));
}

const rulesText = (list) => list.map((x) => x.why ? `${x.pattern} — ${x.why}` : x.pattern).join("\n");
const rulesParse = (text) => text.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
  const [pattern, ...why] = l.split(/\s+[—–-]\s+/);
  return { pattern: pattern.trim(), why: why.join(" - ").trim() };
});
$("rules-engine").addEventListener("change", () => { state.rules.engine = $("rules-engine").value; $("rules-editor").hidden = true; $("rules-view").hidden = false; renderRules(); });
$("rules-edit").addEventListener("click", () => {
  const r = state.rules.engines.find((e) => e.id === state.rules.engine);
  $("rules-focus-text").value = rulesText(r.focus);
  $("rules-skip-text").value = rulesText(r.skip);
  $("rules-notes-text").value = r.notes;
  $("rules-view").hidden = true;
  $("rules-editor").hidden = false;
});
$("rules-cancel").addEventListener("click", () => { $("rules-editor").hidden = true; $("rules-view").hidden = false; });
$("rules-save").addEventListener("click", async () => {
  try {
    await api(`/rules/${state.rules.engine}`, { method: "PUT", json: { focus: rulesParse($("rules-focus-text").value),
      skip: rulesParse($("rules-skip-text").value), notes: $("rules-notes-text").value.trim() } });
    $("rules-editor").hidden = true; $("rules-view").hidden = false;
    toast("Rules saved. The mod agent gets them with its next message.");
    loadRules(state.rules.engine);
  } catch (error) { toast(error.message, "error"); }
});
$("rules-reset").addEventListener("click", async () => {
  if (!confirm("Reset this engine's rules to the defaults?")) return;
  try { await api(`/rules/${state.rules.engine}`, { method: "DELETE" }); loadRules(state.rules.engine); }
  catch (error) { toast(error.message, "error"); }
});

function setBuildTab(tab) {
  state.buildTab = tab;
  for (const b of document.querySelectorAll("#build-tabs button")) b.setAttribute("aria-pressed", String(b.dataset.buildtab === tab));
  $("fv-mods").hidden = tab !== "mods";
  $("fv-rewrites").hidden = tab !== "rewrites";
  if (tab === "mods") loadMods();
  else loadRewrites();
}
for (const b of document.querySelectorAll("#build-tabs button")) b.addEventListener("click", () => setBuildTab(b.dataset.buildtab));

// Sections inside an open mod or rewrite (Overview · Steps · Files · Architecture); the last one is remembered per type.
// Goals tab: the agent's standing goal and its milestones, checked off with proof (mirrored from the
// Agent session into GOALS.json by REA).
function renderGoals(base, goals) {
  const box = $(`${base}-goals`), count = $(`${base}-tab-goals`);
  const parts = goals?.parts || [];
  const done = parts.filter((p) => p.status === "done").length;
  count.textContent = parts.length ? `${done}/${parts.length}` : "";
  if (!goals?.text) {
    box.replaceChildren(el("p", "hint", "No goal yet. When you give this project's agent a standing goal (/goal), it splits it into milestones here and checks them off as it proves them."));
    return;
  }
  const state = goals.achieved ? "Achieved" : { active: "Active", paused: "Paused", done: "Achieved", cleared: "Cleared" }[goals.status] || goals.status;
  const head = el("div", "goal-head");
  head.append(el("span", `pill ${goals.achieved || goals.status === "done" ? "ok" : goals.status === "active" ? "run" : ""}`, state),
    el("span", "hint", `${parts.length ? `${done} of ${parts.length} milestones proven · ` : ""}${goals.turns} turn${goals.turns === 1 ? "" : "s"}` +
      `${goals.maxTurns ? ` of ${goals.maxTurns}` : ""} · updated ${when(goals.updated)}`));
  const list = el("ol", "goal-parts");
  parts.forEach((p, i) => {
    const current = i === goals.part && p.status !== "done" && goals.status === "active";
    const item = el("li", p.status === "done" ? "done" : current ? "current" : "");
    item.append(el("span", "goal-check", p.status === "done" ? "✓" : current ? "▶" : ""), el("span", "goal-text", p.text));
    if (p.evidence) item.append(el("span", "goal-proof", `Proof: ${p.evidence}`));
    else if (current) item.append(el("span", "goal-proof hint", "Working on it now"));
    list.append(item);
  });
  const whole = el("details", "goal-whole");
  whole.append(el("summary", "", "The whole goal"), el("p", "", goals.text));
  const out = [head];
  if (parts.length) out.push(list);
  else out.push(el("p", "hint", "Not split into milestones yet: the agent does that first on its next turn."));
  if (goals.evidence) out.push(el("p", "goal-proof", `Final proof: ${goals.evidence}`));
  out.push(whole);
  box.replaceChildren(...out);
}

function setProjectTab(base, tab) {
  const view = $(`${base}-view`);
  if (!view.querySelector(`[data-panel="${tab}"]`)) tab = "overview";
  for (const b of view.querySelectorAll(".project-tabs button")) b.setAttribute("aria-pressed", String(b.dataset.ptab === tab));
  for (const panel of view.querySelectorAll(".ptab-panel")) panel.hidden = panel.dataset.panel !== tab;
  try { localStorage.setItem(`rea.${base}.tab`, tab); } catch {}
}
function savedProjectTab(base) {
  try { return localStorage.getItem(`rea.${base}.tab`) || "overview"; } catch { return "overview"; }
}
for (const base of ["mod", "rewrite"]) {
  for (const b of document.querySelectorAll(`#${base}-tabs button`)) b.addEventListener("click", () => setProjectTab(base, b.dataset.ptab));
}

// Compact progress strip above the tabs: one chip per step, mirroring its number, done/active state and pill.
function renderStrip(base, steps) {
  $(`${base}-strip`).replaceChildren(...steps.map((li) => {
    const pillEl = li.querySelector(".pill");
    const item = el("li", ["done", "active"].filter((c) => li.classList.contains(c)).join(" "));
    const button = el("button");
    button.type = "button";
    const text = el("span", "strip-text");
    text.append(el("strong", "", li.dataset.short || li.querySelector("h2").textContent), el("span", pillEl?.className || "pill", pillEl?.textContent || ""));
    button.title = [li.querySelector("h2").textContent, pillEl?.textContent].filter(Boolean).join(": ");
    button.append(el("span", "step-n", li.querySelector(".step-n").textContent), text);
    button.addEventListener("click", () => { setProjectTab(base, "steps"); li.scrollIntoView({ block: "start", behavior: "smooth" }); });
    item.append(button);
    return item;
  }));
}

async function loadRewrites() {
  const target = current();
  if (!target || target.type !== "folder") return;
  try {
    const { rewrites } = await api(`/mods/rewrites?folder=${encodeURIComponent(target.id)}`);
    if (current()?.id !== target.id) return;
    $("rewrite-list").replaceChildren(...rewrites.map((rewrite) => {
      const item = el("li");
      item.append(el("strong", "", rewrite.name), el("span", "hint", [rewrite.language, rewrite.engine, rewrite.stage, `${rewrite.parityPassed}/${rewrite.parity.length} verified checks`].filter(Boolean).join(" · ")));
      item.addEventListener("click", () => openRewrite(rewrite.id));
      return item;
    }));
  } catch (error) { toast(error.message, "error"); }
}

$("rewrite-create").addEventListener("click", async () => {
  const target = current();
  if (!target) return;
  try {
    const rewrite = await api("/mods/rewrites", { method: "POST", json: { folder: target.id, name: $("rewrite-name").value,
      language: $("rewrite-language").value, engine: $("rewrite-engine").value, goal: $("rewrite-goal").value } });
    openRewrite(rewrite.id);
  } catch (error) { toast(error.message, "error"); }
});

let rewriteTimer;
async function openRewrite(id) {
  loadProjectBrowser("rewrite", id);
  if (state.rewrite !== id) setProjectTab("rewrite", savedProjectTab("rewrite"));
  state.rewrite = id;
  state.rewriteData = null;
  rewriteCheck.hide();
  rewriteCheck.load().catch((error) => console.warn(error));
  renderTarget();
  await refreshRewrite();
}

async function refreshRewrite() {
  clearTimeout(rewriteTimer);
  const id = state.rewrite;
  if (!id) return;
  try {
    const rewrite = await api(`/mods/rewrites/${id}`);
    if (state.rewrite !== id) return;
    state.rewriteData = rewrite;
    $("rewrite-title").textContent = rewrite.name;
    $("rewrite-meta").textContent = [rewrite.gameName, rewrite.language, rewrite.engine, rewrite.stage, rewrite.projectPath].filter(Boolean).join(" · ");
    $("rewrite-goal-text").textContent = rewrite.goal || "Match the original game's behavior and content.";
    $("rewrite-plan").textContent = rewrite.plan;
    renderGoals("rewrite", rewrite.goals);
    $("rewrite-plan").hidden = !rewrite.plan.trim();
    $("rewrite-plan-button").hidden = Boolean(rewrite.plan.trim());
    $("rewrite-revise").hidden = !rewrite.plan.trim();
    $("rewrite-approve").hidden = !rewrite.plan.trim() || ["approved", "building", "testing"].includes(rewrite.stage);
    $("rewrite-source").textContent = `${rewrite.sourceCount} top-level source item(s) · ${rewrite.projectPath}`;
    $("rewrite-session").hidden = !rewrite.session;
    if (rewrite.session) $("rewrite-session").href = `/Agent/?s=${encodeURIComponent(rewrite.session)}`;
    $("rewrite-parity-summary").textContent = rewrite.parity.length
      ? `${rewrite.parityPassed} of ${rewrite.parity.length} checks passed with evidence. Unverified and failed checks still need work.`
      : "No parity checks recorded yet. Ask the agent to inventory the original game and write PARITY.json.";
    $("rewrite-checks").replaceChildren(...rewrite.parity.map((check) => {
      const item = el("li", check.status);
      item.append(el("strong", "", `${check.status}: ${check.name}`), el("span", "hint", check.evidence || "No evidence recorded"));
      return item;
    }));
    const planned = Boolean(rewrite.plan.trim());
    const approved = ["approved", "building", "testing"].includes(rewrite.stage);
    const failed = rewrite.parity.filter((check) => check.status === "failed").length;
    const allPassed = rewrite.parity.length > 0 && rewrite.parityPassed === rewrite.parity.length;
    pill("rw-st-plan", approved ? "Approved" : planned ? "Waiting for your approval" : rewrite.stage === "planning" ? "The agent is planning…" : "Not yet",
      approved ? "ok" : planned || rewrite.stage === "planning" ? "run" : "");
    pill("rw-st-build", rewrite.stage === "building" ? "Building…" : rewrite.stage === "testing" ? "Testing" : rewrite.sourceCount ? `${rewrite.sourceCount} source item(s)` : approved ? "Ready to build" : "Not yet",
      ["building", "testing"].includes(rewrite.stage) ? "run" : rewrite.sourceCount ? "ok" : "");
    pill("rw-st-parity", rewrite.parity.length ? `${rewrite.parityPassed}/${rewrite.parity.length} verified${failed ? ` · ${failed} failed` : ""}` : "No checks yet",
      allPassed ? "ok" : failed ? "bad" : rewrite.parity.length ? "run" : "");
    const rsteps = [...document.querySelectorAll("#rewrite-view .step[data-rstep]")];
    const rstate = { plan: [approved, true], build: [rewrite.sourceCount > 0 && allPassed, approved], parity: [allPassed, rewrite.sourceCount > 0] };
    for (const li of rsteps) {
      const [done, active] = rstate[li.dataset.rstep];
      li.classList.toggle("done", Boolean(done));
      li.classList.toggle("active", Boolean(active) && !done);
    }
    renderStrip("rewrite", rsteps);
  } catch (error) { toast(error.message, "error"); }
  if (state.rewrite === id) rewriteTimer = setTimeout(refreshRewrite, 5000);
}

// Engines and tool addons installed in REA (Core Backend), relevant to this engine. New sessions get
// it in their brief; an existing session hears again only when the installed set changed.
async function toolsNote(engine = "") {
  try { return (await api(`/tools${engine ? `?engine=${encodeURIComponent(engine)}` : ""}`)).brief || ""; } catch { return ""; }
}
function toolsSeen(session, note) {
  const key = `rea-tools:${session}`;
  let seen = null;
  try { seen = localStorage.getItem(key); localStorage.setItem(key, note); } catch {}
  return seen;
}
async function toolsUpdate(session, engine = "") {
  const note = await toolsNote(engine);
  const seen = toolsSeen(session, note);
  return seen === null || seen === note ? "" : `\n\n${note ? `REA tools update: ${note}` : "REA tools update: nothing relevant is installed in REA any more."}`;
}

// The engine-version note reaches sessions started before it existed (or before it changed) once.
function engineUpdate(session, note) {
  if (!note) return "";
  const key = `rea-engine:${session}`;
  let seen = null;
  try { seen = localStorage.getItem(key); localStorage.setItem(key, note); } catch {}
  return seen === note ? "" : `\n\n${note}`;
}

// The game's notes tagged "proven" (facts a test run or play test showed, with run ids): every new
// session reads them first instead of re-discovering them.
async function provenNotes(kbKey) {
  if (!kbKey) return "";
  try {
    const notes = ((await api(`/kb/${kbKey}`)).notes || []).filter((n) => (n.tags || []).includes("proven"));
    return notes.length ? `PROVEN FINDINGS for this game (read each one first with \`kb.py show ${kbKey} ID\`; don't re-test them or retry their dead ends; ` +
      `add new proven facts there with their run ids):\n${notes.map((n) => `- ${n.id}: ${n.title}`).join("\n")}\n` : "";
  } catch { return ""; }
}

async function tellRewriteAgent(message, stage, task = "build") {
  const rewrite = state.rewriteData;
  if (!rewrite) return;
  let kbKey = "";
  try { kbKey = (await api(`/kb/for/${rewrite.folder}`)).key; } catch {}
  const brief = `Rewrite project ${rewrite.id} at ${rewrite.projectPath}. Original game: ${rewrite.gameName} at ${rewrite.gamePath} (read-only). ` +
    `Target language: ${rewrite.language || "choose during planning"}; target engine: ${rewrite.engine || "choose during planning"}. ` +
    `Goal: ${rewrite.goal || "1:1 feature and behavior parity"}. ${kbKey ? `Game knowledge base key: ${kbKey}. Search it and record findings. ` : ""}` +
    `Use REA's analysis and decompiled code to inspect the original. Keep all new implementation in this project's src/ and tests in tests/. ` +
    `Write PLAN.md before implementation. Maintain PARITY.json as an array of {name,status,evidence} records, with status unverified, passed or failed. ` +
    `Cover launch, controls, rules, timing, saves, assets, UI, audio, levels, edge cases and platform behavior as applicable. ` +
    `Only mark passed after comparing the new build with the original and recording concrete evidence. Never claim 1:1 parity from compilation alone.\n` +
    `${await provenNotes(kbKey)}${await toolsNote()}\n\n`;
  const post = async (url, body) => {
    const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Object.assign(new Error(data.detail || `HTTP ${response.status}`), { status: response.status });
    return data;
  };
  try {
    let session = rewrite.session;
    if (session) {
      try { await post(`/Agent/api/sessions/${encodeURIComponent(session)}/message`, { text: message + await toolsUpdate(session), ...modelFor(task) }); }
      catch (error) { if (error.status !== 404) throw error; session = ""; }
    }
    if (!session) {
      session = (await post("/Agent/api/sessions", { prompt: brief + message, ...modelFor(task) })).id;
      toolsSeen(session, await toolsNote());
      await recordSession(kbKey, session, "rewrite", `Rewrite: ${rewrite.name}`, rewrite.id);
    }
    state.rewriteData = await api(`/mods/rewrites/${rewrite.id}/session`, { method: "POST", json: { session, stage } });
    refreshRewrite();
    toast("Sent to the Master Agent. Open the session to follow its work.");
  } catch (error) { toast(`Couldn't reach the Master Agent: ${error.message}`, "error"); }
}

$("rewrite-plan-button").addEventListener("click", () => tellRewriteAgent(`PLAN: Inspect the original game, map its architecture and observable behavior, write PLAN.md and an initial PARITY.json checklist. Stop for my approval. ${$("rewrite-guidance").value.trim()}`, "planning", "plan"));
$("rewrite-revise").addEventListener("click", () => {
  const guidance = $("rewrite-guidance").value.trim();
  if (!guidance) { $("rewrite-guidance").focus(); return; }
  tellRewriteAgent(`Revise PLAN.md and PARITY.json: ${guidance}. Stop for approval.`, "planning", "plan");
});
$("rewrite-approve").addEventListener("click", () => tellRewriteAgent("Plan approved. Implement the rewrite in src/, add comparison tests in tests/, build and run what the environment permits, then report specific parity gaps and evidence in PARITY.json.", "approved"));
$("rewrite-continue").addEventListener("click", () => tellRewriteAgent("Continue implementing and comparing this rewrite against the original. Run tests, fix failures, update PARITY.json with concrete evidence, and report remaining gaps.", "building"));
$("rewrite-send").addEventListener("click", () => {
  const feedback = $("rewrite-feedback").value.trim();
  if (!feedback) { $("rewrite-feedback").focus(); return; }
  $("rewrite-feedback").value = "";
  tellRewriteAgent(`Comparison feedback: ${feedback}. Investigate, fix the rewrite if needed, add or update a test, and update PARITY.json.`, "testing", "refine");
});
$("rewrite-back").addEventListener("click", () => { state.rewrite = null; clearTimeout(rewriteTimer); renderTarget(); setFolderTab("builds"); setBuildTab("rewrites"); });
$("rewrite-delete").addEventListener("click", async () => {
  if (!confirm("Delete this rewrite project and its source, plan and tests?")) return;
  try { await api(`/mods/rewrites/${state.rewrite}`, { method: "DELETE" }); state.rewrite = null; clearTimeout(rewriteTimer); renderTarget(); setFolderTab("builds"); setBuildTab("rewrites"); }
  catch (error) { toast(error.message, "error"); }
});

async function loadMods() {
  const target = current();
  if (!target || target.type !== "folder") return;
  // Clear the previous game's engine and mods at once, so nothing stale can be clicked.
  if ($("fv-mods").dataset.folder !== target.id) {
    $("fv-mods").dataset.folder = target.id;
    $("mods-support").className = "state";
    $("mods-support").textContent = "Checking which engine this game uses…";
    $("new-mod").hidden = true;
    $("mod-list").replaceChildren();
  }
  try {
    const [detected, list] = await Promise.all([api(`/mods/detect?folder=${target.id}`), api(`/mods?folder=${target.id}`)]);
    if (current()?.id !== target.id) return;
    const ok = detected.supported;
    const support = $("mods-support");
    support.className = `state${ok ? "" : " error"}`;
    const build = detected.platform ? ` · ${detected.platform === "linux-x64" ? "Linux build" : `Windows build (${detected.platform === "win-x86" ? "32-bit" : "64-bit"}, runs through Proton)`}` : "";
    support.textContent = ok ? `${detected.label}${detected.engineVersion ? ` ${detected.engineVersion}` : ""}${build}. ${detected.reason || "The guided mod pipeline works for this game."}` : `${detected.label}: ${detected.reason}`;
    $("new-mod").hidden = !ok;
    const installedIds = new Set((await loadInstalled()).map((m) => m.id));
    if (current()?.id !== target.id) return;
    $("mod-list").replaceChildren(...list.mods.map((mod) => {
      const item = el("li");
      const title = el("strong", "", mod.name);
      if (installedIds.has(mod.id)) title.append(" ", el("span", "badge works", "installed"));
      item.append(title, el("span", "hint", modStatus(mod)), readinessBar(mod.readiness));
      item.addEventListener("click", () => openMod(mod.id));
      return item;
    }));
  } catch (error) { toast(error.message, "error"); }
}

// Installed mods across every game (hostctl's install records), this game's first, each with Uninstall.
async function loadInstalled() {
  const list = $("installed-list"), target = current();
  let mods = [];
  try { mods = (await gw("/api/rea/mods/installed")).mods || []; }
  catch (error) { list.replaceChildren(el("li", "error", `Couldn't read installed mods: ${error.message}`)); $("uninstall-all").hidden = true; return []; }
  const here = (m) => target && m.gamePath === target.path;
  mods.sort((a, b) => here(b) - here(a));
  const stamp = (s) => s ? new Date(s * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "";
  list.replaceChildren(...mods.map((m) => {
    const item = el("li", here(m) ? "here" : "");
    const info = el("div", "inst-info");
    const title = el("strong", "", m.name);
    if (m.running?.length) title.append(" ", el("span", "badge live", "game running"));
    if (!m.project) title.append(" ", el("span", "badge partial", "project deleted"));
    info.append(title, el("span", "hint", [m.gameName, `${m.files} file${m.files === 1 ? "" : "s"}`, stamp(m.installed_at) && `installed ${stamp(m.installed_at)}`].filter(Boolean).join(" · ")));
    const actions = el("div", "inst-actions");
    if (m.project && here(m)) {
      const open = el("button", "button small", "Open");
      open.type = "button";
      open.addEventListener("click", () => openMod(m.id));
      actions.append(open);
    }
    const remove = el("button", "button danger small", "Uninstall");
    remove.type = "button";
    remove.disabled = !!m.running?.length || m.missing;
    if (m.running?.length) remove.title = "Close the game first";
    if (m.missing) remove.title = "The game folder is gone";
    remove.addEventListener("click", () => uninstallMods([m]));
    actions.append(remove);
    item.append(info, actions);
    return item;
  }));
  const local = mods.filter(here);
  const all = $("uninstall-all");
  all.hidden = local.length < 2;
  all.disabled = local.some((m) => m.running?.length);
  all.onclick = () => uninstallMods(local);
  return mods;
}

async function uninstallMods(mods) {
  const names = mods.map((m) => m.name).join(", ");
  if (!confirm(`Uninstall ${names} from ${mods[0].gameName}? Files the mod${mods.length > 1 ? "s" : ""} replaced are restored.`)) return;
  for (const m of mods) {
    try {
      const result = await gw(`/api/rea/mods/${m.id}/uninstall`, { method: "POST", json: {} });
      toast(result.message || `Removed ${m.name}`);
      if (state.mod === m.id) state.install = result;
    } catch (error) { toast(`${m.name}: ${error.message}`, "error"); }
  }
  loadMods();
}

function modStatus(mod) {
  const s = mod.steps || {};
  return [s.decompile?.state === "partial" || (mod.readiness?.failedConversions || 0) + (mod.readiness?.missedScripts || 0) > 0 ? "recovery partial" : s.decompile?.state === "done" ? "recovered" : "not recovered", mod.stage === "approved" || mod.stage === "refining" ? "plan approved" : mod.stage === "planning" ? "planning" : "",
    s.build?.state === "done" ? "built" : s.build?.state === "failed" ? "build failed" : ""].filter(Boolean).join(" · ");
}

function readinessBar(readiness) {
  const box = el("div", "readiness-inline");
  const value = readiness?.percent ?? 0;
  const bar = el("progress");
  bar.max = 100;
  bar.value = value;
  bar.setAttribute("aria-label", `Build readiness ${value}%`);
  box.append(el("span", "", `${value}% · ${readiness?.label || "No build target linked"}`), bar);
  return box;
}

$("new-mod").addEventListener("submit", async (event) => {
  event.preventDefault();
  const target = current();
  if (!target || $("mod-create").disabled) return;
  const name = $("mod-name").value.trim();
  const goal = $("mod-goal").value.trim();
  if (!name || !goal) { toast("Add a name and describe what the mod should do", "error"); return; }
  $("mod-create").disabled = true;
  try {
    const mod = await api("/mods", { method: "POST", json: { folder: target.id, name, goal, recipe: "blank" } });
    $("new-mod").reset();
    openMod(mod.id);
  } catch (error) { toast(error.message, "error"); }
  finally { $("mod-create").disabled = false; }
});

let modTimer;
async function openMod(id) {
  loadProjectBrowser("mod", id);
  setTopTab("work");
  if (state.mod !== id) { $("mod-arch-editor").hidden = true; $("mod-arch-view").hidden = false; setProjectTab("mod", savedProjectTab("mod")); }
  state.mod = id;
  state.modData = null;
  state.install = null;
  modCheck.hide();
  modCheck.load().catch((error) => console.warn(error));
  // Blank the previous mod's view until this one loads.
  for (const el2 of ["mod-title", "mod-meta", "mod-goal-text", "extract-title", "extract-hint", "build-hint", "plan-text", "build-log", "launch-hint"]) $(el2).textContent = "";
  $("mod-title").textContent = "Loading…";
  $("loader-box").hidden = true;
  $("code-view").hidden = true;
  $("code-hits").replaceChildren();
  renderTarget();
  await refreshMod(true);
}

async function refreshMod(withInstall = false) {
  clearTimeout(modTimer);
  const id = state.mod;
  if (!id) return;
  try {
    const [mod, install] = await Promise.all([api(`/mods/${id}`), withInstall ? gw(`/api/rea/mods/${id}/status`).catch((e) => ({ error: e.message })) : null]);
    if (state.mod !== id) return;
    const previousDecompile = state.modData?.steps?.decompile?.finished;
    state.modData = mod;
    if (install) state.install = install;
    renderMod();
    if (mod.steps?.decompile?.finished && mod.steps.decompile.finished !== previousDecompile) loadInventory();
  } catch (error) { toast(error.message, "error"); }
  if (state.mod === id) modTimer = setTimeout(() => refreshMod(++refreshMod.n % 5 === 0), state.modData?.running ? 2000 : 5000);
}
refreshMod.n = 0;

function pill(id, text, kind) { const p = $(id); p.textContent = text; p.className = `pill ${kind || ""}`; }
function stepState(name, done, active) {
  const li = document.querySelector(`#mod-view .step[data-step="${name}"]`);
  li.classList.toggle("done", Boolean(done));
  li.classList.toggle("active", Boolean(active) && !done);
}

function renderMod() {
  const m = state.modData;
  if (!m) return;
  const s = m.steps || {};
  $("mod-title").textContent = m.name;
  $("mod-meta").textContent = [m.gameName, m.engineVersion ? `${m.label} ${m.engineVersion}` : m.label, languageNames(state.inv?.languages).join(", "), m.platform,
    m.recipe === "blank" ? "Custom mod" : `${m.recipe} recipe`, m.projectPath].filter(Boolean).join(" · ");
  const ad = m.adapter || {};
  $("extract-title").textContent = ad.extractLabel || "Decompile the game";
  $("extract-hint").textContent = ad.extractHint || "";
  $("build-hint").textContent = ad.buildHint || "";
  $("loader-box").hidden = !ad.needsLoader;
  $("loader-hint").textContent = ad.loaderHint || "";
  const inst0 = state.install || {};
  pill("st-loader", m.running === "loader" ? "Preparing…" : m.ready && m.engine === "unity-il2cpp" ? "Interop generated: ready to decompile"
    : inst0.installed ? "Loader installed: start the game once" : m.loaderBuilt ? "Ready to install" : "", m.ready && m.engine === "unity-il2cpp" ? "ok" : "run");
  $("do-loader").disabled = Boolean(m.running);
  $("do-loader-install").hidden = !m.loaderBuilt || inst0.installed;
  $("mod-goal-text").textContent = m.goal || "(no goal written)";
  const readiness = m.readiness || { percent: 0, label: "Build unverified", issues: [] };
  $("mod-readiness-label").textContent = `${readiness.percent}% · ${readiness.label}`;
  $("mod-readiness-bar").value = readiness.percent;
  $("mod-readiness-issues").replaceChildren(...readiness.issues.map((issue) => el("li", "", issue)));
  $("mod-readiness-box").hidden = !readiness.issues.length;
  $("mod-readiness-meta").textContent = readiness.issues.length ? `${readiness.issues.length} · ${readiness.issues[0]}` : "";
  $("mod-session").hidden = !m.session;
  if (m.session) $("mod-session").href = `/Agent/?s=${encodeURIComponent(m.session)}`;

  const dec = s.decompile || {};
  const partial = dec.state === "partial" || (dec.state === "done" && ((readiness.failedConversions || 0) + (readiness.missedScripts || 0) > 0));
  pill("st-decompile", m.running === "decompile" ? "Decompiling…" : partial ? `Partial (${dec.files || 0} files); inspect recovery report` : dec.state === "done" ? `${dec.files || 0} files recovered` : dec.state === "failed" ? `Failed: ${dec.error}` : "Not yet",
    m.running === "decompile" ? "run" : partial ? "bad" : dec.state === "done" ? "ok" : dec.state === "failed" ? "bad" : "");
  $("do-decompile").textContent = ["done", "partial"].includes(dec.state) ? "Run again" : (ad.extractLabel || "Decompile").split(" ")[0];
  $("do-decompile").disabled = Boolean(m.running) || !m.ready;
  $("do-decompile").title = m.ready ? "" : "Do the loader step first";
  $("code-search").hidden = !["done", "partial"].includes(dec.state);
  stepState("decompile", dec.state === "done" && !partial, !dec.state || partial);

  const planned = Boolean(m.plan.trim());
  const approved = m.stage === "approved" || m.stage === "refining";
  pill("st-plan", approved ? "Approved" : planned ? "Waiting for your approval" : m.stage === "planning" ? "The agent is planning…" : "Not yet",
    approved ? "ok" : planned ? "run" : m.stage === "planning" ? "run" : "");
  $("plan-text").hidden = !planned;
  $("plan-text").textContent = m.plan;
  const relevant = m.relevant || [];
  $("relevant-box").hidden = !relevant.length;
  $("relevant-files").replaceChildren(...relevant.map((f) => {
    const item = el("li", f.exists ? "" : "missing");
    item.append(el("span", "val", f.why || ""), el("span", "addr", f.exists ? f.path.replace(/^decompiled\//, "") : `${f.path.replace(/^decompiled\//, "")} (not found)`));
    if (f.exists) item.addEventListener("click", () => showProjectFile(f.path, 0, $("relevant-files")));
    return item;
  }));
  $("do-plan").hidden = planned;
  $("do-plan").disabled = !["done", "partial"].includes(dec.state);
  $("do-plan").title = !["done", "partial"].includes(dec.state) ? "Recover the game first" : "";
  $("do-revise").hidden = !planned;
  $("do-approve").hidden = !planned || approved;
  stepState("plan", approved, ["done", "partial"].includes(dec.state));

  const b = s.build || {};
  pill("st-build", m.running === "build" ? "Building…" : b.state === "done" ? "Built" : b.state === "failed" ? "Build failed" : "Not yet",
    m.running === "build" ? "run" : b.state === "done" ? "ok" : b.state === "failed" ? "bad" : "");
  $("do-build").disabled = Boolean(m.running) || !m.ready;
  setLog($("build-log"), m.buildLog || "(no build yet)", m.running === "build");
  const lines = (m.buildLog || "").trimEnd().split("\n").filter(Boolean);
  $("build-log-meta").textContent = lines.length ? `${lines.length} line${lines.length === 1 ? "" : "s"} · ${lines.at(-1)}` : "";
  $("src-files-box").hidden = !m.src.length;
  $("src-files-meta").textContent = `${m.src.length} file${m.src.length === 1 ? "" : "s"}`;
  $("src-files").replaceChildren(...m.src.map((f) => {
    const a = el("a", "", f.replace(/^src\//, ""));
    a.addEventListener("click", () => showProjectFile(f));
    return a;
  }));
  stepState("build", b.state === "done", approved);
  $("testrun-support").hidden = !m.headless;
  $("testrun-support").textContent = m.headless || "";
  $("do-testrun").disabled = Boolean(m.running);
  if (m.running === "testrun" || !$("testrun-status").childElementCount || state.testRunMod !== m.id) { state.testRunMod = m.id; loadTestRun(); }

  const inst = state.install || {};
  const stale = inst.installed && b.finished && b.finished > (inst.installed_at || 0) * 1000;
  pill("st-install", inst.error ? inst.error : inst.installed ? (stale ? "Installed (older build: reinstall)" : "Installed") : "Not installed",
    inst.error ? "bad" : inst.installed ? (stale ? "run" : "ok") : "");
  $("do-install").textContent = inst.installed ? "Reinstall" : "Install";
  $("do-install").disabled = !m.built || Boolean(m.running);
  if (!inst.launch && m.launch) inst.launch = m.launch;
  $("do-install").title = m.built ? "" : "Build the mod first";
  $("do-uninstall").hidden = !inst.installed;
  const gameUp = Boolean(inst.running?.length);
  $("do-play").disabled = Boolean(m.running) || gameUp;
  $("do-play").title = gameUp ? "The game is running" : m.running ? "Wait for the current REA job" : "";
  if (gameUp) $("play-hint").textContent = "The game is running.";
  else if ($("play-hint").textContent === "The game is running.") $("play-hint").textContent = "";
  $("launch-hint").hidden = !inst.launch;
  $("launch-hint").textContent = (inst.launch || "") + (inst.others?.length ? `\n\nOther REA mods installed in this game: ${inst.others.length}.` : "");
  stepState("install", inst.installed && !stale, b.state === "done");

  $("mod-arch-meta").textContent = m.architectureUpdated ? `ARCHITECTURE.md · updated ${when(m.architectureUpdated)}` : "Not written yet";
  if ($("mod-arch-editor").hidden) $("mod-arch-view").replaceChildren(markdown(m.architecture || "_Not written yet. The agent fills it in while planning and building._"));
  renderGoals("mod", m.goals);

  $("game-log-box").hidden = m.gameLog === null;
  $("game-log").textContent = m.gameLog || "";
  pill("st-refine", readiness.verified ? "Play test passed" : inst.installed && !stale ? "Ready to play" : "", readiness.verified ? "ok" : "run");
  stepState("refine", readiness.verified, inst.installed && !stale);
  $("mod-tab-issues").textContent = readiness.issues.length ? String(readiness.issues.length) : "";
  $("mod-tab-issues").title = readiness.issues.length ? `${readiness.issues.length} readiness issue(s)` : "";
  renderStrip("mod", [...document.querySelectorAll("#mod-view .step[data-step]")]);
}

async function showProjectFile(rel, line, anchor) {
  const pre = $("code-view");
  pre.hidden = false;
  pre.textContent = "Loading…";
  (anchor || (rel.startsWith("src/") ? $("src-files") : $("code-hits"))).after(pre);
  try {
    const { text } = await api(`/mods/${state.mod}/file?path=${encodeURIComponent(rel)}`);
    pre.replaceChildren(el("div", "hint", rel));
    const lines = text.split("\n");
    lines.forEach((content, i) => {
      const row = el("span", i + 1 === line ? "hl" : "");
      row.append(highlight(content), "\n");
      pre.append(row);
    });
    const hit = pre.querySelector(".hl");
    if (hit) pre.scrollTop = hit.offsetTop - pre.clientHeight / 3;
    else pre.scrollTop = 0;
  } catch (error) { pre.textContent = error.message; }
}

let searchTimer;
$("code-q").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    const q = $("code-q").value.trim();
    if (q.length < 2) { $("code-hits").replaceChildren(); return; }
    try {
      const { hits, truncated } = await api(`/mods/${state.mod}/search?q=${encodeURIComponent(q)}`);
      const rows = hits.map((h) => {
        const item = el("li");
        item.append(el("span", "val", h.text), el("span", "addr", `${h.path.replace(/^decompiled\//, "")}:${h.line}`));
        item.addEventListener("click", () => showProjectFile(h.path, h.line));
        return item;
      });
      if (!rows.length) rows.push(el("li", "", "No matches."));
      if (truncated) rows.push(el("li", "more", "First 300 matches shown"));
      $("code-hits").replaceChildren(...rows);
    } catch (error) { $("code-hits").replaceChildren(el("li", "error", error.message)); }
  }, 300);
});

async function modJob(kind) {
  try { state.modData = await api(`/mods/${state.mod}/${kind}`, { method: "POST", json: {} }); renderMod(); refreshMod(); }
  catch (error) { toast(error.message, "error"); }
}
$("do-decompile").addEventListener("click", () => modJob("decompile"));
$("do-loader").addEventListener("click", () => modJob("loader"));
$("do-loader-install").addEventListener("click", () => installAction("install"));
$("do-build").addEventListener("click", () => modJob("build"));

// One Master Agent session per mod: the first message carries the brief, later ones are steps.
async function tellAgent(text, stage, task = stage === "approved" ? "build" : stage === "refining" ? "refine" : "plan") {
  const m = state.modData;
  let kbKey = "";
  try { kbKey = (await api(`/kb/for/${m.folder}`)).key; } catch {}
  const brief = `You are helping me mod a game with REA (Rocsy). Use the skills rocsy/rea-modding and rocsy/rea-knowledge: read ` +
    `/opt/data/skills/rocsy/rea-modding/SKILL.md and /opt/data/skills/rocsy/rea-knowledge/SKILL.md first and follow their rules.\n` +
    (kbKey ? `Knowledge base KEY for this game: ${kbKey} (search it before you start, write what you learn; check \`kb.py decomp ${kbKey}\` before decompiling anything and record what you decompile).\n` : "") +
    await provenNotes(kbKey) +
    `Mod project ID ${m.id} at /opt/data/rocsy-workspace/rea-mods/${m.id}/ — game "${m.gameName}" (${m.label || "Unity (Mono)"}${m.engineVersion ? ` ${m.engineVersion}` : ""}${m.platform ? `, ${m.platform}` : ""}; engine adapter "${m.engine}"), ` +
    `starting recipe: ${m.recipe}.\nGoal: ${m.goal || "(see GOAL.md)"}\n` +
    (m.engineBrief ? `${m.engineBrief}\n` : "") +
    `Current build readiness: ${m.readiness?.percent || 0}%. Unresolved: ${(m.readiness?.issues || []).join(" | ") || "none recorded"}. ` +
    `Read GET /mods/${m.id} and resolve its readiness issues. A successful decompile or package is not a running-game test.\n` +
    `Two architecture files: the game's ARCHITECTURE.md (kb.py arch / set-arch) is ONLY about the game itself, from its decompiled files ` +
    `(engine, modules, core systems, data, hook points); this mod's design (its systems, src/ files, which game hooks it uses, how they connect) ` +
    `goes in the mod's own /opt/data/rocsy-workspace/rea-mods/${m.id}/ARCHITECTURE.md (kb.py mod-arch ${m.id} / set-mod-arch ${m.id}). Never put mod plans in the game's file.\n` +
    `Saved functions: before writing loader, hook or patch code, check the reusable code saved from other ${m.label || m.engine} games ` +
    `(kb.py funcs ${m.engine} "what you need") and start from it. When a piece of reusable structural code works here (mod loading/injection, ` +
    `hook or patch helpers, UI scaffolding, build tricks; proven by a passing test run or the user's play test), save it with kb.py func-save ` +
    `(generic, without this mod's game-specific content) or mark the saved one you reused as working.\n` +
    `How to mod: change THIS game, don't build a different one inside it. Use the game's own scenes, scripts, systems, UI, ` +
    `models, textures, sounds, items and mechanics (find them in decompiled/ and the knowledge base) and edit or extend them; ` +
    `make new assets or code only for mechanics, UI or content the game doesn't have, in the game's style. Edit or extend the ` +
    `game's scripts and singletons (copy the original into src/files, or extends "res://…") instead of rewriting them from ` +
    `scratch, so the rest of the game keeps working. Even a total conversion runs in the game's world with its player, camera, ` +
    `controls and art; a UI-only game made of new screens is not a mod. PLAN.md names the existing game files and assets each feature builds on.\n` +
    `${proofRule(m)}\n` +
    `${await toolsNote(m.engine)}\n\n`;
  // After planning, every step starts from the files the plan picked.
  if (stage !== "planning" && m.relevantBrief) text += `\n${m.relevantBrief}`;
  const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then(async (r) => { const d = await r.json().catch(() => ({})); if (!r.ok) throw Object.assign(new Error(d.detail || `HTTP ${r.status}`), { status: r.status }); return d; });
  try {
    let session = m.session;
    if (session) {
      try { await post(`/Agent/api/sessions/${encodeURIComponent(session)}/message`, { text: text + engineUpdate(session, m.engineBrief) + await toolsUpdate(session, m.engine), ...modelFor(task) }); }
      catch (error) { if (error.status !== 404) throw error; session = ""; }
    }
    if (!session) {
      session = (await post("/Agent/api/sessions", { prompt: brief + text, ...modelFor(task) })).id;
      toolsSeen(session, await toolsNote(m.engine));
      engineUpdate(session, m.engineBrief);
      await recordSession(kbKey, session, "mod", `Mod: ${m.name}`, m.id);
    }
    state.modData = await api(`/mods/${m.id}/session`, { method: "POST", json: { session, stage } });
    renderMod();
    toast("Sent to the Master Agent. Open the agent session to watch it work.");
  } catch (error) { toast(`Couldn't reach the Master Agent: ${error.message}`, "error"); }
}

const guidance = () => { const g = $("plan-guidance").value.trim(); $("plan-guidance").value = ""; return g; };
$("do-plan").addEventListener("click", () => {
  const g = guidance();
  tellAgent(`Step: PLAN. Study the decompiled code and the game's assets for this goal, write PLAN.md and NOTES.md, then stop and wait for my approval. ` +
    `For every feature, list the game's own scenes, scripts, assets and mechanics it reuses or changes, and only then what is new, and the [PROOF] line that will show it worked. ` +
    `Also decide which decompiled files matter most for this mod and write them to RELEVANT.md, most important first (usually 5-25): one line per file, ` +
    "\"- `decompiled/<path>` — what it holds and what the mod reads, hooks or patches there (class/method names)\". " +
    `Use real paths from decompiled/ (check each exists); skip engine/SDK libraries and files that are only background. Later steps read these first.${g ? `\nMy guidance: ${g}` : ""}`, "planning");
});
$("do-revise").addEventListener("click", () => {
  const g = guidance();
  if (!g) { toast("Write what should change in the plan first"); $("plan-guidance").focus(); return; }
  tellAgent(`Change PLAN.md: ${g}\nUpdate RELEVANT.md if the files the mod touches change. Then stop and wait for my approval again.`, "planning");
});
$("do-approve").addEventListener("click", () => {
  const g = guidance();
  tellAgent(`Plan approved.${g ? ` Also: ${g}` : ""} Step: BUILD. Implement it in src/ on top of the game's own files and assets, build with the REA API and fix errors until it builds, ` +
    `adding each feature's [PROOF] line, then run a Test run with \`$R reamod test ${state.modData.id}\` (3 screenshots in chat). If you may play-test on the real game (\`$R reamod play ${state.modData.id}\`, see your permissions), do that too and check its log; otherwise tell me exactly what to test. ` +
    `Finish with the list of milestones and which [PROOF] lines each run's log shows.`, "approved");
});
$("do-fix-readiness").addEventListener("click", () => {
  const m = state.modData;
  const issues = m.readiness?.issues || [];
  tellAgent(`Step: RESOLVE BUILD READINESS. Current REA checks report:\n${issues.map((x) => `- ${x}`).join("\n") || "- Review the current build and play-test evidence."}\nRead the recovery report and all draft files, integrate actual source under src/files/, fix static and build errors, and run the build through REA. Recheck GET /mods/${m.id} after each fix and keep the [PROOF] lines intact. A decompile or packaged pack is not proof the game runs. If runtime testing is unavailable, leave readiness unverified and state exactly what test remains.`, "refining", "refine");
});
$("do-refine").addEventListener("click", () => {
  const text = $("refine-text").value.trim();
  if (!text) { $("refine-text").focus(); return; }
  $("refine-text").value = "";
  tellAgent(`I played it with the mod installed. ${text}\nStep: REFINE. Check gameLog (GET /mods/${state.modData.id}) for errors from the mod and for which [PROOF] lines appeared (missing ones show where it stopped), change src/, rebuild, and tell me what changed and what to test. I'll reinstall.`, "refining");
});

// Shared with testrun.mjs askAgent(): milestones must leave a greppable line in the game's log.
function proofRule(m) {
  const tag = m.className || m.name || m.id;
  return `Proof lines: every milestone of the goal (mod loaded, each feature's entry point reached, each feature actually doing its job, e.g. level loaded, 12 agents spawned, round 3 started, item bought) writes ONE log line "[PROOF ${tag}] <milestone>: <concrete values>" through the engine's log (BepInEx Logger.LogInfo or Debug.Log, Godot print(), JS console.log, Lua print(), Ren'Py renpy.log). Write it where the effect happens (after the spawn returned, inside the patched method, when the state changed), never up front at startup, and never for something that didn't happen. PLAN.md lists each feature's proof line. After every test run or play test, grep its logs (game.log, run.log, player log) for "[PROOF ${tag}]" and report per milestone: seen (quote the line) or missing. A feature without its proof line in a passing run is unproven: say so, never claim it works.`;
}

// "Focus the agent" boxes above Project files: mod- or rewrite-only work, or an addition to it.
function focusBox(prefix, send) {
  const go = () => {
    const text = $(`${prefix}-focus-text`).value.trim();
    const add = $(`${prefix}-focus-kind`).value === "add";
    if (add && !text) { toast("Write what to add first"); $(`${prefix}-focus-text`).focus(); return; }
    $(`${prefix}-focus-text`).value = "";
    send(text, add);
  };
  $(`${prefix}-focus-send`).addEventListener("click", go);
  $(`${prefix}-focus-text`).addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); go(); } });
}
focusBox("mod", (text, add) => {
  const m = state.modData;
  const scope = `Work ONLY on mod project ${m.id} ("${m.name}"): change files under its src/files/ (patch the game's own files and assets; never edit the recovered copy), ` +
    `build through the REA API and fix errors until it builds, then run a Test run. Don't touch other mods, rewrites or unrelated tasks.`;
  tellAgent(add
    ? `Step: ADD TO THE MOD. Add this to the existing mod, keeping everything it already does: ${text}\nNote the addition in the mod architecture. ${scope}`
    : `Step: FOCUS ON THE MOD. Make this mod toward its goal${text ? `, concentrating on: ${text}` : ""}. ${scope}`, add ? "refining" : "approved");
});
focusBox("rewrite", (text, add) => {
  const r = state.rewriteData;
  const scope = `Work ONLY on rewrite project ${r.id} ("${r.name}"): implementation in its src/, tests in tests/, evidence in PARITY.json. Don't touch mods or other projects.`;
  tellRewriteAgent(add
    ? `ADD TO THE REWRITE: ${text}\nKeep existing behavior, add or update a test for it, build and run the tests. ${scope}`
    : `FOCUS ON THE REWRITE: continue implementing it${text ? `, concentrating on: ${text}` : ""}. Run the tests and fix failures. ${scope}`, "building");
});

async function recordPlayTest(result) {
  const evidence = $("verify-evidence").value.trim();
  try {
    state.modData = await api(`/mods/${state.mod}/verify`, { method: "POST", json: { state: result, evidence } });
    $("verify-evidence").value = "";
    renderMod();
    toast(result === "passed" ? "Current build marked as play-tested" : "Failure recorded for the agent");
  } catch (error) { toast(error.message, "error"); }
}
$("verify-pass").addEventListener("click", () => recordPlayTest("passed"));
$("verify-fail").addEventListener("click", () => recordPlayTest("failed"));

async function installAction(action) {
  const m = state.modData;
  if (action === "uninstall" && !confirm(`Remove ${m.name} from the game? Files it replaced are restored.`)) return;
  if (action === "install" && !state.install?.installed && !confirm(`Install ${m.name} into ${m.gameName}?\n\nBepInEx and the mod are copied into the game folder; anything replaced is backed up and Uninstall puts the game back. Close the game first.`)) return;
  try {
    const result = await gw(`/api/rea/mods/${m.id}/${action}`, { method: "POST", json: {} });
    state.install = result;
    toast(result.message || "Done");
    renderMod();
  } catch (error) { toast(error.message, "error"); }
}
$("do-install").addEventListener("click", () => installAction("install"));
// ▶ Play: installs the current build if it isn't (or an older one is), then starts the game through Steam.
$("do-play").addEventListener("click", async () => {
  const m = state.modData, inst = state.install || {}, b = m.steps?.build || {};
  const stale = inst.installed && b.finished && b.finished > (inst.installed_at || 0) * 1000;
  if (inst.running?.length) { toast("The game is already running"); return; }
  const button = $("do-play");
  button.disabled = true;
  try {
    if ((!inst.installed || stale) && m.built) {
      if (!confirm(`${stale ? "Reinstall the latest build of" : "Install"} ${m.name} into ${m.gameName} and start the game?\n\nAnything replaced is backed up; Uninstall puts the game back.`)) return;
      state.install = await gw(`/api/rea/mods/${m.id}/install`, { method: "POST", json: {} });
    } else if (!inst.installed && !confirm(`${m.name} isn't built or installed. Start the unmodded game?`)) return;
    $("play-hint").textContent = "Starting through Steam and checking the mod loader…";
    const result = await gw(`/api/rea/mods/${m.id}/launch`, { method: "POST", json: {} });
    toast(result.message || "Starting the game", result.loader === null ? "error" : undefined);
    $("play-hint").textContent = result.loader === null ? "" : "Play, then describe what to change below.";
    setTimeout(() => refreshMod(true), 5000);
  } catch (error) { $("play-hint").textContent = ""; toast(error.message, "error"); refreshMod(true); }
  finally { button.disabled = false; renderMod(); }
});
$("do-uninstall").addEventListener("click", () => installAction("uninstall"));

$("mod-arch-edit").addEventListener("click", () => {
  $("mod-arch-text").value = state.modData?.architecture || "";
  $("mod-arch-view").hidden = true;
  $("mod-arch-editor").hidden = false;
  setProjectTab("mod", "arch");
});
$("mod-arch-cancel").addEventListener("click", () => { $("mod-arch-editor").hidden = true; $("mod-arch-view").hidden = false; renderMod(); });
$("mod-arch-save").addEventListener("click", async () => {
  try {
    await api(`/mods/${state.mod}/architecture`, { method: "PUT", json: { text: $("mod-arch-text").value } });
    $("mod-arch-editor").hidden = true;
    $("mod-arch-view").hidden = false;
    refreshMod();
  } catch (error) { toast(error.message, "error"); }
});

$("mod-back").addEventListener("click", () => { state.mod = null; clearTimeout(modTimer); renderTarget(); setFolderTab("builds"); });
$("mod-delete").addEventListener("click", async () => {
  if (!confirm("Delete this mod project (its code and plan)? Uninstall it from the game first.")) return;
  try { await api(`/mods/${state.mod}`, { method: "DELETE" }); state.mod = null; renderTarget(); setFolderTab("builds"); }
  catch (error) { toast(error.message, "error"); }
});

// ------------------------------------------------------------------ knowledge base --
// Per-game Notes + ARCHITECTURE.md (kb.mjs). The agent writes them through the rea-knowledge
// skill; the search box here is the same retrieval the agent uses.

// Minimal Markdown → DOM (no innerHTML: notes are written by the agent).
function inline(text) {
  const frag = document.createDocumentFragment();
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\[([^\]]+)\]\((https?:\/\/[^)\s]+)\))/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index > last) frag.append(text.slice(last, m.index));
    if (m[1]) frag.append(el("code", "", m[1].slice(1, -1)));
    else if (m[2]) frag.append(el("strong", "", m[2].slice(2, -2)));
    else { const a = el("a", "", m[4]); a.href = m[5]; a.target = "_blank"; a.rel = "noopener noreferrer"; frag.append(a); }
    last = m.index + m[0].length;
  }
  frag.append(text.slice(last));
  return frag;
}

function markdown(text) {
  const root = document.createDocumentFragment();
  const lines = String(text || "").replace(/^---\n[\s\S]*?\n---\n?/, "").split("\n");
  let list = null, para = [];
  const flushPara = () => { if (para.length) { const p = el("p"); p.append(inline(para.join(" "))); root.append(p); para = []; } };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith("```")) {
      flushPara(); list = null;
      const code = [];
      while (++i < lines.length && !lines[i].startsWith("```")) code.push(lines[i]);
      const pre = el("pre"); pre.append(el("code", "", code.join("\n"))); root.append(pre);
      continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)/);
    if (h) { flushPara(); list = null; const node = el(`h${h[1].length}`); node.append(inline(h[2])); root.append(node); continue; }
    const li = line.match(/^\s*([-*]|\d+\.)\s+(.*)/);
    if (li) {
      flushPara();
      const tag = /\d/.test(li[1]) ? "ol" : "ul";
      if (!list || list.tagName.toLowerCase() !== tag) { list = el(tag); root.append(list); }
      const item = el("li"); item.append(inline(li[2])); list.append(item);
      continue;
    }
    if (!line.trim()) { flushPara(); list = null; continue; }
    list = null;
    para.push(line.trim());
  }
  flushPara();
  return root;
}

const when = (ms) => ms ? new Date(ms).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "";
let kbNote = null;

async function loadKb() {
  const target = current();
  if (!target || target.type !== "folder") return;
  try {
    const { key, path: dir } = await api(`/kb/for/${target.id}`);
    const data = await api(`/kb/${key}`);
    if (current()?.id !== target.id) return;
    state.kb = { ...data, key, path: target.path, dir };
    renderKb();
  } catch (error) { toast(error.message, "error"); }
}

function renderKb() {
  const kb = state.kb;
  if (!kb) return;
  $("kb-list").replaceChildren(...(kb.notes.length ? kb.notes.map((n) => {
    const item = el("li");
    const row = el("div", "kb-row");
    row.append(el("strong", "", n.title));
    if (n.kind === "session") row.append(el("span", "badge session", "session"));
    row.append(el("span", `badge ${n.author === "user" ? "user" : ""}`, n.author === "user" ? "you" : "agent"));
    for (const t of n.tags) row.append(el("span", "badge", t));
    item.append(row, el("span", "hint", `${when(n.updated)} · ${n.preview}`));
    item.addEventListener("click", () => openNote(n.id));
    return item;
  }) : [el("li", "empty", "No notes yet. The Master Agent adds them as it works; you can add your own.")]));
  $("arch-meta").textContent = `ARCHITECTURE.md · updated ${when(kb.architectureUpdated)} · ${kb.dir}`;
  if ($("arch-editor").hidden) $("arch-view").replaceChildren(markdown(kb.architecture));
  const mods = kb.modArchitectures || [];
  $("arch-mods").hidden = !mods.length;
  $("arch-mod-list").replaceChildren(...mods.map((x) => {
    const item = el("li");
    const row = el("div", "kb-row");
    row.append(el("strong", "", x.name), el("span", "badge mod-architecture", "mod"));
    item.append(row, el("span", "hint", `updated ${when(x.updated)} · ${x.path}`));
    item.addEventListener("click", () => { openMod(x.mod); setProjectTab("mod", "arch"); });
    return item;
  }));
}

async function openNote(id) {
  try {
    kbNote = await api(`/kb/${state.kb.key}/notes/${id}`);
    kbNote.id = id;
    $("kb-note-title").textContent = kbNote.title;
    $("kb-note-meta").textContent = [kbNote.kind === "session" ? "session note" : "note", kbNote.author === "user" ? "by you" : "by the agent",
      kbNote.tags.join(", "), `updated ${when(kbNote.updated)}`].filter(Boolean).join(" · ");
    $("kb-note-body").replaceChildren(markdown(kbNote.body));
    $("kb-note").hidden = false;
    $("kb-editor").hidden = true;
    $("kb-note").scrollIntoView({ block: "nearest" });
  } catch (error) { toast(error.message, "error"); }
}

function editNote(note) {
  kbNote = note;
  $("kb-title").value = note?.title || "";
  $("kb-tags").value = note?.tags?.join(", ") || "";
  $("kb-body").value = note?.body || "";
  $("kb-note").hidden = true;
  $("kb-editor").hidden = false;
  $("kb-title").focus();
}
$("kb-new").addEventListener("click", () => editNote(null));
$("kb-edit").addEventListener("click", () => editNote(kbNote));
$("kb-close").addEventListener("click", () => { $("kb-note").hidden = true; kbNote = null; });
$("kb-cancel").addEventListener("click", () => { $("kb-editor").hidden = true; if (kbNote) $("kb-note").hidden = false; });
$("kb-save").addEventListener("click", async () => {
  const body = { title: $("kb-title").value, tags: $("kb-tags").value, body: $("kb-body").value };
  try {
    const saved = kbNote ? await api(`/kb/${state.kb.key}/notes/${kbNote.id}`, { method: "PUT", json: body })
      : await api(`/kb/${state.kb.key}/notes`, { method: "POST", json: { ...body, author: "user" } });
    $("kb-editor").hidden = true;
    await loadKb();
    openNote(saved.id);
  } catch (error) { toast(error.message, "error"); }
});
$("kb-delete").addEventListener("click", async () => {
  if (!kbNote || !confirm(`Delete the note "${kbNote.title}"?`)) return;
  try { await api(`/kb/${state.kb.key}/notes/${kbNote.id}`, { method: "DELETE" }); $("kb-note").hidden = true; kbNote = null; loadKb(); }
  catch (error) { toast(error.message, "error"); }
});

let kbSearchTimer;
async function kbSearch() {
  const q = $("kb-q").value.trim();
  const box = $("kb-results");
  if (q.length < 2 || !state.kb) { box.hidden = true; return; }
  const scope = [...document.querySelectorAll("#kb-scope button[aria-pressed=true]")].map((b) => b.dataset.scope).join(",");
  try {
    const { results } = await api(`/kb/${state.kb.key}/search?k=10&scope=${scope}&q=${encodeURIComponent(q)}`);
    box.hidden = false;
    box.replaceChildren(...(results.length ? results.map((r) => {
      const item = el("li");
      const row = el("div", "kb-row");
      row.append(el("span", `badge ${r.kind}`, r.kind === "code" ? "code" : r.kind), el("strong", "", r.title),
        el("span", "hint", `${r.source}${r.line ? `:${r.line}` : ""} · score ${r.score}`));
      item.append(row, el("pre", "", r.text));
      const noteId = r.source.match(/^notes\/(.+)\.md$/)?.[1];
      const modId = r.kind === "mod-architecture" ? r.source.match(/^rea-mods\/([0-9a-f]{12})\//)?.[1] : null;
      item.addEventListener("click", () => noteId ? openNote(noteId) : r.kind === "architecture" ? setFolderTab("arch")
        : modId ? (openMod(modId), setProjectTab("mod", "arch")) : null);
      return item;
    }) : [el("li", "empty", "Nothing found.")]));
  } catch (error) { box.hidden = false; box.replaceChildren(el("li", "error", error.message)); }
}
$("kb-q").addEventListener("input", () => { clearTimeout(kbSearchTimer); kbSearchTimer = setTimeout(kbSearch, 250); });
for (const b of document.querySelectorAll("#kb-scope button")) b.addEventListener("click", () => {
  b.setAttribute("aria-pressed", String(b.getAttribute("aria-pressed") !== "true"));
  kbSearch();
});

$("arch-edit").addEventListener("click", () => {
  $("arch-text").value = state.kb?.architecture || "";
  $("arch-view").hidden = true;
  $("arch-editor").hidden = false;
});
$("arch-cancel").addEventListener("click", () => { $("arch-editor").hidden = true; $("arch-view").hidden = false; });
$("arch-save").addEventListener("click", async () => {
  try {
    await api(`/kb/${state.kb.key}/architecture`, { method: "PUT", json: { text: $("arch-text").value } });
    $("arch-editor").hidden = true;
    $("arch-view").hidden = false;
    loadKb();
  } catch (error) { toast(error.message, "error"); }
});
$("arch-agent").addEventListener("click", async () => {
  const target = current();
  const kb = state.kb;
  const folderGuide = await investigationGuide(target);
  const prompt = `Map how the game at ${target.path} ("${target.name}") is built and keep it in its REA knowledge base (KEY ${kb.key}). ` +
    `Use the skill rocsy/rea-knowledge (/opt/data/skills/rocsy/rea-knowledge/SKILL.md) and the REA MCP tools (paths as REA sees them, read-only).` +
    folderGuide +
    ` ARCHITECTURE.md is only about the game itself (no mod plans: each mod has its own architecture file).` +
    ` Read the current ARCHITECTURE.md and notes first, and the decompile progress (kb.py decomp ${kb.key}): reuse what is already in its decompiled/ folder and record what you decompile. Decompiled code is searchable through the knowledge base; ` +
    `otherwise inspect the main executables and assemblies with REA. Then rewrite ARCHITECTURE.md in the skill's structure, add notes for ` +
    `systems worth a closer look, and finish with a session note listing open questions.`;
  try {
    const response = await fetch("/Agent/api/sessions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt, ...modelFor("map") }) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    await recordSession(kb.key, data.id, "map", "Map the architecture");
    window.location.href = `/Agent/?s=${encodeURIComponent(data.id)}`;
  } catch (error) { toast(`Couldn't start the Master Agent: ${error.message}`, "error"); }
});

// ----------------------------------------------------------------------- sessions tab --
// Sessions REA started for this game (kb.mjs keeps the list) with their live state from the
// Agent service's summary endpoint.
async function recordSession(kbKey, id, kind, label, mod = "") {
  if (!kbKey || !id) return;
  try { await api(`/kb/${kbKey}/sessions`, { method: "POST", json: { id, kind, label, mod } }); } catch {}
}

let sessTimer;
async function loadSessions() {
  clearTimeout(sessTimer);
  const target = current();
  if (!target || target.type !== "folder" || state.fvTab !== "sessions") return;
  try {
    const { key } = await api(`/kb/for/${target.id}`);
    const { sessions } = await api(`/kb/${key}/sessions`);
    let live = {};
    let summaryError = "";
    if (sessions.length) {
      try {
        for (let i = 0; i < sessions.length; i += 100) {
          const ids = sessions.slice(i, i + 100).map((x) => x.id).join(",");
          const r = await fetch(`/Agent/api/sessions/summary?ids=${encodeURIComponent(ids)}`);
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          for (const x of (await r.json()).sessions) live[x.id] = x;
        }
      } catch (error) { summaryError = `Agent status unavailable (${error.message}). You can still open your sessions.`; }
    }
    if (current()?.id !== target.id || state.fvTab !== "sessions") return;
    renderSessions(sessions, live, key, summaryError);
    if (Object.values(live).some((x) => x.busy)) sessTimer = setTimeout(loadSessions, 4000);
  } catch (error) { $("sess-list").replaceChildren(el("li", "error", error.message)); }
}

const KIND_NAMES = { mod: "mod", rewrite: "rewrite", ask: "question", map: "architecture" };
function renderSessions(sessions, live, kbKey, summaryError = "") {
  const rows = sessions.map((rec) => ({ rec, s: live[rec.id] }))
    .sort((a, b) => (b.s?.updated || b.rec.created / 1000) - (a.s?.updated || a.rec.created / 1000));
  if (!rows.length) { $("sess-list").replaceChildren(el("li", "empty", "No sessions yet. They appear here when you ask the agent about this game, map its architecture or work on a mod.")); return; }
  const items = rows.map(({ rec, s }) => {
    const item = el("li", s?.busy ? "busy" : s?.missing ? "gone" : "");
    const top = el("div", "sess-top");
    top.append(el("span", `badge ${rec.kind}`, KIND_NAMES[rec.kind] || rec.kind), el("strong", "", s?.title || rec.label || rec.id));
    if (s?.missing) top.append(el("span", "badge bad", "deleted in the Agent"));
    else if (s) top.append(el("span", `badge ${s.busy ? "live" : s.error ? "bad" : ""}`, s.busy ? (s.stage || "working") : s.state));
    if (!s?.missing) {
      const open = el("a", "button", "Open");
      open.href = `/Agent/?s=${encodeURIComponent(rec.id)}`;
      open.target = "_blank";
      open.rel = "noopener";
      top.append(open);
    } else {
      const forget = el("button", "button", "Remove");
      forget.type = "button";
      forget.addEventListener("click", async () => {
        try { await api(`/kb/${kbKey}/sessions`, { method: "POST", json: { id: rec.id, remove: true } }); loadSessions(); }
        catch (error) { toast(error.message, "error"); }
      });
      top.append(forget);
    }
    item.append(top);
    const facts = el("div", "sess-facts");
    const fact = (text) => facts.append(el("span", "", text));
    if (rec.label && s?.title && rec.label !== s.title) fact(rec.label);
    if (s && !s.missing) {
      fact(`${s.model}${s.chosen ? " (chosen in REA)" : ""}${s.reasoning && s.reasoning !== "default" ? ` · thinking ${s.reasoning}` : ""}`);
      fact(`${s.hermes_runs} Hermes run${s.hermes_runs === 1 ? "" : "s"}`);
      if (s.jobs) fact(`${s.jobs} job${s.jobs === 1 ? "" : "s"}`);
      fact(`updated ${when(s.updated * 1000)}`);
      if (s.hermes_session) { const c = el("span"); c.append("Hermes session ", el("code", "", s.hermes_session)); facts.append(c); }
    } else fact(`started ${when(rec.created)}`);
    item.append(facts);
    item.append(readinessBar(rec.readiness));
    if (s?.last_user) { const q = el("p", "sess-msg"); q.append(el("b", "", "You: "), s.last_user); item.append(q); }
    if (s?.last_agent) { const a = el("p", "sess-msg"); a.append(el("b", "", "Agent: "), s.last_agent); item.append(a); }
    if (s?.error) item.append(el("p", "sess-msg error", s.error));
    return item;
  });
  if (summaryError) items.unshift(el("li", "error", summaryError));
  $("sess-list").replaceChildren(...items);
}
$("sess-refresh").addEventListener("click", loadSessions);

// --------------------------------------------------------------------- decompiled tab --
// What has been decompiled for this game (decomp.mjs): per file and tool, with the output kept in
// the knowledge base's decompiled/ folder so the agent and later sessions don't decompile it again.
const DECOMP_BY = { page: "this page", mods: "a mod project", agent: "the agent", user: "you" };
let decompData = null;
let decompValidation = null;
let repairRunning = false;
let repairStop = false;
const repairKey = (key) => `rea.decomp.repair.${key}`;
function repairStatus(message) { $("decomp-repair-status").textContent = message; }
function renderValidation(report) {
  decompValidation = report;
  const box = $("decomp-errors");
  box.classList.toggle("has-errors", report.total > 0);
  $("decomp-errors-summary").textContent = report.message ||
    `${report.total} syntax error${report.total === 1 ? "" : "s"} in ${report.checked} checked source files`;
  const languages = Object.entries(report.languages || {}).map(([name, count]) => `${name}: ${count}`).join(" · ");
  $("decomp-validation-meta").textContent = `${languages || "No source files found"}${report.unsupported ?
    ` · ${report.unsupported} files have no available parser` : ""}. Structural checks do not prove runtime correctness.`;
  $("decomp-address").disabled = repairRunning || !report.project || !report.total;
  $("decomp-diagnostics").replaceChildren(...(report.diagnostics || []).map((d) => {
    const item = el("li");
    const location = el(d.source === "overlay" ? "code" : "button", "decomp-location", `${d.path}:${d.line}:${d.column}`);
    if (d.source !== "overlay") {
      location.type = "button";
      location.title = "Open this source file";
      location.addEventListener("click", () => showDecompPath(d.browserPath || d.path));
    }
    item.append(location, ` ${d.message}${d.source === "overlay" ? " (mod overlay)" : ""}`);
    return item;
  }));
  if (report.truncated) $("decomp-diagnostics").append(el("li", "hint", `Showing first ${report.diagnostics.length} of ${report.total} errors.`));
}
async function loadValidation(key) {
  const report = await api(`/kb/${key}/validation`);
  if (decompData?.key === key) renderValidation(report);
  return report;
}
async function agentJson(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { "Content-Type": "application/json" },
    body: options.json === undefined ? undefined : JSON.stringify(options.json) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Agent HTTP ${response.status}`);
  return data;
}
const pauseRepair = () => new Promise((resolve) => setTimeout(resolve, 4000));
async function addressDecompErrors(key, session = "") {
  if (repairRunning || !decompValidation?.project) return;
  repairRunning = true;
  repairStop = false;
  $("decomp-address").disabled = true;
  $("decomp-stop").hidden = false;
  try {
    let report = await loadValidation(key);
    while (report.total && !repairStop) {
      if (!session) {
        const prompt = `Use Rocsy's rea-modding and rea-knowledge skills. Repair syntax errors for REA mod project ${report.project} ` +
          `and knowledge base ${key}. Read GET /REA/api/kb/${key}/validation first. Identify the recovered language and ` +
          `engine version. Decompiled files are read-only; put corrected deliverables in the mod project's src/files/ ` +
          `at their original paths when that engine supports file overlays. Resolve each diagnostic, run the available ` +
          `parser and REA build, then recheck validation. ` +
          `Do not install the mod. Keep working within this turn until there are no errors. If a specific issue cannot ` +
          `be fixed, end with BLOCKED: and a concrete reason and remaining file/line.\n` +
          report.diagnostics.slice(0, 80).map((d) => `${d.path}:${d.line}:${d.column} ${d.message}`).join("\n") +
          await api(`/mods/${report.project}`).then((m) => toolsNote(m.engine), () => "").then((n) => n ? `\n${n}` : "");
        session = (await agentJson("/Agent/api/sessions", { method: "POST", json: { prompt, ...modelFor("refine") } })).id;
        await recordSession(key, session, "mod", "Address decompiled errors", report.project);
        try { localStorage.setItem(repairKey(key), session); } catch {}
      } else {
        repairStatus("Waiting for Rocsy to finish this run…");
      }
      $("decomp-agent").href = `/Agent/?s=${encodeURIComponent(session)}`;
      $("decomp-agent").hidden = false;
      let agent;
      do { await pauseRepair(); agent = await agentJson(`/Agent/api/sessions/${encodeURIComponent(session)}`); }
      while (agent.busy);
      report = await loadValidation(key);
      if (!report.total) { repairStatus("Syntax checks pass. Rocsy's session has the repair details."); break; }
      const last = [...(agent.turns || [])].reverse().find((turn) => turn.role === "agent")?.text || "";
      if (agent.state === "failed" || agent.state === "stopped") {
        repairStatus(`Rocsy stopped: ${agent.error || agent.state}. ${report.total} errors remain.`); break;
      }
      if (agent.asked) { repairStatus(`Rocsy needs your answer: ${agent.asked}`); break; }
      if (/\bBLOCKED\s*:/i.test(last)) {
        repairStatus(`Rocsy reported a blocker: ${last.match(/\bBLOCKED\s*:[^\n]*/i)?.[0] || last.slice(0, 250)}`); break;
      }
      if (repairStop) { repairStatus(`Stopped after this run. ${report.total} errors remain.`); break; }
      repairStatus(`${report.total} errors remain. Asking Rocsy to continue…`);
      const text = `Continue repairing project ${report.project}. GET /REA/api/kb/${key}/validation and fix ` +
        `the remaining errors. Keep working until zero remain or report BLOCKED: with the exact reason.\n` +
        report.diagnostics.slice(0, 80).map((d) => `${d.path}:${d.line}:${d.column} ${d.message}`).join("\n");
      await agentJson(`/Agent/api/sessions/${encodeURIComponent(session)}/message`,
        { method: "POST", json: { text, ...modelFor("refine") } });
    }
  } catch (error) { repairStatus(`Repair paused: ${error.message}`); }
  finally {
    repairRunning = false;
    $("decomp-stop").hidden = true;
    $("decomp-address").disabled = !decompValidation?.total;
    try { localStorage.removeItem(repairKey(key)); } catch {}
  }
}
async function loadDecomp() {
  const target = current();
  if (!target || target.type !== "folder" || state.fvTab !== "decomp") return;
  try {
    const { key } = await api(`/kb/for/${target.id}`);
    const data = await api(`/kb/${key}/decompiled`);
    if (current()?.id !== target.id || state.fvTab !== "decomp") return;
    decompData = data;
    renderDecomp(data);
    if (state.inv) loadInventory();
    loadExplorer(key);
    decompCheck.load().catch((error) => { decompCheck.hide(); console.warn(error); });
    try {
      await loadValidation(key);
      let previous = "";
      try { previous = localStorage.getItem(repairKey(key)); } catch {}
      if (previous && !repairRunning) addressDecompErrors(key, previous);
    } catch (error) { $("decomp-errors-summary").textContent = `Validation unavailable: ${error.message}`; }
  } catch (error) { $("decomp-list").replaceChildren(el("li", "error", error.message)); }
}

function toolLine(name, t) {
  const row = el("div", "tool-row");
  row.append(el("span", `badge ${t.status}`, `${name} · ${t.status}`));
  const parts = [];
  if (t.total) parts.push(`${(t.done || 0).toLocaleString()} of ${t.total.toLocaleString()} functions`);
  else if (t.done) parts.push(`${t.done.toLocaleString()} item${t.done === 1 ? "" : "s"}`);
  if (t.files) parts.push(`${t.files.toLocaleString()} source files`);
  parts.push(`by ${DECOMP_BY[t.by] || t.by}`, `updated ${when(t.updated)}`);
  row.append(el("span", "", parts.join(" · ")));
  if (t.note) row.append(el("span", "", t.note));
  return row;
}

function renderDecomp(data) {
  $("decomp-open").hidden = !state.local;
  $("decomp-storage-path").replaceChildren("Recovered files: ", el("code", "", data.dir));
  const bins = data.binaries;
  const fns = bins.reduce((n, b) => n + (b.tools.ghidra?.done || 0), 0);
  const complete = bins.filter((b) => Object.values(b.tools).some((t) => t.status === "complete")).length;
  $("decomp-summary").textContent = bins.length
    ? `${bins.length} file${bins.length === 1 ? "" : "s"} · ${complete} fully decompiled · ${fns.toLocaleString()} function${fns === 1 ? "" : "s"} with pseudocode · folder ${data.dir}`
    : "";
  if (!bins.length) $("decomp-list").replaceChildren(el("li", "empty", "Nothing decompiled yet. Functions you open in a file from this folder, mod decompile steps and the agent's work all show up here."));
  else $("decomp-list").replaceChildren(...bins.map((b) => {
    const item = el("li", b.missing ? "missing" : b.stale ? "stale" : "");
    const top = el("div", "sess-top");
    top.append(el("strong", "", b.rel));
    if (b.format) top.append(el("span", "badge", b.format));
    if (b.stale) top.append(el("span", "badge partial", "stale: the game file changed"));
    if (b.missing) top.append(el("span", "badge bad", "file gone"));
    const details = el("button", "button", "Details");
    details.type = "button";
    const body = el("div");
    details.addEventListener("click", () => toggleDecompBinary(b, body, details));
    const forget = el("button", "button danger", "Forget");
    forget.type = "button";
    forget.addEventListener("click", async () => {
      if (!confirm(`Forget everything decompiled from ${b.rel} and delete its output?`)) return;
      try { await api(`/kb/${data.key}/decompiled?rel=${encodeURIComponent(b.rel)}`, { method: "DELETE" }); loadDecomp(); }
      catch (error) { toast(error.message, "error"); }
    });
    const more = el("button", "button", "Continue");
    more.type = "button";
    more.title = "Ask the agent to keep decompiling this file (no turn limit)";
    more.addEventListener("click", () => continueDecomp(b.rel));
    top.append(more, details, forget);
    item.append(top, ...Object.entries(b.tools).map(([name, t]) => toolLine(name, t)), body);
    return item;
  }));
  $("decomp-log").replaceChildren(...(data.log.length ? data.log.map((x) => {
    const li = el("li");
    li.append(`${when(x.at)} · ${x.tool} · ${x.action}${x.item ? ` ${x.item}` : ""} · `, el("code", "", x.rel), ` · ${DECOMP_BY[x.by] || x.by}`);
    return li;
  }) : [el("li", "", "No activity yet.")]));
}

async function toggleDecompBinary(b, body, button) {
  if (body.childElementCount) { body.replaceChildren(); button.textContent = "Details"; return; }
  button.textContent = "Hide";
  try {
    const full = await api(`/kb/${decompData.key}/decompiled/binary?rel=${encodeURIComponent(b.rel)}`);
    for (const [name, t] of Object.entries(full.tools)) {
      const items = full.items.filter((x) => x.tool === name);
      const row = el("div", "tool-row");
      const browse = el("button", "button", `Browse ${name} output`);
      browse.type = "button";
      browse.addEventListener("click", () => showDecompPath(t.out));
      row.append(browse, el("code", "", full.dirs[name]));
      body.append(row);
      if (items.length) {
        const list = el("ul", "decomp-items");
        for (const x of items.slice(0, 2000)) {
          const li = el("li");
          const open = el("button", "", x.name);
          open.type = "button";
          open.title = `${x.kind} ${x.id} · ${when(x.at)} by ${DECOMP_BY[x.by] || x.by}`;
          if (x.file) open.addEventListener("click", () => showDecompPath(x.file));
          else open.disabled = true;
          li.append(open);
          list.append(li);
        }
        body.append(list);
      }
    }
  } catch (error) { body.replaceChildren(el("p", "error", error.message)); }
}

// A file or folder inside decompiled/, in the viewer below the list.
async function showDecompPath(rel) {
  try {
    const data = await api(`/kb/${decompData.key}/decompiled/file?path=${encodeURIComponent(rel)}`);
    $("decomp-viewer").hidden = false;
    $("decomp-path").replaceChildren(el("code", "", `${decompData.dir}/${data.path}`));
    $("decomp-dir").hidden = !data.entries;
    $("decomp-code").hidden = Boolean(data.entries);
    if (data.entries) {
      const up = data.path.includes("/") ? data.path.slice(0, data.path.lastIndexOf("/")) : "";
      const rows = data.entries.map((e) => {
        const li = el("li");
        const b = el("button", "", e.dir ? `${e.name}/` : e.name);
        b.type = "button";
        b.addEventListener("click", () => showDecompPath(`${data.path}/${e.name}`));
        li.append(b);
        return li;
      });
      if (up) { const li = el("li"); const b = el("button", "", "../"); b.type = "button"; b.addEventListener("click", () => showDecompPath(up)); li.append(b); rows.unshift(li); }
      $("decomp-dir").replaceChildren(...(rows.length ? rows : [el("li", "hint", "Empty folder")]));
    } else {
      $("decomp-code").replaceChildren(/\.(c|cs|cpp|h|hpp|java|js)$/i.test(data.path) ? highlight(data.text) : data.text);
    }
    $("decomp-viewer").scrollIntoView({ block: "nearest" });
  } catch (error) { toast(error.message, "error"); }
}
$("decomp-close").addEventListener("click", () => { $("decomp-viewer").hidden = true; });

// Continue decompiling: one Master Agent session per game (Sessions tab, kind "decomp"). Starting
// or resuming it sets a /goal with no turn limit, so the agent keeps going until it's done.
const DECOMP_GOAL = "the game's own code is fully decompiled and recorded in the knowledge base's decompiled/ folder (every relevant file marked " +
  "complete with kb.py decomp-mark), the decompiled output has no known errors, and its syntax is verified (it parses or compiles wherever the " +
  "language allows, e.g. dotnet build for ILSpy output, Godot --check-only for GDScript, node --check for JavaScript), so it is theoretically runnable";
async function continueDecomp(rel = "") {
  const target = current();
  if (!target || target.type !== "folder") return;
  const button = $("decomp-continue");
  button.disabled = true;
  try {
    const { key } = await api(`/kb/for/${target.id}`);
    const data = await api(`/kb/${key}/decompiled`);
    const { sessions } = await api(`/kb/${key}/sessions`);
    const progress = data.binaries.length ? data.binaries.map((b) => `- ${b.rel}${b.stale ? " (STALE: the game changed, redo)" : ""}: ` +
      Object.entries(b.tools).map(([t, x]) => `${t} ${x.status}${x.total ? ` ${x.done || 0}/${x.total} functions` : x.files ? ` ${x.files} files` : x.done ? ` ${x.done} items` : ""}`).join(", ")).join("\n")
      : "- nothing decompiled yet";
    const objective = `Continue decompiling the game "${target.name}" at ${target.path} (read-only, paths as REA sees them) until ${DECOMP_GOAL}.\n` +
      (rel ? `Start with ${target.path}/${rel}.\n` : "") +
      `Knowledge base KEY ${key}; decompiled/ folder: ${data.dir}.\nProgress recorded so far (kb.py decomp ${key}):\n${progress}\n\n` +
      `Use the skill rocsy/rea-knowledge (read /opt/data/skills/rocsy/rea-knowledge/SKILL.md, section "Decompile progress") and the REA MCP tools. ` +
      `Before decompiling anything run kb.py decomp ${key} REL and never redo recorded items; continue partial files where they stopped. ` +
      `Save every decompiled function with kb.py decomp-save, record bulk output with decomp-mark --files N, and mark a file --status complete once done. ` +
      `Work on the game's own code first (main executable, Assembly-CSharp*, GameAssembly + metadata, game scripts/packs) and skip stock engine and third-party libraries. ` +
      `For .NET, Godot packs, asar or RPG Maker/Ren'Py scripts use REA's Mods decompile (it records itself) instead of decompiling by hand. ` +
      `Check the output's syntax and fix or re-run broken pieces; note anything that can't be recovered. Keep ARCHITECTURE.md and notes current as you learn. ` +
      `When the goal is met, run $R goal done with the final kb.py decomp summary as evidence.` + checkBrief(key) +
      await investigationGuide(target);
    const text = `/goal infinite ${objective}`;
    const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(async (r) => { const d = await r.json().catch(() => ({})); if (!r.ok) throw Object.assign(new Error(d.detail || `HTTP ${r.status}`), { status: r.status }); return d; });
    let session = sessions.find((x) => x.kind === "decomp")?.id || "";
    if (session) {
      try { await post(`/Agent/api/sessions/${encodeURIComponent(session)}/message`, { text, on_busy: "queue", ...modelFor("decomp") }); }
      catch (error) { if (error.status !== 404) throw error; session = ""; }
    }
    const resumed = Boolean(session);
    if (!session) {
      session = (await post("/Agent/api/sessions", { prompt: text, ...modelFor("decomp") })).id;
      await recordSession(key, session, "decomp", `Decompile: ${target.name}`);
    }
    toast(`${resumed ? "Resumed" : "Started"} the decompile session with a no-limit goal. It's in the Sessions tab.`);
    window.open(`/Agent/?s=${encodeURIComponent(session)}`, "_blank", "noopener");
  } catch (error) { toast(`Couldn't reach the Master Agent: ${error.message}`, "error"); }
  finally { button.disabled = false; }
}
$("decomp-continue").addEventListener("click", () => continueDecomp());

// Validate build (buildcheck.mjs), one panel per subject: the Decompiled tab (the game's recovered
// output), a mod (every file packed, the patched game compiles, runs like the original) and a rewrite
// (parity checklist, compiles, runs). Each panel polls while its check runs.
const CHECK_VERDICT = { pass: ["complete", "Validated"], partial: ["partial", "Partly validated"], fail: ["failed", "Not valid"] };
const CHECK_MARK = { pass: "✓", warn: "!", fail: "✗", skip: "–" };
function checkRow(label, c) {
  const row = el("div", "decomp-check-row");
  row.append(el("span", `mark ${c.status}`, CHECK_MARK[c.status] || "?"), el("span", "label", label), el("span", "", c.detail || ""));
  const list = [...(c.errors || []), ...(c.missing || [])];
  if (list.length) {
    const more = el("details");
    more.append(el("summary", "", `${list.length}${list.length >= 200 ? "+" : ""} item${list.length === 1 ? "" : "s"}`));
    const ul = el("ul");
    ul.append(...list.map((x) => el("li", "", x)));
    more.append(ul);
    row.append(more);
  }
  return row;
}
// box: an empty <details>; button: its "Validate build" button; url(): the check's API path (or "")
// for what is open now; fix(report): hand the failures to the agent; visible(): still on screen.
function buildCheckPanel({ box, button, url, fix, visible, logPath }) {
  const p = { report: null, timer: 0, url: "" };
  const summary = el("summary"), verdict = el("span", "badge"), headline = el("span");
  summary.append(verdict, " ", headline);
  const meta = el("p", "hint"), units = el("div"), actions = el("div", "step-actions");
  const fixBtn = el("button", "button primary", "Ask Rocsy to fix this"), logBtn = el("button", "button", "Show log");
  fixBtn.type = logBtn.type = "button";
  actions.append(fixBtn, logBtn);
  const body = el("div", "decomp-errors-body");
  body.append(meta, units, actions);
  box.replaceChildren(summary, body);
  function render(report) {
    p.report = report;
    box.hidden = !report;
    button.disabled = report?.state === "running";
    button.textContent = report?.state === "running" ? "Validating…" : "Validate build";
    body.querySelector("pre.code")?.remove();
    logBtn.textContent = "Show log";
    if (!report) return;
    const live = report.state === "running";
    const [cls, word] = live ? ["live", "Checking"] : CHECK_VERDICT[report.verdict] || ["failed", "Failed"];
    verdict.className = `badge ${cls}`;
    verdict.textContent = word;
    headline.textContent = live ? `${report.step || "starting"}…` : report.headline || "";
    meta.textContent = live ? `Started ${when(report.started)} · CPU only; builds and test runs wait until it finishes.`
      : `${report.engine ? `${report.engine} · ` : ""}checked ${when(report.finished || report.started)} in ` +
        `${Math.max(1, Math.round(((report.finished || Date.now()) - report.started) / 1000))}s` +
        " · Compiling proves the code is valid; running it only covers startup, not every code path.";
    units.replaceChildren(...(report.units || []).map((u) => {
      const card = el("div", "decomp-check-unit");
      card.append(el("strong", "", u.label || u.rel), checkRow("Complete", u.complete), checkRow("Compiles", u.compile), checkRow("Runs", u.run));
      return card;
    }));
    fixBtn.hidden = live || report.verdict === "pass";
    if (live) box.open = true;
  }
  async function load() {
    clearTimeout(p.timer);
    const path = url();
    if (!path) return;
    p.url = path;
    const { report } = await api(path);
    if (url() !== path) return;
    const wasLive = p.report?.state === "running";
    render(report);
    if (report?.state === "running") p.timer = setTimeout(() => { if (visible() && url() === path) load().catch(() => {}); }, 3000);
    else if (wasLive) { box.open = true; toast(`Build validation: ${CHECK_VERDICT[report.verdict]?.[1] || report.verdict}`); }
  }
  button.addEventListener("click", async () => {
    const path = url();
    if (!path) return;
    button.disabled = true;
    try {
      const { report } = await api(path, { method: "POST", json: {} });
      render(report);
      box.open = true;
      load();
    } catch (error) { toast(error.message, "error"); button.disabled = false; }
  });
  logBtn.addEventListener("click", async () => {
    const old = body.querySelector("pre.code");
    if (old) { old.remove(); logBtn.textContent = "Show log"; return; }
    try {
      // The Decompiled tab reads it through decompiled/file (JSON), projects through raw (plain text).
      const response = await fetch(API + logPath(), { headers: { "X-REA": "1" } });
      if (!response.ok) throw new Error(response.status === 404 ? "No log yet" : `HTTP ${response.status}`);
      const text = (response.headers.get("content-type") || "").includes("json") ? (await response.json()).text || "" : await response.text();
      body.append(el("pre", "code", text.length > 200000 ? `…${text.slice(-200000)}` : text));
      logBtn.textContent = "Hide log";
    } catch (error) { toast(error.message, "error"); }
  });
  fixBtn.addEventListener("click", () => fix(p.report));
  // For agent briefs: what the last check found, and how to re-run it.
  p.brief = (base) => {
    const r = p.report;
    if (!r || r.state === "running" || r.verdict === "pass" || url() !== p.url) return "";
    const lines = (r.units || []).flatMap((u) => [["complete", u.complete], ["compile", u.compile], ["run", u.run]]
      .filter(([, c]) => c.status === "fail" || c.status === "warn")
      .map(([what, c]) => `- ${u.rel} ${what} ${c.status}: ${c.detail}${(c.errors || c.missing || []).length ? `\n  ${[...(c.errors || []), ...(c.missing || [])].slice(0, 15).join("\n  ")}` : ""}`));
    return `\n\nThe last "Validate build" check (${new Date(r.finished || r.started).toISOString()}) says: ${r.headline}\n${lines.join("\n")}\n` +
      `Fix these first. Re-run the check with curl -s -X POST -H 'X-REA: 1' ${location.origin}${API}${p.url} and read the result with GET on the same URL (state done) until it reports pass or only what can't be fixed${base ? `. ${base}` : ""}.`;
  };
  p.load = load;
  p.hide = () => { clearTimeout(p.timer); p.report = null; box.hidden = true; button.disabled = false; button.textContent = "Validate build"; };
  return p;
}
const decompCheck = buildCheckPanel({ box: $("decomp-check"), button: $("decomp-check-go"),
  url: () => decompData ? `/kb/${decompData.key}/build-check` : "", visible: () => state.fvTab === "decomp",
  logPath: () => `/kb/${decompData.key}/decompiled/file?path=build-check.log`, fix: () => continueDecomp() });
const checkBrief = () => decompCheck.brief();
const modCheck = buildCheckPanel({ box: $("mod-check"), button: $("mod-check-go"),
  url: () => state.mod ? `/mods/${state.mod}/build-check` : "", visible: () => Boolean(state.mod),
  logPath: () => `/mods/${state.mod}/raw?path=build-check.log`,
  fix: () => tellAgent(`Step: FIX WHAT "VALIDATE BUILD" FOUND for mod ${state.mod}. Change src/ (never the decompiled files), rebuild through REA and validate again.` +
    modCheck.brief("Remember the mod must keep using the game's own scripts and assets"), "refining", "refine") });
const rewriteCheck = buildCheckPanel({ box: $("rewrite-check"), button: $("rewrite-check-go"),
  url: () => state.rewrite ? `/mods/rewrites/${state.rewrite}/build-check` : "", visible: () => Boolean(state.rewrite),
  logPath: () => `/mods/rewrites/${state.rewrite}/raw?path=build-check.log`,
  fix: () => tellRewriteAgent(`Fix what "Validate build" found for this rewrite: make it compile and run cleanly, and complete the parity checklist with evidence.` +
    rewriteCheck.brief("Keep PARITY.json honest: only mark behaviors passed with evidence"), "building") });

// ------------------------------------------------------------------ decompiled explorer --
// The game as REA rebuilt it: each decompiled binary is a folder with its recovered Scripts and
// extracted Assets (engine/import leftovers hidden unless asked), browsable like a file system with
// previews for code, images, audio, video, fonts, 3D models and a hex view for everything else.
const DX_KINDS = [["all", "All"], ["scripts", "Scripts"], ["image", "Images"], ["audio", "Audio"], ["video", "Video"], ["model", "Models"],
  ["scene", "Scenes"], ["font", "Fonts"], ["shader", "Shaders"], ["data", "Data & text"], ["other", "Other"]];
const DX_GLYPH = { code: "{ }", project: "proj", image: "img", audio: "♪", video: "▶", model: "3D", scene: "scn", shader: "fx", font: "Aa", data: "txt", engine: "eng", other: "bin" };
const DX_TEXT = new Set(["code", "project", "data", "scene", "shader"]);
const DX_PAGE = 240;
const dx = { key: "", dir: "", data: null, kind: "all", engine: false, query: "", open: new Set(), sel: null, roots: [], fontN: 0, viewer: null, blobs: [] };
// Leaving a model: free its WebGL context and embedded-texture blobs.
function dxRelease() {
  dx.viewer?.destroy();
  dx.viewer = null;
  for (const url of dx.blobs.splice(0)) URL.revokeObjectURL(url);
}

const dxGroup = (f) => f.k === "engine" ? "Engine & import files" : f.k === "code" || f.k === "project" ? "Scripts" : "Assets";
const dxRaw = (p, download) => `${API}/kb/${dx.key}/decompiled/raw?path=${encodeURIComponent(p)}${download ? "&download=1" : ""}`;
const dxExt = (name) => (name.match(/\.([^./]+)$/)?.[1] || "").toLowerCase();

function dxMatches(f) {
  if (f.k === "engine" && !dx.engine) return false;
  if (dx.kind === "scripts" ? !(f.k === "code" || f.k === "project") : dx.kind !== "all" && f.k !== dx.kind) return false;
  return !dx.query || f.v.toLowerCase().includes(dx.query);
}

// Folder nodes: { id, name, dirs: Map, files: [], count, bin, group }.
function dxNode(id, name, extra = {}) { return { id, name, dirs: new Map(), files: [], count: 0, ...extra }; }
function dxBuild() {
  dx.roots = [];
  let shown = 0;
  for (const [i, bin] of (dx.data?.binaries || []).entries()) {
    const root = dxNode(`b${i}`, bin.rel, { bin, root: true });
    for (const f of bin.files) {
      if (!dxMatches(f)) continue;
      const group = dxGroup(f);
      let node = root.dirs.get(group);
      if (!node) { node = dxNode(`${root.id}/${group}`, group, { bin, group: true }); root.dirs.set(group, node); }
      const parts = f.v.split("/");
      root.count++; node.count++;
      for (const part of parts.slice(0, -1)) {
        let next = node.dirs.get(part);
        if (!next) { next = dxNode(`${node.id}/${part}`, part, { bin }); node.dirs.set(part, next); }
        next.count++;
        node = next;
      }
      node.files.push({ ...f, name: parts.at(-1), bin });
      shown++;
    }
    // Scripts first, then Assets, then engine files.
    root.dirs = new Map([...root.dirs].sort(([a], [b]) => ["Scripts", "Assets"].indexOf(b) - ["Scripts", "Assets"].indexOf(a) || a.localeCompare(b)));
    if (root.count || !dx.query && dx.kind === "all") dx.roots.push(root);
  }
  return shown;
}
const dxSortDirs = (node) => [...node.dirs.values()].sort((a, b) => node.root ? 0 : a.name.localeCompare(b.name, undefined, { numeric: true }));
const dxSortFiles = (node) => node.files.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));

async function loadExplorer(key) {
  if (dx.key !== key) Object.assign(dx, { open: new Set(), sel: null, query: "", kind: "all", engine: false });
  if (!dx.query) $("dx-filter").value = "";
  $("dx-engine").checked = dx.engine;
  dx.key = key;
  try {
    const data = await api(`/kb/${key}/decompiled/tree`);
    if (dx.key !== key) return;
    dx.data = data;
    dx.dir = data.dir;
    renderExplorer();
  } catch (error) { $("dx-tree").replaceChildren(el("li", "error", error.message)); }
}

function renderExplorer() {
  const shown = dxBuild();
  const all = (dx.data?.binaries || []).reduce((n, b) => n + b.files.length, 0);
  const counts = {};
  for (const b of dx.data?.binaries || []) for (const f of b.files) counts[f.k] = (counts[f.k] || 0) + 1;
  $("dx-kinds").replaceChildren(...DX_KINDS.map(([id, label]) => {
    const n = id === "all" ? all - (dx.engine ? 0 : counts.engine || 0) : id === "scripts" ? (counts.code || 0) + (counts.project || 0) : counts[id] || 0;
    if (id !== "all" && id !== dx.kind && !n) return "";
    const b = el("button", "", `${label} ${n.toLocaleString()}`);
    b.type = "button";
    b.setAttribute("aria-pressed", String(dx.kind === id));
    b.addEventListener("click", () => { dx.kind = id; renderExplorer(); });
    return b;
  }).filter(Boolean));
  const bins = dx.data?.binaries || [];
  $("dx-summary").textContent = !bins.length ? "" :
    `${bins.length} decompiled file${bins.length === 1 ? "" : "s"} · ${((counts.code || 0) + (counts.project || 0)).toLocaleString()} scripts · ` +
    `${(all - (counts.code || 0) - (counts.project || 0) - (counts.engine || 0)).toLocaleString()} assets` +
    `${counts.engine ? ` · ${counts.engine.toLocaleString()} engine/import files${dx.engine ? "" : " hidden"}` : ""}` +
    `${dx.query || dx.kind !== "all" ? ` · ${shown.toLocaleString()} match` : ""}${dx.data?.truncated ? " · list capped at 80,000 files" : ""}`;
  if (!bins.length) {
    $("dx-tree").replaceChildren(el("li", "empty", "Nothing decompiled yet."));
    $("dx-view").replaceChildren(el("p", "hint", "Run the decompile step in the Mods tab (Godot, RPG Maker, Ren'Py, Electron and Unreal games get their scripts and assets; Unity gets its C#), " +
      "open functions of a file from the Files tab (Ghidra pseudocode lands under Scripts → Functions), or pull an .exe's embedded resources with Extract assets."));
    return;
  }
  // A search opens every folder with a match (while that stays readable).
  if (dx.query && shown <= 400) for (const r of dx.roots) (function openAll(n) { dx.open.add(n.id); for (const d of n.dirs.values()) openAll(d); })(r);
  else for (const r of dx.roots) { dx.open.add(r.id); }
  $("dx-tree").replaceChildren(...dx.roots.map((r) => dxTreeItem(r, 0)));
  // Keep the open view if it still exists, else show the first binary.
  const sel = dx.sel && dxFind(dx.sel.id);
  const file = sel && dx.sel.file && sel.files.find((x) => x.p === dx.sel.file.p);
  if (file) dxShowFile(file);
  else if (sel && !dx.sel.file) dxShowFolder(sel);
  else if (dx.roots[0]) dxShowFolder(dx.roots[0]);
  else $("dx-view").replaceChildren(el("p", "hint", "Nothing matches."));
}

function dxFind(id) {
  const walk = (n) => { if (n.id === id) return n; for (const d of n.dirs.values()) { const x = walk(d); if (x) return x; } return null; };
  for (const r of dx.roots) { const x = walk(r); if (x) return x; }
  return null;
}

function dxTreeItem(node, depth) {
  const li = el("li");
  li.setAttribute("role", "treeitem");
  const open = dx.open.has(node.id);
  li.setAttribute("aria-expanded", String(open));
  const row = el("button", `dx-row dir${node.root ? " root" : ""}${node.group ? " group" : ""}${dx.sel?.id === node.id && !dx.sel.file ? " active" : ""}`);
  row.type = "button";
  row.style.paddingLeft = `${6 + depth * 14}px`;
  row.append(el("span", "dx-caret", open ? "▾" : "▸"), el("span", "dx-name", node.root ? node.name : `${node.name}/`), el("span", "dx-count", node.count.toLocaleString()));
  if (node.root && node.bin.stale) row.append(el("span", "badge partial", "stale"));
  row.addEventListener("click", () => {
    if (dx.sel?.id === node.id && !dx.sel.file || !open) {
      if (open) dx.open.delete(node.id); else dx.open.add(node.id);
    }
    dxShowFolder(node);
    li.replaceWith(dxTreeItem(node, depth));
  });
  li.append(row);
  if (open) {
    const ul = el("ul");
    ul.setAttribute("role", "group");
    for (const d of dxSortDirs(node)) ul.append(dxTreeItem(d, depth + 1));
    const files = dxSortFiles(node);
    for (const f of files.slice(0, 300)) {
      const fli = el("li");
      fli.setAttribute("role", "treeitem");
      const b = el("button", `dx-row file${dx.sel?.file?.p === f.p ? " active" : ""}`);
      b.type = "button";
      b.style.paddingLeft = `${20 + (depth + 1) * 14}px`;
      b.append(el("span", `dx-glyph k-${f.k}`, DX_GLYPH[f.k] || "bin"), el("span", "dx-name", f.name));
      b.addEventListener("click", () => {
        for (const x of $("dx-tree").querySelectorAll(".dx-row.active")) x.classList.remove("active");
        b.classList.add("active");
        dxShowFile(f);
      });
      fli.append(b);
      ul.append(fli);
    }
    if (files.length > 300) {
      const more = el("button", "dx-row more", `${(files.length - 300).toLocaleString()} more in this folder…`);
      more.type = "button";
      more.style.paddingLeft = `${20 + (depth + 1) * 14}px`;
      more.addEventListener("click", () => dxShowFolder(node));
      const mli = el("li"); mli.append(more); ul.append(mli);
    }
    li.append(ul);
  }
  return li;
}

function dxCrumbs(node, file) {
  // Path from the binary root to this folder, each part clickable.
  const crumbs = el("nav", "dx-crumbs");
  const ids = [];
  const target = node?.id || "";
  for (const part of target.split("/")) ids.push(ids.length ? `${ids.at(-1)}/${part}` : part);
  ids.forEach((id, i) => {
    const n = dxFind(id);
    if (!n) return;
    if (i) crumbs.append(el("span", "sep", "/"));
    const b = el("button", "", n.name);
    b.type = "button";
    b.addEventListener("click", () => { dx.open.add(n.id); dxShowFolder(n); });
    crumbs.append(b);
  });
  if (file) crumbs.append(el("span", "sep", "/"), el("strong", "", file.name));
  return crumbs;
}

function dxBinaryInfo(bin) {
  const box = el("div", "dx-bininfo");
  for (const [name, t] of Object.entries(bin.tools)) {
    const parts = [t.total ? `${(t.done || 0).toLocaleString()} of ${t.total.toLocaleString()} functions` : t.done ? `${t.done.toLocaleString()} items` : "",
      t.files ? `${t.files.toLocaleString()} files` : ""].filter(Boolean);
    const row = el("div", "tool-row");
    row.append(el("span", `badge ${t.status}`, `${name} · ${t.status}`), el("span", "hint", parts.join(" · ")));
    box.append(row);
  }
  const actions = el("div", "step-actions");
  if (/\.(exe|dll)$/i.test(bin.rel) && !bin.missing) {
    const ex = el("button", "button", bin.tools.peres ? "Extract assets again" : "Extract assets");
    ex.type = "button";
    ex.title = "Pull the icons, bitmaps, version info, strings and embedded data out of this file";
    ex.addEventListener("click", () => dxExtract(bin.rel, ex));
    actions.append(ex);
  }
  const more = el("button", "button", "Continue decompiling");
  more.type = "button";
  more.addEventListener("click", () => continueDecomp(bin.rel));
  actions.append(more);
  box.append(actions);
  if (bin.tools.ilspy && !Object.keys(bin.tools).some((t) => t !== "ilspy"))
    box.append(el("p", "hint", "Unity keeps its textures, audio and meshes in the game's *_Data/*.assets and bundle files; REA has no Unity asset extractor installed, so this shows the C# scripts only."));
  if (bin.stale) box.append(el("p", "hint", "Stale: the game file changed since this was decompiled. Forget it in Progress and decompile again for the current version."));
  return box;
}

function dxShowFolder(node) {
  dxRelease();
  dx.sel = { id: node.id };
  for (const x of $("dx-tree").querySelectorAll(".dx-row.active")) x.classList.remove("active");
  const view = $("dx-view");
  const head = el("div", "dx-head");
  head.append(dxCrumbs(node));
  const dirs = dxSortDirs(node), files = dxSortFiles(node);
  const kinds = {};
  for (const f of files) kinds[f.k] = (kinds[f.k] || 0) + 1;
  head.append(el("p", "hint", `${node.count.toLocaleString()} file${node.count === 1 ? "" : "s"} in total · ${dirs.length} folder${dirs.length === 1 ? "" : "s"} and ${files.length.toLocaleString()} file${files.length === 1 ? "" : "s"} here` +
    (files.length ? ` (${Object.entries(kinds).map(([k, n]) => `${n} ${k}`).join(", ")})` : "")));
  view.replaceChildren(head);
  if (node.root) view.append(dxBinaryInfo(node.bin));
  const grid = el("ul", "dx-grid");
  let limit = DX_PAGE;
  const fill = () => {
    const items = [...dirs.map((d) => ["dir", d]), ...files.map((f) => ["file", f])];
    grid.replaceChildren(...items.slice(0, limit).map(([type, x]) => {
      const li = el("li");
      const b = el("button", `dx-tile ${type}`);
      b.type = "button";
      b.title = type === "dir" ? `${x.name}/ · ${x.count} files` : `${x.v} · ${size(x.s)}`;
      if (type === "dir") {
        b.append(el("span", "dx-thumb folder", "▤"), el("span", "dx-tname", `${x.name}/`), el("span", "dx-tmeta", `${x.count.toLocaleString()} files`));
        b.addEventListener("click", () => { dx.open.add(node.id); dx.open.add(x.id); renderTreeOnly(); dxShowFolder(x); });
      } else {
        const thumb = el("span", `dx-thumb k-${x.k}`);
        if (x.k === "image" && x.s < 8 * 1024 * 1024 && !/^(tga|dds|psd|exr|hdr|ktx2?|tiff?|dib|pcx)$/.test(dxExt(x.name))) {
          const img = el("img");
          img.loading = "lazy";
          img.alt = "";
          img.src = dxRaw(x.p);
          img.addEventListener("error", () => img.replaceWith(DX_GLYPH.image));
          thumb.append(img);
        } else thumb.textContent = DX_GLYPH[x.k] || "bin";
        b.append(thumb, el("span", "dx-tname", x.name), el("span", "dx-tmeta", size(x.s)));
        b.addEventListener("click", () => dxShowFile(x));
      }
      li.append(b);
      return li;
    }));
    if (items.length > limit) {
      const li = el("li");
      const more = el("button", "dx-tile more", `Show ${Math.min(DX_PAGE, items.length - limit)} more of ${(items.length - limit).toLocaleString()}`);
      more.type = "button";
      more.addEventListener("click", () => { limit += DX_PAGE; fill(); });
      li.append(more);
      grid.append(li);
    }
  };
  fill();
  view.append(dirs.length || files.length ? grid : el("p", "hint", "Empty folder."));
}

function renderTreeOnly() { $("dx-tree").replaceChildren(...dx.roots.map((r) => dxTreeItem(r, 0))); }

async function dxShowFile(f) {
  dxRelease();
  const parentId = (() => {
    const group = `b${dx.data.binaries.indexOf(f.bin)}/${dxGroup(f)}`;
    const dir = f.v.includes("/") ? f.v.slice(0, f.v.lastIndexOf("/")) : "";
    return dir ? `${group}/${dir}` : group;
  })();
  dx.sel = { id: parentId, file: f };
  const view = $("dx-view");
  const head = el("div", "dx-head");
  head.append(dxCrumbs(dxFind(parentId), f));
  const meta = el("div", "dx-meta");
  meta.append(el("span", `badge k-${f.k}`, f.k), el("span", "hint", `${size(f.s)} · ${f.t}${f.t !== f.bin.rel ? ` output of ${f.bin.rel}` : ""}`));
  const actions = el("div", "step-actions");
  const dl = el("a", "button", "Download");
  dl.href = dxRaw(f.p, true);
  actions.append(dl);
  if (state.local) {
    const show = el("button", "button", "Show in folder");
    show.type = "button";
    show.addEventListener("click", () => openOnHost(`${dx.dir}/${f.p}`));
    actions.append(show);
  }
  const copy = el("button", "button", "Copy path");
  copy.type = "button";
  copy.title = "Copy the path as the agent sees it";
  copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(`${dx.dir}/${f.p}`); toast("Path copied"); } catch { toast(`${dx.dir}/${f.p}`); }
  });
  actions.append(copy);
  head.append(meta, actions);
  const body = el("div", "dx-preview");
  view.replaceChildren(head, body);
  if (matchMedia("(max-width: 860px)").matches) view.scrollIntoView({ block: "start" });
  const ext = dxExt(f.name);
  try {
    if (f.k === "image" && DX_DECODE_EXT.has(ext)) {
      // Formats browsers can't show: decoded here (models.js).
      const response = await fetch(dxRaw(f.p));
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const canvas = dxDecodeImage(ext, await response.arrayBuffer());
      canvas.className = "dx-image";
      body.append(el("div", "dx-checker"), el("p", "hint", `${canvas.width} × ${canvas.height} px · decoded .${ext}`));
      body.firstChild.append(canvas);
    } else if (f.k === "image" && !/^(psd|exr|hdr|ktx2?|tiff?|dib)$/.test(ext)) {
      const img = el("img", "dx-image");
      img.alt = f.name;
      img.src = dxRaw(f.p);
      const dims = el("p", "hint");
      img.addEventListener("load", () => { dims.textContent = `${img.naturalWidth} × ${img.naturalHeight} px`; });
      img.addEventListener("error", () => { dims.textContent = "The browser can't show this image format."; });
      body.append(el("div", "dx-checker"), dims);
      body.firstChild.append(img);
    } else if (f.k === "audio" && /^(ogg|oga|opus|wav|mp3|flac|m4a|aac)$/.test(ext)) {
      const audio = el("audio");
      audio.controls = true;
      audio.preload = "metadata";
      audio.src = dxRaw(f.p);
      body.append(audio);
    } else if (f.k === "video" && /^(webm|mp4|m4v|ogv)$/.test(ext)) {
      const video = el("video", "dx-video");
      video.controls = true;
      video.preload = "metadata";
      video.src = dxRaw(f.p);
      body.append(video);
    } else if (f.k === "font" && /^(ttf|otf|woff2?)$/.test(ext)) {
      const family = `dxfont${++dx.fontN}`;
      const face = new FontFace(family, `url("${dxRaw(f.p)}")`);
      await face.load();
      document.fonts.add(face);
      for (const px of [40, 24, 15]) {
        const p = el("p", "dx-font", px === 15 ? "ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdefghijklmnopqrstuvwxyz 0123456789 .,:;!?()[]{}&%$#@" : "The quick brown fox jumps over the lazy dog");
        p.style.fontFamily = family;
        p.style.fontSize = `${px}px`;
        body.append(p);
      }
    } else if (DX_MODEL_EXT.has(ext) && (f.k === "model" || ext === "x")) {
      await dxModel(f, ext, body);
    } else if (DX_TEXT.has(f.k) || ext === "gltf" || ext === "mtl") {
      if (/^(tscn|escn)$/.test(ext) && typeof sceneExplore === "function") sceneExplore(f, body);
      await dxText(f, body);
    } else {
      await dxHex(f, body);
    }
  } catch (error) { body.append(el("p", "error", `Couldn't preview: ${error.message}`)); }
}

async function dxText(f, body) {
  // Same extension, different content: Godot's binary .scn/.res and the like get the hex view.
  const head = await fetch(dxRaw(f.p), { headers: { Range: "bytes=0-4095" } });
  if (!head.ok) throw new Error(`HTTP ${head.status}`);
  if (new Uint8Array(await head.arrayBuffer()).includes(0)) return dxHex(f, body, true);
  const pre = el("pre", "code dx-code");
  if (f.s <= 2 * 1024 * 1024) {
    const data = await api(`/kb/${dx.key}/decompiled/file?path=${encodeURIComponent(f.p)}`);
    pre.append(/\.(c|cs|cpp|cc|h|hpp|java|js|mjs|ts|gd|lua|py|rpy|hlsl|glsl|shader|gdshader)$/i.test(f.name) ? highlight(data.text) : data.text);
  } else {
    const response = await fetch(dxRaw(f.p), { headers: { Range: "bytes=0-524287" } });
    pre.append(await response.text());
    body.append(el("p", "hint", `Showing the first 512 KB of ${size(f.s)}. Download it for the rest.`));
  }
  body.append(pre);
}

async function dxHex(f, body, binary = false) {
  const response = await fetch(dxRaw(f.p), { headers: { Range: "bytes=0-4095" } });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const bytes = new Uint8Array(await response.arrayBuffer());
  // Text that only has an unusual extension reads better as text.
  if (!binary && bytes.length && !bytes.includes(0) && new TextDecoder("utf-8", { fatal: false }).decode(bytes).split("�").length < 4) return dxText(f, body);
  const lines = [];
  for (let i = 0; i < bytes.length; i += 16) {
    const row = bytes.subarray(i, i + 16);
    const hex = [...row].map((b) => b.toString(16).padStart(2, "0")).join(" ").padEnd(47, " ");
    lines.push(`${i.toString(16).padStart(8, "0")}  ${hex}  ${[...row].map((b) => b >= 32 && b < 127 ? String.fromCharCode(b) : ".").join("")}`);
  }
  body.append(el("p", "hint", `${f.k === "engine" ? "Engine/import file" : "Binary file"}: no preview for .${dxExt(f.name) || "?"} here, first ${bytes.length.toLocaleString()} of ${f.s.toLocaleString()} bytes shown.`),
    el("pre", "code dx-code", lines.join("\n")));
}

// 3D models: a WebGL view (drag to turn, scroll to zoom) with the model's textures overlaid. The
// look comes from what REA found (textures.mjs: Godot scenes/materials, the OBJ's .mtl, same-name
// images), from the file itself (glTF embedded textures/colours), from what you or the Master
// Agent saved, or from an image you pick here. No materials beyond base colour/texture, no animation.
const DX_IMG = /\.(png|jpe?g|webp|gif|bmp|tga|dds|pcx)$/i;
const DX_SHOWABLE = /\.(png|jpe?g|webp|gif|bmp)$/i;

// Files a model names (textures, glTF buffers, Half-Life *T.mdl, Quake's palette) → the matching
// file in the same binary's decompiled output: exact relative/res:// path first, then the same file
// name anywhere (closest folder wins), then the same name with another image extension.
function dxResolver(f) {
  const files = f.bin.files || [];
  const byPath = new Map(files.map((x) => [x.p, x]));
  const dir = f.p.slice(0, f.p.lastIndexOf("/"));
  const outRoot = Object.values(f.bin.tools || {}).map((t) => t.out).filter((o) => o && f.p.startsWith(o + "/")).sort((a, b) => b.length - a.length)[0] || dir;
  const norm = (p) => { const out = []; for (const seg of p.split("/")) { if (!seg || seg === ".") continue; if (seg === "..") out.pop(); else out.push(seg); } return out.join("/"); };
  const shared = (p) => { const a = p.split("/"), b = dir.split("/"); let n = 0; while (n < a.length && a[n] === b[n]) n++; return n; };
  const index = (list) => {
    const base = new Map(), stem = new Map();
    for (const x of list) {
      const name = x.p.split("/").pop().toLowerCase();
      (base.get(name) || base.set(name, []).get(name)).push(x);
      const st = name.replace(/\.[^.]+$/, "");
      (stem.get(st) || stem.set(st, []).get(st)).push(x);
    }
    return { base, stem };
  };
  const images = index(files.filter((x) => x.k === "image" && DX_IMG.test(x.p)));
  const all = index(files);
  const best = (list) => list?.length ? [...list].sort((a, b) => (DX_SHOWABLE.test(b.p) - DX_SHOWABLE.test(a.p)) || shared(b.p) - shared(a.p))[0] : null;
  const find = (ref, ix, image) => {
    if (!ref) return null;
    const r = String(ref).replace(/\\/g, "/").trim();
    const exact = r.startsWith("res://") ? `${outRoot}/${norm(r.slice(6))}` : norm(`${dir}/${r.replace(/^[a-z]:\//i, "")}`);
    const hit = byPath.get(exact);
    if (hit && (!image || DX_IMG.test(hit.p))) return hit;
    const name = r.split("/").pop().toLowerCase();
    return best(ix.base.get(name)) || (image ? best(ix.stem.get(name.replace(/\.[^.]+$/, ""))) : null);
  };
  const fetchFile = async (x) => { if (!x) return null; const r = await fetch(dxRaw(x.p)); return r.ok ? r.arrayBuffer() : null; };
  return {
    image: (ref) => find(ref, images, true)?.p || null,
    fetchRef: (ref) => fetchFile(find(ref, all, false)),
    find: (name) => fetchFile(best(all.base.get(String(name).toLowerCase()))),
    stem: f.name.replace(/\.[^.]+$/, ""),
  };
}

async function dxModel(f, ext, body) {
  if (f.s > 96 * 1024 * 1024) { body.append(el("p", "hint", `${size(f.s)} is too big to preview here. Download it and open it in Blender.`)); return dxHex(f, body); }
  const response = await fetch(dxRaw(f.p));
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const resolver = dxResolver(f);
  const mesh = await dxParseModel(ext, await response.arrayBuffer(), resolver);
  // Textures the file carries or names → something the viewer can load.
  const missingRefs = new Set();
  for (const s of mesh.surfaces) {
    if (s.texBlob) { s.texUrl = URL.createObjectURL(s.texBlob); dx.blobs.push(s.texUrl); delete s.texBlob; }
    if (!s.texUrl && !s.texCanvas && s.texRef) {
      // MD2 lists several skins: the first one that exists wins.
      const p = (s.texRefs || [s.texRef]).map((r) => resolver.image(r)).find(Boolean);
      if (p) { s.texUrl = dxRaw(p); s.texPath = p; } else missingRefs.add(s.texRef.split(/[\\/]/).pop());
    }
  }
  const info = el("p", "hint", `${mesh.vertices.toLocaleString()} vertices · ${mesh.faces.toLocaleString()} faces · ${mesh.surfaces.length} surface${mesh.surfaces.length === 1 ? "" : "s"}` +
    `${mesh.note ? ` · ${mesh.note}` : ""}${missingRefs.size ? ` · not in the decompiled files: ${[...missingRefs].slice(0, 6).join(", ")}${missingRefs.size > 6 ? "…" : ""}` : ""} · drag to turn, scroll to zoom`);
  const controls = el("div", "dx-model-controls");
  const canvas = el("canvas", "dx-canvas");
  const surfacesBox = el("ul", "dx-surfaces");
  const picker = el("div", "dx-picker");
  picker.hidden = true;
  body.append(info, controls, canvas, surfacesBox, picker);
  if (!mesh.surfaces.length) { canvas.remove(); return; }
  const viewer = dxViewer(canvas, mesh);
  if (!viewer) { body.append(el("p", "hint", "This browser has no WebGL2, so textures can't be shown.")); return; }
  dx.viewer = viewer;

  // Looks to choose from.
  let found = { candidates: [], saved: null };
  try { found = await api(`/kb/${dx.key}/decompiled/textures?path=${encodeURIComponent(f.p)}`); } catch (error) { info.append(` · texture lookup failed: ${error.message}`); }
  const looks = [...found.candidates];
  if (mesh.surfaces.some((s) => s.texUrl || s.texCanvas || s.color || s.col)) looks.push({ id: "embedded", source: "embedded",
    label: mesh.surfaces.some((s) => s.texCanvas || /^blob:/.test(s.texUrl || "")) ? "Built into the file (embedded textures)" : mesh.surfaces.some((s) => s.texPath) ? "From the model file (the textures it names)"
      : mesh.surfaces.some((s) => s.texUrl) ? "Built into the file (embedded textures)" : mesh.surfaces.some((s) => s.col) ? "Built into the file (vertex colours)" : "Built into the file (material colours)" });
  looks.push({ id: "none", label: "Plain (no texture)", source: "none" });
  const pick = looks.find((c) => c.source === "saved") || looks.find((c) => c.source === "scene") || looks.find((c) => c.source === "mtl")
    || looks.find((c) => c.id === "embedded") || looks.find((c) => c.source === "name") || looks.at(-1);
  let look = pick;
  let custom = null;

  const lookOf = (c, s) => {
    if (c?.source === "none") return { texture: null, color: [0.78, 0.78, 0.78, 1] };
    if (c?.source === "embedded") return { texture: s.texUrl || s.texCanvas || null, color: s.color || [1, 1, 1, 1], from: s.texPath || s.texRef || "" };
    const hit = c?.surfaces?.find((x) => x.index === s.index) || c?.surfaces?.find((x) => x.name && x.name === s.name);
    if (hit && (hit.texture || hit.color)) return { texture: hit.texture ? dxRaw(hit.texture) : null, color: hit.color || [1, 1, 1, 1], from: hit.texture || hit.material || "" };
    if (c?.texture) return { texture: dxRaw(c.texture), color: c.color || [1, 1, 1, 1], from: c.texture };
    if (c?.color) return { texture: null, color: c.color, from: c.material || "" };
    return { texture: s.texUrl || s.texCanvas || null, color: s.color || [0.78, 0.78, 0.78, 1] };
  };
  const apply = () => {
    const c = custom || look;
    const per = mesh.surfaces.map((s) => lookOf(c, s));
    viewer.setLook(per);
    surfacesBox.replaceChildren(...mesh.surfaces.map((s, i) => {
      const li = el("li");
      const sw = el("span", "dx-swatch");
      const [r, g, b] = per[i].color.map((x) => Math.round(Math.min(1, Math.max(0, x)) * 255));
      sw.style.background = `rgb(${r}, ${g}, ${b})`;
      if (typeof per[i].texture === "string" && DX_SHOWABLE.test(decodeURIComponent(per[i].texture.split("path=")[1] || "")) || /^(blob|data):/.test(per[i].texture || "")) {
        const im = el("img"); im.src = per[i].texture; im.alt = ""; sw.append(im);
      } else if (per[i].texture instanceof HTMLCanvasElement) {
        const c = document.createElement("canvas"); c.width = 22; c.height = 22;
        c.getContext("2d").drawImage(per[i].texture, 0, 0, 22, 22); sw.append(c);
      }
      li.append(sw, el("span", "dx-sname", `${s.index} · ${s.name || "surface"}`),
        el("span", "hint", per[i].texture ? (per[i].from ? per[i].from.split("/").pop() : "embedded texture") : per[i].from ? `colour from ${per[i].from.split("/").pop()}` : "colour"));
      return li;
    }));
    saveBtn.disabled = !custom;
  };

  const lookSelect = el("select");
  lookSelect.setAttribute("aria-label", "Texture source");
  const fillSelect = () => lookSelect.replaceChildren(...[...looks, ...(custom ? [custom] : [])].map((c) => {
    const o = el("option", "", c.label);
    o.value = c.id;
    o.selected = (custom || look) === c;
    return o;
  }));
  lookSelect.addEventListener("change", () => {
    if (lookSelect.value === "custom") return;
    look = looks.find((c) => c.id === lookSelect.value);
    custom = null;
    fillSelect();
    apply();
  });
  const modes = el("nav", "seg dx-modes");
  for (const [id, label] of [["textured", "Textured"], ["solid", "Solid"], ["wire", "Wireframe"]]) {
    const b = el("button", "", label);
    b.type = "button";
    b.setAttribute("aria-pressed", String(id === "textured"));
    b.addEventListener("click", () => { for (const x of modes.children) x.setAttribute("aria-pressed", String(x === b)); viewer.setMode(id); });
    modes.append(b);
  }
  const chooseBtn = el("button", "button", "Choose texture…");
  chooseBtn.type = "button";
  chooseBtn.addEventListener("click", () => { picker.hidden = !picker.hidden; if (!picker.hidden) fillPicker(); });
  const saveBtn = el("button", "button primary", "Save for this model");
  saveBtn.type = "button";
  saveBtn.title = "Remember this look for the model (the agent and later visits use it)";
  saveBtn.addEventListener("click", async () => {
    try {
      const saved = await api(`/kb/${dx.key}/decompiled/textures`, { method: "POST", json: { model: f.p, texture: custom.texture || "", surfaces: custom.surfaces || [], note: "picked on the page", by: "user" } });
      const entry = { id: "saved", label: "Saved by you", source: "saved", ...saved.saved };
      const old = looks.findIndex((c) => c.id === "saved");
      if (old >= 0) looks.splice(old, 1);
      looks.unshift(entry);
      look = entry; custom = null;
      fillSelect(); apply();
      clearBtn.hidden = false;
      toast("Saved the texture for this model");
    } catch (error) { toast(error.message, "error"); }
  });
  const clearBtn = el("button", "button danger", "Forget saved");
  clearBtn.type = "button";
  clearBtn.hidden = !found.saved;
  clearBtn.addEventListener("click", async () => {
    try {
      await api(`/kb/${dx.key}/decompiled/textures?path=${encodeURIComponent(f.p)}`, { method: "DELETE" });
      const old = looks.findIndex((c) => c.id === "saved");
      if (old >= 0) looks.splice(old, 1);
      look = looks.find((c) => c.source === "scene") || looks.find((c) => c.source === "mtl") || looks.find((c) => c.id === "embedded") || looks.at(-1);
      custom = null; clearBtn.hidden = true;
      fillSelect(); apply();
    } catch (error) { toast(error.message, "error"); }
  });
  const hermesBtn = el("button", "button", "Find textures with Hermes");
  hermesBtn.type = "button";
  hermesBtn.title = "Ask the Master Agent to work out which textures belong to this game's models (from scenes, materials, scripts and import files) and save them";
  hermesBtn.addEventListener("click", () => dxFindTextures(f, hermesBtn));
  const nothing = !looks.some((c) => ["saved", "scene", "mtl"].includes(c.source)) && !mesh.surfaces.some((s) => s.texUrl || s.texCanvas || s.col);
  if (nothing) { hermesBtn.classList.add("primary"); saveBtn.classList.remove("primary"); }
  const row1 = el("div", "dx-model-row");
  const label = el("label", "dx-look");
  label.append("Look ", lookSelect);
  row1.append(label, modes);
  const row2 = el("div", "step-actions");
  row2.append(chooseBtn, saveBtn, clearBtn, hermesBtn);
  controls.append(row1, row2);
  if (nothing) controls.append(el("p", "hint", "REA found no texture for this model in the game's scenes, materials or files. Pick one yourself, or let Hermes look through the scenes, scripts and import files and link them."));

  // Image picker: the game's images, closest names first; apply to the whole model or one surface.
  const fillPicker = () => {
    const images = (f.bin.files || []).filter((x) => x.k === "image" && DX_IMG.test(x.p)).map((x) => ({ ...x, name: x.v.split("/").pop() }));
    const stem = f.name.toLowerCase().replace(/\.[^.]+$/, "").replace(/[^a-z0-9]+/g, "");
    const dir = f.p.slice(0, f.p.lastIndexOf("/"));
    const score = (x) => {
      const n = x.name.toLowerCase().replace(/\.[^.]+$/, "").replace(/[^a-z0-9]+/g, "");
      return (n === stem ? 0 : n.includes(stem) || stem.includes(n) ? 1 : 3) + (x.p.startsWith(dir + "/") ? 0 : 1);
    };
    const target = el("select");
    target.setAttribute("aria-label", "Apply to");
    target.append(Object.assign(el("option", "", "Whole model"), { value: "" }),
      ...mesh.surfaces.map((s) => Object.assign(el("option", "", `Surface ${s.index} · ${s.name || "surface"}`), { value: String(s.index) })));
    const search = el("input");
    search.type = "search";
    search.placeholder = "Filter images by name or folder";
    const grid = el("ul", "dx-grid dx-pick-grid");
    const draw = () => {
      const q = search.value.trim().toLowerCase();
      const list = images.filter((x) => !q || x.v.toLowerCase().includes(q)).sort((a, b) => score(a) - score(b) || a.v.localeCompare(b.v)).slice(0, 120);
      grid.replaceChildren(...list.map((x) => {
        const li = el("li");
        const b = el("button", "dx-tile");
        b.type = "button";
        b.title = x.v;
        const th = el("span", "dx-thumb k-image");
        if (DX_SHOWABLE.test(x.p)) { const im = el("img"); im.loading = "lazy"; im.alt = ""; im.src = dxRaw(x.p); th.append(im); }
        else th.textContent = dxExt(x.p);
        b.append(th, el("span", "dx-tname", x.name));
        b.addEventListener("click", () => {
          const base = custom || look;
          custom ||= { id: "custom", label: "Your pick (not saved yet)", source: "custom",
            texture: ["scene", "mtl", "name", "saved"].includes(base?.source) ? base.texture : undefined,
            surfaces: ["scene", "mtl", "saved"].includes(base?.source) ? (base.surfaces || []).map((s) => ({ ...s })) : [] };
          if (target.value === "") { custom.texture = x.p; custom.surfaces = []; }
          else {
            const s = mesh.surfaces.find((y) => String(y.index) === target.value);
            custom.surfaces = custom.surfaces.filter((y) => y.index !== s.index && !(y.name && y.name === s.name));
            custom.surfaces.push({ index: s.index, ...(s.name ? { name: s.name } : {}), texture: x.p });
          }
          fillSelect(); apply();
        });
        li.append(b);
        return li;
      }));
      if (!list.length) grid.append(el("li", "hint", "No matching images."));
    };
    search.addEventListener("input", draw);
    const head = el("div", "dx-picker-head");
    const lab = el("label", "dx-look");
    lab.append("Apply to ", target);
    head.append(lab, search);
    picker.replaceChildren(head, grid);
    draw();
  };

  fillSelect();
  apply();
}

// One Master Agent session per game (kind "textures") that links textures to every model REA
// couldn't, saving each through the textures API.
async function dxFindTextures(f, button) {
  const target = current();
  if (!target || target.type !== "folder") return;
  button.disabled = true;
  try {
    const key = dx.key;
    const [open, { sessions }] = await Promise.all([api(`/kb/${key}/decompiled/textures/unresolved`), api(`/kb/${key}/sessions`)]);
    const base = "http://127.0.0.1:8976/REA/api";
    const list = open.models.slice(0, 150).map((m) => `- ${m.path}${m.guesses.length ? ` (same-name images: ${m.guesses.join(", ")})` : ""}`).join("\n") || "- none left: just verify and improve the current links";
    const objective = `Link the textures (or colours) of the 3D models of the game "${target.name}" (${target.path}, read-only) that REA couldn't link automatically, ` +
      `and save every link so the REA page's model viewer shows the models textured.\nStart with ${f.p}.\n` +
      `Knowledge base KEY ${key}; decompiled/ folder: ${dx.dir} (paths below are relative to it).\n` +
      `Models without a link (${open.unresolved} of ${open.total}${open.models.length < open.unresolved ? `, first ${open.models.length} listed` : ""}):\n${list}\n\n` +
      `Tools: GET ${base}/kb/${key}/decompiled/textures?path=P shows what REA already found for a model; GET ${base}/kb/${key}/decompiled/tree lists every recovered file ` +
      `(p = path, k = image/model/scene/...); GET ${base}/kb/${key}/decompiled/file?path=P reads a text file. ` +
      `Evidence, best first: scenes and resources (.tscn/.escn/.tres: mesh = ExtResource → material/N, material_override, surface_material_override/N → albedo_texture or shader texture params), ` +
      `scripts that set materials or textures at run time (load("res://….png"), set_surface_material, material_override, Unity/Unreal equivalents), .import files, shader code, ` +
      `texture atlases (compare the mesh's UV range with the atlas), and name/folder conventions only as a last resort.\n` +
      `Save each model with: curl -s -X POST -H 'X-REA: 1' -H 'Content-Type: application/json' -d '{"model":"P","texture":"IMAGE_P","surfaces":[{"index":0,"texture":"IMAGE_P"},{"index":1,"color":[r,g,b,1]}],"note":"evidence: …","by":"agent"}' ${base}/kb/${key}/decompiled/textures ` +
      `(surfaces: index = the model's surface order: OBJ usemtl order (also matched by "name"), glTF primitive order; use "texture" alone when one image covers the whole model). ` +
      `When a model really has no texture (vertex colours, procedural shader, generated at run time), save {"model":"P","none":true,"note":"why","by":"agent"}. ` +
      `Always put the evidence in "note" and write "guess" in it for name-only matches. Check GET ${base}/kb/${key}/decompiled/textures/unresolved as you go; ` +
      `when it is empty, write what you learned about how this game assigns materials to the knowledge base (kb.py note ${key} --session) and run $R goal done with the counts as evidence.` +
      await investigationGuide(target);
    const text = `/goal infinite ${objective}`;
    const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(async (r) => { const d = await r.json().catch(() => ({})); if (!r.ok) throw Object.assign(new Error(d.detail || `HTTP ${r.status}`), { status: r.status }); return d; });
    let session = sessions.find((x) => x.kind === "textures")?.id || "";
    if (session) {
      try { await post(`/Agent/api/sessions/${encodeURIComponent(session)}/message`, { text, on_busy: "queue", ...modelFor("textures") }); }
      catch (error) { if (error.status !== 404) throw error; session = ""; }
    }
    const resumed = Boolean(session);
    if (!session) {
      session = (await post("/Agent/api/sessions", { prompt: text, ...modelFor("textures") })).id;
      await recordSession(key, session, "textures", `Textures: ${target.name}`);
    }
    toast(`${resumed ? "Resumed" : "Started"} the texture session (${open.unresolved} model${open.unresolved === 1 ? "" : "s"} to link). It's in the Sessions tab; reopen a model to see what it saved.`);
    window.open(`/Agent/?s=${encodeURIComponent(session)}`, "_blank", "noopener");
  } catch (error) { toast(`Couldn't reach the Master Agent: ${error.message}`, "error"); }
  finally { button.disabled = false; }
}

// --- WebGL2 view: one draw per surface; textured, solid (lit colour) or wireframe.
function dxViewer(canvas, mesh) {
  const gl = canvas.getContext("webgl2", { antialias: true });
  if (!gl) return null;
  const sh = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
  const prog = gl.createProgram();
  gl.attachShader(prog, sh(gl.VERTEX_SHADER, `#version 300 es
in vec3 p; in vec2 t; in vec3 n; in vec4 c;
uniform mat4 M; uniform vec3 S;
out vec2 vt; out vec3 vn; out vec4 vc;
void main() { vec4 q = M * vec4(p, 1.0); gl_Position = vec4(q.x * S.x, q.y * S.y, -q.z * S.z, 1.0); vt = t; vn = mat3(M) * n; vc = c; }`));
  gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, `#version 300 es
precision mediump float;
in vec2 vt; in vec3 vn; in vec4 vc;
uniform sampler2D tex; uniform vec4 col; uniform int useTex; uniform int lit;
out vec4 o;
void main() {
  vec4 c = col * vc;
  if (useTex == 1) { c *= texture(tex, vt); if (c.a < 0.4) discard; }
  float l = lit == 1 ? 0.38 + 0.62 * abs(dot(normalize(vn), normalize(vec3(0.35, 0.6, 0.72)))) : 1.0;
  o = vec4(c.rgb * l, 1.0);
}`));
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return null;
  const loc = (n) => gl.getUniformLocation(prog, n);
  const U = { M: loc("M"), S: loc("S"), tex: loc("tex"), col: loc("col"), useTex: loc("useTex"), lit: loc("lit") };
  const buf = (data, attr, size) => {
    const b = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, b);
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
    const a = gl.getAttribLocation(prog, attr);
    gl.enableVertexAttribArray(a);
    gl.vertexAttribPointer(a, size, gl.FLOAT, false, 0, 0);
    return b;
  };
  let min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
  const parts = mesh.surfaces.map((s) => {
    for (let i = 0; i < s.pos.length; i += 3) for (let a = 0; a < 3; a++) { min[a] = Math.min(min[a], s.pos[i + a]); max[a] = Math.max(max[a], s.pos[i + a]); }
    const vao = gl.createVertexArray();
    gl.bindVertexArray(vao);
    const bufs = [buf(s.pos, "p", 3), buf(s.uv || new Float32Array(s.pos.length / 3 * 2), "t", 2), buf(s.nrm, "n", 3)];
    // Vertex colours (PLY, glTF COLOR_0, OBJ "v x y z r g b", FBX, COLLADA); white when there are none.
    const cLoc = gl.getAttribLocation(prog, "c");
    if (s.col) bufs.push(buf(s.col, "c", 4)); else { gl.disableVertexAttribArray(cLoc); gl.vertexAttrib4f(cLoc, 1, 1, 1, 1); }
    // Wireframe: each triangle's three edges.
    const lines = new Float32Array(s.pos.length * 2);
    for (let i = 0, j = 0; i + 8 < s.pos.length; i += 9) for (const [a, b] of [[0, 3], [3, 6], [6, 0]]) {
      lines.set(s.pos.subarray(i + a, i + a + 3), j); lines.set(s.pos.subarray(i + b, i + b + 3), j + 3); j += 6;
    }
    const wire = gl.createVertexArray();
    gl.bindVertexArray(wire);
    bufs.push(buf(lines, "p", 3));
    gl.bindVertexArray(null);
    return { vao, wire, count: s.pos.length / 3, lines: lines.length / 3, bufs, look: { texture: null, color: [0.78, 0.78, 0.78, 1] } };
  });
  const center = min.map((v, a) => (v + max[a]) / 2);
  const radius = Math.max(...max.map((v, a) => (v - min[a]) / 2), 1e-6) * 1.15;
  const textures = new Map();
  // A texture source: a URL (raw tree file, blob, data:) or a canvas (embedded/paletted skins).
  const texture = (src) => {
    if (!src) return null;
    if (textures.has(src)) return textures.get(src);
    const entry = { tex: gl.createTexture(), ready: false };
    textures.set(src, entry);
    const upload = (img) => {
      if (dead) return;
      gl.bindTexture(gl.TEXTURE_2D, entry.tex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
      gl.generateMipmap(gl.TEXTURE_2D);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
      // Small textures are usually pixel art.
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, (img.naturalWidth || img.width) <= 128 ? gl.NEAREST : gl.LINEAR);
      entry.ready = true;
      frame();
    };
    if (src instanceof HTMLCanvasElement) { upload(src); return entry; }
    const ext = (decodeURIComponent(String(src).split("path=")[1] || "").match(/\.([a-z0-9]+)$/i)?.[1] || "").toLowerCase();
    if (DX_DECODE_EXT.has(ext)) {
      fetch(src).then((r) => r.arrayBuffer()).then((b) => upload(dxDecodeImage(ext, b))).catch(() => {});
      return entry;
    }
    const img = new Image();
    img.onload = () => upload(img);
    img.src = src;
    return entry;
  };
  let yaw = 0.7, pitch = 0.35, zoom = 1, mode = "textured", dead = false, queued = false;
  const draw = () => {
    queued = false;
    if (dead) return;
    const w = canvas.clientWidth, h = canvas.clientHeight, dpr = devicePixelRatio || 1;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);
    gl.useProgram(prog);
    const cy = Math.cos(yaw), sy = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
    // M = Rx(pitch) · Ry(yaw) · T(-center), column-major.
    const r = [cy, sp * sy, -cp * sy, 0, 0, cp, sp, 0, sy, -sp * cy, cp * cy, 0];
    const t = [0, 1, 2].map((i) => -(r[i] * center[0] + r[4 + i] * center[1] + r[8 + i] * center[2]));
    gl.uniformMatrix4fv(U.M, false, [...r, t[0], t[1], t[2], 1]);
    const k = zoom / radius, aspect = w / Math.max(1, h);
    gl.uniform3f(U.S, aspect > 1 ? k / aspect : k, aspect > 1 ? k : k * aspect, 0.5 / radius);
    gl.uniform1i(U.tex, 0);
    const accent = getComputedStyle(document.documentElement).getPropertyValue("--blue").trim().match(/#(..)(..)(..)/)?.slice(1).map((x) => parseInt(x, 16) / 255) || [0.49, 0.7, 0.86];
    for (const part of parts) {
      if (mode === "wire") {
        gl.bindVertexArray(part.wire);
        gl.uniform1i(U.useTex, 0); gl.uniform1i(U.lit, 0);
        gl.uniform4f(U.col, accent[0], accent[1], accent[2], 1);
        gl.drawArrays(gl.LINES, 0, part.lines);
        continue;
      }
      const tx = mode === "textured" && texture(part.look.texture);
      gl.bindVertexArray(part.vao);
      gl.uniform1i(U.lit, 1);
      if (tx?.ready) { gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, tx.tex); gl.uniform1i(U.useTex, 1); }
      else gl.uniform1i(U.useTex, 0);
      const c = part.look.color;
      gl.uniform4f(U.col, c[0], c[1], c[2], c[3] ?? 1);
      gl.drawArrays(gl.TRIANGLES, 0, part.count);
    }
    gl.bindVertexArray(null);
  };
  const frame = () => { if (!queued) { queued = true; requestAnimationFrame(draw); } };
  let drag = null;
  canvas.addEventListener("pointerdown", (ev) => { drag = [ev.clientX, ev.clientY]; canvas.setPointerCapture(ev.pointerId); });
  canvas.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    yaw += (ev.clientX - drag[0]) * 0.01; pitch = Math.max(-1.5, Math.min(1.5, pitch + (ev.clientY - drag[1]) * 0.01));
    drag = [ev.clientX, ev.clientY];
    frame();
  });
  canvas.addEventListener("pointerup", () => { drag = null; });
  canvas.addEventListener("wheel", (ev) => { ev.preventDefault(); zoom = Math.max(0.2, Math.min(30, zoom * (ev.deltaY < 0 ? 1.15 : 1 / 1.15))); frame(); }, { passive: false });
  const resize = new ResizeObserver(frame);
  resize.observe(canvas);
  frame();
  return {
    setLook(per) { per.forEach((l, i) => { if (parts[i]) parts[i].look = l; }); frame(); },
    setMode(m) { mode = m; frame(); },
    destroy() {
      dead = true;
      resize.disconnect();
      for (const p of parts) { for (const b of p.bufs) gl.deleteBuffer(b); gl.deleteVertexArray(p.vao); gl.deleteVertexArray(p.wire); }
      for (const t of textures.values()) gl.deleteTexture(t.tex);
      gl.getExtension("WEBGL_lose_context")?.loseContext();
    },
  };
}

// Extract a Windows binary's embedded resources (decomp.mjs → peres.mjs).
async function dxExtract(rel, button) {
  if (!dx.key || !rel) return;
  const label = button?.textContent;
  if (button) { button.disabled = true; button.textContent = "Extracting…"; }
  try {
    const result = await api(`/kb/${dx.key}/decompiled/extract`, { method: "POST", json: { rel } });
    toast(`${rel}: ${result.files ? `${result.files} files (${result.summary})` : "no embedded resources"}`);
    $("dx-extract").hidden = true;
    loadDecomp();
  } catch (error) { toast(error.message, "error"); }
  finally { if (button) { button.disabled = false; button.textContent = label; } }
}

$("dx-extract-toggle").addEventListener("click", async () => {
  const box = $("dx-extract");
  box.hidden = !box.hidden;
  if (box.hidden) return;
  const target = current();
  const select = $("dx-extract-file");
  select.replaceChildren(el("option", "", "Loading the folder's files…"));
  $("dx-extract-go").disabled = true;
  try {
    const inv = await api(`/targets/${target.id}/inventory`);
    const pe = (inv.files || []).filter((f) => /^(PE|\.NET)/.test(f.format || "")).sort((a, b) => (a.rank ?? 99) - (b.rank ?? 99) || b.size - a.size);
    select.replaceChildren(...(pe.length ? pe.slice(0, 500).map((f) => { const o = el("option", "", `${f.rel} (${f.format}, ${size(f.size)})`); o.value = f.rel; return o; })
      : [el("option", "", "No Windows .exe/.dll in this folder")]));
    $("dx-extract-go").disabled = !pe.length;
  } catch (error) { select.replaceChildren(el("option", "", error.message)); $("dx-extract-go").disabled = true; }
});
$("dx-extract-go").addEventListener("click", () => dxExtract($("dx-extract-file").value, $("dx-extract-go")));
let dxFilterTimer;
$("dx-filter").addEventListener("input", () => {
  clearTimeout(dxFilterTimer);
  dxFilterTimer = setTimeout(() => { dx.query = $("dx-filter").value.trim().toLowerCase(); dx.sel = null; renderExplorer(); }, 200);
});
$("dx-engine").addEventListener("change", () => { dx.engine = $("dx-engine").checked; renderExplorer(); });
for (const b of document.querySelectorAll("#decomp-views button")) b.addEventListener("click", () => {
  for (const x of document.querySelectorAll("#decomp-views button")) x.setAttribute("aria-pressed", String(x === b));
  $("decomp-explorer").hidden = b.dataset.dview !== "explorer";
  $("decomp-progress").hidden = b.dataset.dview !== "progress";
});

// ------------------------------------------------------------------------- test runs --
const RUN_STATE = { passed: ["complete", "passed"], failed: ["failed", "failed"], running: ["partial", "running"], inconclusive: ["partial", "inconclusive"],
  static: ["", "built only"] };
function runBadge(r) { const [cls, label] = RUN_STATE[r.state] || ["", r.state]; return el("span", `badge ${cls}${r.state === "running" ? " live" : ""}`, label); }
const PHASES = { build: "building", run: "running the game headless", check: "checking syntax" };
const shotsShut = new Set();     // run ids whose screenshot strip the reader closed (cards re-render while a run goes)

function shotUrl(r, s) { return `${API}/mods/${r.mod}/runs/${r.id}/shots/${s.file}`; }
function shotStrip(r) {
  const box = el("details", "run-shots");
  box.open = !shotsShut.has(r.id);
  box.addEventListener("toggle", () => { if (box.open) shotsShut.delete(r.id); else shotsShut.add(r.id); });
  const mine = r.shots.filter((s) => !s.base), base = r.shots.filter((s) => s.base);
  box.append(el("summary", "", `Screenshots: ${mine.length} with the mod${base.length ? `, ${base.length} without` : ""}`));
  const group = (title, list) => {
    if (!list.length) return;
    const grid = el("div", "shot-grid");
    for (const s of list) {
      const fig = el("figure");
      const b = el("button");
      b.type = "button";
      b.title = s.label;
      const img = el("img");
      img.loading = "lazy"; img.alt = s.label; img.src = shotUrl(r, s);
      b.append(img);
      b.addEventListener("click", () => openShot(r, list, list.indexOf(s), title));
      fig.append(b, el("figcaption", "", `${s.at}s · ${s.label}`));
      grid.append(fig);
    }
    box.append(el("h4", "", title), grid);
  };
  group("With the mod", mine);
  group("Unmodified game (baseline)", base);
  return box;
}

let shotView = null;
function openShot(r, list, i, title) {
  shotView = { r, list, i, title };
  const s = list[i];
  $("shot-img").src = shotUrl(r, s);
  $("shot-img").alt = s.label;
  $("shot-open").href = shotUrl(r, s);
  $("shot-cap").textContent = `${title} · ${i + 1} of ${list.length} · ${s.at}s · ${s.label}`;
  $("shot-prev").disabled = i === 0;
  $("shot-next").disabled = i === list.length - 1;
  if (!$("shot-viewer").open) $("shot-viewer").showModal();
}
$("shot-prev").addEventListener("click", () => shotView && shotView.i > 0 && openShot(shotView.r, shotView.list, shotView.i - 1, shotView.title));
$("shot-next").addEventListener("click", () => shotView && shotView.i < shotView.list.length - 1 && openShot(shotView.r, shotView.list, shotView.i + 1, shotView.title));
$("shot-close").addEventListener("click", () => $("shot-viewer").close());
$("shot-viewer").addEventListener("click", (event) => { if (event.target === $("shot-viewer")) $("shot-viewer").close(); });
$("shot-viewer").addEventListener("keydown", (event) => {
  if (event.key === "ArrowLeft") $("shot-prev").click();
  else if (event.key === "ArrowRight") $("shot-next").click();
});
// File list: hide it with ‹, bring it back with ☰ (remembered per browser).
function setSidebar(open, focus = false) {
  document.querySelector(".layout").classList.toggle("sidebar-hidden", !open);
  $("sidebar-open").hidden = open;
  $("sidebar-open").setAttribute("aria-expanded", String(open));
  try { localStorage.setItem("rea.sidebar", open ? "open" : "hidden"); } catch {}
  if (focus) (open ? $("sidebar-close") : $("sidebar-open")).focus();
}
$("sidebar-close").addEventListener("click", () => setSidebar(false, true));
$("sidebar-open").addEventListener("click", () => setSidebar(true, true));
{ let saved = "open"; try { saved = localStorage.getItem("rea.sidebar") || "open"; } catch {} setSidebar(saved !== "hidden"); }
try { $("testrun-shots").checked = localStorage.getItem("rea.testrunShots") === "1"; } catch {}
$("testrun-shots").addEventListener("change", () => { try { localStorage.setItem("rea.testrunShots", $("testrun-shots").checked ? "1" : "0"); } catch {} });

function runCard(r, { mod = "", withDelete = false } = {}) {
  const item = el("li", r.state === "failed" ? "stale" : "");
  const top = el("div", "sess-top");
  top.append(runBadge(r), el("strong", "", `${r.modName || mod} · test run ${when(r.started)}`));
  if (r.state === "running") top.append(el("span", "hint", `${r.phase === "run" && r.screenshots ? "running the game on a virtual display" : PHASES[r.phase] || r.phase}…`));
  if (r.agent?.session) {
    const a = el("a", "button", "Agent session");
    a.href = `/Agent/?s=${encodeURIComponent(r.agent.session)}`; a.target = "_blank"; a.rel = "noopener";
    top.append(a);
  }
  if (withDelete && r.state !== "running") {
    const del = el("button", "button danger", "Delete");
    del.type = "button";
    del.addEventListener("click", async () => {
      try { await api(`/mods/${r.mod}/runs/${r.id}`, { method: "DELETE" }); loadLogs(); } catch (error) { toast(error.message, "error"); }
    });
    top.append(del);
  }
  item.append(top);
  const facts = el("div", "sess-facts");
  const fact = (t) => facts.append(el("span", "", t));
  if (r.finished) fact(`took ${Math.round((r.finished - r.started) / 1000)}s`);
  fact(`ran ${r.timeout}s ${r.screenshots ? "on a virtual display, with screenshots" : "headless"}`);
  fact(r.by === "agent" ? "started by the agent" : "started by you");
  if (r.coverage?.total) fact(`${r.coverage.loaded.length} of ${r.coverage.total} mod file(s) loaded`);
  if (r.baselineErrorCount > 0) fact(`${r.baselineErrorCount} existing game error type(s) excluded`);
  if (r.killed?.length) fact(`killed ${r.killed.length} leftover process${r.killed.length === 1 ? "" : "es"}`);
  if (r.agent?.queued && !r.agent.session) fact("agent asked to fix it (waiting for the Agent service)");
  if (r.agent?.error) fact(`agent request failed: ${r.agent.error}`);
  if (Array.isArray(r.proof)) fact(`${r.proof.length} [PROOF] milestone${r.proof.length === 1 ? "" : "s"} seen`);
  item.append(facts);
  if (r.why) item.append(el("p", "hint", `Why: ${r.why}`));
  if (r.summary) item.append(el("p", "sess-msg", r.summary));
  if (r.errors?.length) {
    const box = el("details");
    box.append(el("summary", "", `${r.errorCount || r.errors.length} new error${(r.errorCount || r.errors.length) === 1 ? "" : "s"}`));
    const list = el("ul", "run-errors");
    for (const e of r.errors.slice(0, 50)) list.append(el("li", "", e));
    box.append(list);
    item.append(box);
  }
  if (r.shots?.length) item.append(shotStrip(r));
  else if (r.screenshots && r.state !== "running" && r.state !== "static") item.append(el("p", "hint", "No screenshots were taken (the game didn't get as far as showing a frame; see run.log)."));
  if (r.files?.length) {
    const row = el("div", "tool-row");
    for (const f of r.files) {
      const b = el("button", "button", f);
      b.type = "button";
      b.addEventListener("click", () => showLog(r.mod, `runs/${r.id}/${f}`));
      row.append(b);
    }
    item.append(row);
  }
  return item;
}

// Log panes keep the reader's scroll spot; they follow the tail while a job runs (or when already at the bottom).
function setLog(pre, text, follow) {
  if (pre.textContent === text) return;
  const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
  const top = pre.scrollTop;
  pre.textContent = text;
  pre.scrollTop = follow || atBottom ? pre.scrollHeight : top;
}
try { $("build-log-box").open = localStorage.getItem("rea.buildLogOpen") === "1"; } catch {}
$("build-log-box").addEventListener("toggle", () => {
  try { localStorage.setItem("rea.buildLogOpen", $("build-log-box").open ? "1" : "0"); } catch {}
  if ($("build-log-box").open) $("build-log").scrollTop = $("build-log").scrollHeight;
});
$("testrun-log-close").addEventListener("click", (event) => { event.preventDefault(); $("testrun-log-box").hidden = true; $("testrun-log").textContent = ""; });

let runTimer;
async function loadTestRun() {
  clearTimeout(runTimer);
  const m = state.modData;
  if (!m) return;
  try {
    const { runs, active } = await api(`/mods/${m.id}/runs`);
    if (state.modData?.id !== m.id) return;
    const latest = active || runs[0];
    $("testrun-status").replaceChildren(...(latest ? [el("ul", "sess-list decomp-list", null)] : [el("p", "hint", "No test runs yet.")]));
    if (latest) $("testrun-status").firstChild.append(runCard({ ...latest, files: runs.find((x) => x.id === latest.id)?.files || [] }));
    $("do-testrun").disabled = Boolean(active) || Boolean(m.running);
    if (active) runTimer = setTimeout(loadTestRun, 2500);
    else if (m.running === "testrun") refreshMod();
  } catch (error) { $("testrun-status").replaceChildren(el("p", "error", error.message)); }
}

$("do-testrun").addEventListener("click", async () => {
  const m = state.modData;
  $("do-testrun").disabled = true;
  try {
    const screenshots = $("testrun-shots").checked;
    await api(`/mods/${m.id}/testrun`, { method: "POST", json: { timeout: Number($("testrun-timeout").value), agentOnFail: $("testrun-agent").checked, screenshots } });
    toast(`Test run started: build → syntax check → ${screenshots ? "run on a virtual display with screenshots" : "headless run"}`);
    await refreshMod();
    loadTestRun();
  } catch (error) { toast(error.message, "error"); $("do-testrun").disabled = false; }
});
$("testrun-logs").addEventListener("click", () => { state.mod = null; clearTimeout(modTimer); clearTimeout(runTimer); renderTarget(); setFolderTab("logs"); });

// ------------------------------------------------------------------------------ logs tab --
async function loadLogs() {
  const target = current();
  if (!target || target.type !== "folder" || state.fvTab !== "logs") return;
  try {
    const { runs, jobLogs } = await api(`/mods/logs?folder=${encodeURIComponent(target.id)}`);
    if (current()?.id !== target.id || state.fvTab !== "logs") return;
    const items = runs.map((r) => runCard(r, { withDelete: true }));
    for (const j of jobLogs) {
      const item = el("li");
      const top = el("div", "sess-top");
      top.append(el("span", "badge", j.name.replace(/\.log$/, "")), el("strong", "", `${j.modName} · ${j.name}`));
      const view = el("button", "button", "View");
      view.type = "button";
      view.addEventListener("click", () => showLog(j.mod, j.name));
      const del = el("button", "button danger", "Delete");
      del.type = "button";
      del.addEventListener("click", async () => {
        try { await api(`/mods/${j.mod}/logs/${j.name}`, { method: "DELETE" }); loadLogs(); } catch (error) { toast(error.message, "error"); }
      });
      top.append(view, del);
      item.append(top, el("div", "sess-facts", `${size(j.size)} · updated ${when(j.mtime)}`));
      items.push(item);
    }
    $("logs-list").replaceChildren(...(items.length ? items : [el("li", "empty", "No logs yet. Test runs and the mods' decompile/build logs show up here.")]));
    if (runs.some((r) => r.state === "running")) setTimeout(loadLogs, 3000);
  } catch (error) { $("logs-list").replaceChildren(el("li", "error", error.message)); }
}

async function showLog(mod, rel) {
  try {
    const data = await api(`/mods/${mod}/file?path=${encodeURIComponent(rel)}`);
    const inLogs = state.fvTab === "logs" && !state.mod;
    const viewer = inLogs ? "logs" : null;
    if (!viewer) {
      const box = $("testrun-log-box");
      box.hidden = false; box.open = true;
      $("testrun-log-meta").textContent = rel.split("/").pop();
      $("testrun-log").textContent = data.text || "(empty)";
      $("testrun-log").scrollTop = $("testrun-log").scrollHeight;
      box.scrollIntoView({ block: "nearest" });
      return;
    }
    $("logs-viewer").hidden = false;
    $("logs-path").replaceChildren(el("code", "", data.path));
    $("logs-code").textContent = data.text || "(empty)";
    $("logs-viewer").scrollIntoView({ block: "nearest" });
  } catch (error) { toast(error.message, "error"); }
}
$("logs-refresh").addEventListener("click", loadLogs);
$("logs-close").addEventListener("click", () => { $("logs-viewer").hidden = true; });
$("logs-clear").addEventListener("click", async () => {
  const target = current();
  if (!confirm(`Delete every test run and job log of ${target.name}'s mods?`)) return;
  try { const { deleted } = await api(`/mods/logs?folder=${encodeURIComponent(target.id)}`, { method: "DELETE" }); toast(`Deleted ${deleted} log${deleted === 1 ? "" : "s"}`); loadLogs(); }
  catch (error) { toast(error.message, "error"); }
});
$("decomp-refresh").addEventListener("click", loadDecomp);
$("decomp-open").addEventListener("click", () => decompData && openOnHost(decompData.dir));
$("decomp-address").addEventListener("click", () => decompData && addressDecompErrors(decompData.key));
$("decomp-stop").addEventListener("click", () => { repairStop = true; repairStatus("Stopping after the current Rocsy run…"); });

// ---------------------------------------------------------- folders on this computer --
// Mounting goes through the gateway (/api/rea/...) to the host helper: the REA container can't
// see or mount host folders itself.
async function gw(path, options = {}) {
  const headers = { "X-BB-Action": "1", ...(options.headers || {}) };
  if (options.json !== undefined) { headers["Content-Type"] = "application/json"; options.body = JSON.stringify(options.json); }
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw Object.assign(new Error(data.error || `HTTP ${response.status}`), { data, status: response.status });
  return data;
}

let hostPath = "";
async function loadMounted() {
  const list = $("mounted-list");
  try {
    const data = await gw("/api/rea/folders");
    list.replaceChildren(...(data.folders.length ? data.folders.map((f) => {
      const item = el("li");
      const info = el("span");
      info.append(el("strong", "", f.slug), document.createTextNode(" "), el("code", "", f.path));
      if (!f.mounted) info.append(document.createTextNode(" "), el("span", "mount-state", "not mounted yet"));
      const remove = el("button", "button danger", "Remove");
      remove.type = "button";
      remove.addEventListener("click", () => changeFolders("remove", { slug: f.slug }, `Remove ${f.path}? REA restarts and closes open files.`));
      item.append(info, remove);
      return item;
    }) : [el("li", "", "No folders yet.")]));
  } catch (error) { list.replaceChildren(el("li", "error", error.message)); }
}

async function browseHost(path) {
  const list = $("host-list");
  list.replaceChildren(el("li", "", "Loading…"));
  try {
    const data = await gw(`/api/rea/browse?path=${encodeURIComponent(path || "")}&hidden=${$("host-hidden").checked ? 1 : 0}`);
    hostPath = data.path;
    $("host-path").textContent = data.path;
    $("host-up").disabled = !data.parent;
    $("host-up").onclick = () => browseHost(data.parent);
    $("host-refused").textContent = data.refused;
    $("host-add").disabled = Boolean(data.refused || data.added);
    $("host-add").textContent = data.added ? "Added" : "Add this folder";
    $("host-shortcuts").replaceChildren(...data.shortcuts.map((s) => {
      const chip = el("button", "", s.label);
      chip.type = "button";
      chip.addEventListener("click", () => browseHost(s.path));
      return chip;
    }));
    const dirs = $("host-unadded").checked ? data.dirs.filter((d) => !d.added) : data.dirs;
    list.replaceChildren(...(dirs.length ? dirs.map((d) => {
      const item = el("li", d.refused ? "dir refused" : "dir", `${d.name}/`);
      if (d.added) item.append(el("span", "added-state", "Added"));
      if (d.refused) item.title = d.refused;
      item.addEventListener("click", () => browseHost(d.path));
      return item;
    }) : [el("li", "", $("host-unadded").checked ? "No directories that have not been added here." : "No folders here.")]));
  } catch (error) { list.replaceChildren(el("li", "error", error.message)); }
}

async function waitForRea() {
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 1500));
    try { if ((await fetch(`${API}/health`)).ok) return true; } catch {}
  }
  return false;
}

async function changeFolders(action, body, question) {
  if (question && !confirm(question)) return;
  const button = $("host-add");
  button.disabled = true;
  try {
    let data;
    try { data = await gw(`/api/rea/folders/${action}`, { method: "POST", json: body }); }
    catch (error) {
      if (!error.data?.can_force || !confirm(`${error.message}.\n\nDo it anyway?`)) throw error;
      data = await gw(`/api/rea/folders/${action}`, { method: "POST", json: { ...body, force: true } });
    }
    toast("REA is restarting to mount the folders…");
    await waitForRea();
    await loadMounted();
    if (action === "add") {
      const added = data.folders.find((f) => f.path === hostPath) || data.folders.at(-1);
      const target = await api("/targets", { method: "POST", json: { root: "folders", rel: added.slug } });
      $("folders").close();
      await loadTargets();
      select(target.id);
      toast(`${added.path} is mounted read-only`, "");
    } else { loadTargets(); browseHost(hostPath); }
  } catch (error) { toast(error.message, "error"); }
  finally { if (action === "add") button.disabled = Boolean($("host-refused").textContent) || button.textContent === "Added"; }
}

$("folder-button").addEventListener("click", () => { $("folders").showModal(); loadMounted(); browseHost(hostPath); });
$("host-hidden").addEventListener("change", () => browseHost(hostPath));
$("host-unadded").addEventListener("change", () => browseHost(hostPath));
$("host-add").addEventListener("click", () => changeFolders("add", { path: hostPath }, `Mount ${hostPath} read-only into REA? REA restarts for a few seconds and closes open files.`));

// ---------------------------------------------------------------------------- uploads --
async function upload(file) {
  if (!file) return;
  $("upload-progress").hidden = false;
  $("upload-name").textContent = `Uploading ${file.name}…`;
  $("upload-bar").value = 0;
  let id;
  try {
    ({ id } = await api("/uploads", { method: "POST", json: { name: file.name, size: file.size } }));
    for (let offset = 0; offset < file.size; offset += CHUNK) {
      await api(`/uploads/${id}?offset=${offset}`, { method: "PUT", body: file.slice(offset, offset + CHUNK),
        headers: { "Content-Type": "application/octet-stream" } });
      $("upload-bar").value = Math.min(1, (offset + CHUNK) / file.size);
    }
    const target = await api(`/uploads/${id}/done`, { method: "POST", json: {} });
    await loadTargets();
    select(target.id);
  } catch (error) {
    toast(`Upload failed: ${error.message}`, "error");
    if (id) api(`/uploads/${id}`, { method: "DELETE" }).catch(() => {});
  } finally { $("upload-progress").hidden = true; }
}

$("upload-input").addEventListener("change", (event) => { upload(event.target.files[0]); event.target.value = ""; });
const drop = $("drop");
for (const name of ["dragenter", "dragover"]) document.addEventListener(name, (e) => { e.preventDefault(); drop.classList.add("over"); });
for (const name of ["dragleave", "drop"]) document.addEventListener(name, (e) => { e.preventDefault(); if (name === "drop" || e.target === document.documentElement) drop.classList.remove("over"); });
document.addEventListener("drop", (e) => upload(e.dataTransfer?.files?.[0]));

// ---------------------------------------------------------------------- agent folders --
let pickerRoot = "workspace";
async function browse(dir = "") {
  const list = $("picker-list");
  list.replaceChildren(el("li", "", "Loading…"));
  try {
    const data = await api(`/files?root=${pickerRoot}&dir=${encodeURIComponent(dir)}`);
    $("picker-path").textContent = `${pickerRoot}/${data.dir}`;
    const rows = [];
    if (data.dir) {
      const up = el("li", "dir", "↑ Up");
      up.addEventListener("click", () => browse(data.dir.split("/").slice(0, -1).join("/")));
      rows.push(up);
    }
    for (const entry of data.entries) {
      const item = el("li", entry.dir ? "dir" : "");
      item.append(el("span", "", entry.dir ? `${entry.name}/` : entry.name), el("span", "size", entry.dir ? "" : size(entry.size)));
      if (entry.dir) {
        const asFolder = el("a", "open-folder", "Open as folder");
        asFolder.addEventListener("click", async (event) => {
          event.stopPropagation();
          try {
            const target = await api("/targets", { method: "POST", json: { root: pickerRoot, rel: entry.rel } });
            $("picker").close();
            await loadTargets();
            select(target.id);
          } catch (error) { toast(error.message, "error"); }
        });
        item.lastChild.replaceWith(asFolder);
      }
      item.addEventListener("click", async () => {
        if (entry.dir) return browse(entry.rel);
        try {
          const target = await api("/targets", { method: "POST", json: { root: pickerRoot, rel: entry.rel } });
          $("picker").close();
          await loadTargets();
          select(target.id);
        } catch (error) { toast(error.message, "error"); }
      });
      rows.push(item);
    }
    if (!data.entries.length) rows.push(el("li", "", pickerRoot === "folders" && !data.dir ? "No folders mounted yet: use Add folder." : "Empty folder"));
    list.replaceChildren(...rows);
  } catch (error) { list.replaceChildren(el("li", "error", error.message)); }
}
$("pick-button").addEventListener("click", () => { $("picker").showModal(); browse(); });
for (const button of document.querySelectorAll("#picker-root button")) button.addEventListener("click", () => {
  pickerRoot = button.dataset.root;
  for (const b of document.querySelectorAll("#picker-root button")) b.setAttribute("aria-pressed", String(b === button));
  browse();
});

// ------------------------------------------------------------------------ actions --
for (const button of document.querySelectorAll("#tabs button")) button.addEventListener("click", () => setTab(button.dataset.tab));
$("fn-filter").addEventListener("input", () => { state.fnLimit = PAGE; renderFunctions(); });
$("fn-external").addEventListener("change", () => { state.fnLimit = PAGE; renderFunctions(); });
$("str-filter").addEventListener("input", () => { state.strLimit = PAGE; renderStrings(); });

$("close-button").addEventListener("click", async () => {
  const target = current();
  await api(`/targets/${target.id}/close`, { method: "POST", json: {} }).catch((e) => toast(e.message, "error"));
  state.data = {};
  loadTargets();
});
$("remove-button").addEventListener("click", async () => {
  const target = current();
  const what = target.source === "upload" ? "Remove this file and delete the uploaded copy?" : "Remove this file from the list? The original stays in the agent's folder.";
  if (!confirm(what)) return;
  try {
    await api(`/targets/${target.id}`, { method: "DELETE" });
    state.current = null;
    loadTargets();
  } catch (error) { toast(error.message, "error"); }
});

let askTarget = null;
async function investigationGuide(folder) {
  const [inventoryResult, detectResult, modsResult] = await Promise.allSettled([
    api(`/targets/${folder.id}/inventory`), api(`/mods/detect?folder=${encodeURIComponent(folder.id)}`),
    api(`/mods?folder=${encodeURIComponent(folder.id)}`),
  ]);
  const inventory = inventoryResult.status === "fulfilled" ? inventoryResult.value : null;
  const detected = detectResult.status === "fulfilled" ? detectResult.value : null;
  const mods = modsResult.status === "fulfilled" ? modsResult.value.mods || [] : [];
  const projects = mods.map((m) => `${m.id} (${m.name}: ${m.readiness?.percent || 0}% ${m.readiness?.label || "unverified"})`).join("; ");
  const files = inventory?.files?.slice(0, 8).map((f) => `${folder.path}/${f.rel} (${f.format || f.kind}${f.embeddedPack ? ", embedded " + f.embeddedPack + " pack" : ""})`).join("; ");
  const engine = detected?.engineVersionLabel ? `${detected.label}, version ${detected.engineVersionLabel}`
    : detected?.label || inventory?.engines?.map((e) => e.version ? `${e.name} ${e.version}` : e.name).join(", ");
  const mountNote = folder.path.startsWith("/folders/")
    ? `The /folders mount exists inside REA, not in your terminal; do not use terminal ls/search_files on that path. ` : "";
  return `\nREA folder ID ${folder.id}. ${engine ? `Engine detected: ${engine}. ` : ""}` +
    (files ? `Top files: ${files}. ` : "") +
    `Get the complete read-only file list at http://127.0.0.1:8976/REA/api/targets/${folder.id}/inventory. ` +
    mountNote +
    `Use open_binary only on a listed file, never on the folder. If the task needs source recovery, read ` +
    `rocsy/rea-modding and use REA's Mods workflow: GET /REA/api/mods/detect?folder=${folder.id}, ` +
    (projects ? `Existing mod projects: ${projects}. Reuse the matching project and read GET /REA/api/mods/{project-id} readiness. ` : "") +
    `POST /REA/api/mods with {folder:"${folder.id}",name,goal,recipe:"blank"} only if no matching project exists, then ` +
    `POST /REA/api/mods/{project-id}/decompile (POSTs need X-REA: 1). Recovered files are in the shared rea-mods workspace. ` +
    `Decompile and packaging success do not prove the game runs; resolve readiness issues and report any missing play test. ` +
    (detected?.engine ? await toolsNote(detected.engine) : "");
}
function askAbout(target) {
  askTarget = target;
  $("ask-text").value = "";
  $("ask").showModal();
  $("ask-text").focus();
}
$("ask-button").addEventListener("click", () => askAbout(current()));
$("ask-form").addEventListener("submit", async (event) => {
  if (event.submitter?.value !== "send") return;
  event.preventDefault();
  const target = askTarget || current();
  const folder = target.type === "folder";
  const question = $("ask-text").value.trim() || (folder ? "Work out what this game/program is built with, which files hold its logic, and how it is put together."
    : "Explain what this program does and how its main features work.");
  const folderGuide = folder ? await investigationGuide(target) : "";
  const game = folder ? target : state.targets.find((t) => t.id === target.parent);
  let kbKey = "";
  if (game) try { kbKey = (await api(`/kb/for/${game.id}`)).key; } catch {}
  const kb = kbKey
    ? `\nThis game has a knowledge base, KEY ${kbKey}: use the skill rocsy/rea-knowledge ` +
      `(/opt/data/skills/rocsy/rea-knowledge/SKILL.md): search it first, check its decompile progress (kb.py decomp ${kbKey}) before decompiling anything and record what you decompile, write notes as you learn, keep ARCHITECTURE.md current (only about the game itself: a mod's design goes in that mod's own ARCHITECTURE.md), end with a session note.` : "";
  const prompt = `Use the REA reverse-engineering tools (MCP server "rea") to investigate the ${folder ? "folder" : "file"} ${target.path} ` +
    `(${target.name}${target.format ? `, ${target.format}` : ""}; paths are as REA sees them, read-only).${folderGuide} ` +
    `Show the evidence for your conclusions.${kb}\n\n${question}`;
  $("ask-send").disabled = true;
  try {
    const response = await fetch("/Agent/api/sessions", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, ...modelFor("ask") }) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    if (kbKey) await recordSession(kbKey, data.id, "ask", `${folder ? "Folder" : target.name}: ${question.slice(0, 120)}`);
    window.location.href = `/Agent/?s=${encodeURIComponent(data.id)}`;
  } catch (error) {
    toast(`Couldn't start the Master Agent: ${error.message}`, "error");
    $("ask-send").disabled = false;
  }
});

// ------------------------------------------------------------------- saved functions --
// Reusable code per engine (library.mjs): what worked on one game, ready for the next one.
const LIB_KINDS = { "mod-loader": "Mod loader", hook: "Hook", patch: "Patch", ui: "UI", utility: "Utility", build: "Build", other: "Other" };
const lib = { engine: "", kind: "", q: "", data: null, entry: null, editing: null };

function setTopTab(tab) {
  const library = tab === "library";
  for (const b of document.querySelectorAll("#top-tabs button")) b.setAttribute("aria-pressed", String(b.dataset.top === tab));
  if (Boolean(state.library) === library) return;
  state.library = library;
  try { history.replaceState(null, "", library ? "#library" : location.pathname + location.search); } catch {}
  renderTarget();
  if (library) loadLibrary();
}
for (const b of document.querySelectorAll("#top-tabs button")) b.addEventListener("click", () => setTopTab(b.dataset.top));

async function loadLibrary() {
  const params = new URLSearchParams();
  if (lib.engine) params.set("engine", lib.engine);
  if (lib.kind) params.set("kind", lib.kind);
  if (lib.q.length >= 2) params.set("q", lib.q);
  try { lib.data = await api(`/library?${params}`); renderLibrary(); }
  catch (error) { $("lib-list").replaceChildren(el("li", "error", error.message)); }
}

const engineLabel = (id) => lib.data?.engines.find((e) => e.id === id)?.label || id;

function renderLibrary() {
  const d = lib.data;
  if (!d) return;
  const total = d.engines.reduce((n, e) => n + e.count, 0);
  const chip = (id, label) => {
    const b = el("button", "", label);
    b.type = "button";
    b.setAttribute("aria-pressed", String(lib.engine === id));
    b.addEventListener("click", () => { lib.engine = id; loadLibrary(); });
    return b;
  };
  $("lib-engines").replaceChildren(chip("", `All engines · ${total}`),
    ...d.engines.filter((e) => e.count || e.id === lib.engine).map((e) => chip(e.id, `${e.label} · ${e.count}`)));
  if ($("lib-kind").options.length === 1)
    for (const k of d.kinds) { const o = el("option", "", LIB_KINDS[k] || k); o.value = k; $("lib-kind").append(o); }
  $("lib-list").replaceChildren(...(d.entries.length ? d.entries.map((e) => {
    const item = el("li");
    const row = el("div", "kb-row");
    row.append(el("strong", "", e.title), el("span", "badge", engineLabel(e.engine)), el("span", "badge", LIB_KINDS[e.kind] || e.kind),
      el("span", `badge ${e.status}`, e.status), el("span", `badge ${e.author === "user" ? "user" : ""}`, e.author === "user" ? "you" : "agent"));
    for (const t of e.tags || []) row.append(el("span", "badge", t));
    const from = [e.engineVersion, e.source?.game && `from ${e.source.game}`, `${e.files.length} file${e.files.length === 1 ? "" : "s"}`, `updated ${when(e.updated)}`];
    item.append(row, el("span", "hint", [e.summary, from.filter(Boolean).join(" · ")].filter(Boolean).join(" — ")));
    item.addEventListener("click", () => openLibEntry(e.id));
    return item;
  }) : [el("li", "empty", total ? "Nothing matches." : "No saved functions yet. When a mod loader, hook or patch works, the Master Agent saves it here for the next game on the same engine.")]));
}

async function openLibEntry(id) {
  try {
    const e = lib.entry = await api(`/library/${id}`);
    $("lib-editor").hidden = true;
    $("lib-entry-title").textContent = e.title;
    $("lib-entry-meta").textContent = [engineLabel(e.engine), e.engineVersion, LIB_KINDS[e.kind] || e.kind, e.language,
      `${e.status}${e.statusNote ? `: ${e.statusNote}` : ""}`, e.author === "user" ? "saved by you" : "saved by the agent",
      e.source?.game && `from ${e.source.game}${e.source.mod ? ` (mod ${e.source.mod})` : ""}`, `updated ${when(e.updated)}`, e.path].filter(Boolean).join(" · ");
    $("lib-entry-summary").textContent = e.summary || "";
    $("lib-entry-usage").replaceChildren(markdown(e.usage || ""));
    $("lib-entry-files").replaceChildren(...e.files.map((f) => {
      const box = el("div");
      const head = el("div", "code-head");
      const copy = el("button", "button small", "Copy");
      copy.type = "button";
      copy.addEventListener("click", async () => { try { await navigator.clipboard.writeText(f.text); toast("Copied"); } catch { toast("Couldn't copy", "error"); } });
      head.append(el("strong", "", f.name), copy);
      const pre = el("pre");
      for (const line of f.text.split("\n")) pre.append(highlight(line), "\n");
      box.append(head, pre);
      return box;
    }));
    $("lib-entry").hidden = false;
    $("lib-entry").scrollIntoView({ block: "nearest" });
  } catch (error) { toast(error.message, "error"); }
}

function libFileRow(f = { name: "", text: "" }) {
  const box = el("div", "lib-file");
  const row = el("div", "filter-row");
  const name = el("input");
  name.type = "search"; name.placeholder = "File name, e.g. rocsy/loader.gd"; name.value = f.name; name.className = "lib-fname";
  const remove = el("button", "button small", "Remove");
  remove.type = "button";
  remove.addEventListener("click", () => box.remove());
  const text = el("textarea");
  text.className = "lib-ftext"; text.rows = 10; text.value = f.text; text.placeholder = "Code"; text.spellcheck = false;
  row.append(name, remove);
  box.append(row, text);
  return box;
}

function editLibEntry(e) {
  lib.editing = e;
  const opts = (select, items, value) => {
    select.replaceChildren(...items.map(([v, label]) => { const o = el("option", "", label); o.value = v; return o; }));
    select.value = value;
  };
  opts($("lib-engine"), lib.data.engines.map((x) => [x.id, x.label]), e?.engine || lib.engine || "godot");
  $("lib-engine").disabled = Boolean(e);
  opts($("lib-ekind"), lib.data.kinds.map((k) => [k, LIB_KINDS[k] || k]), e?.kind || "mod-loader");
  $("lib-title").value = e?.title || "";
  $("lib-status").value = e?.status || "works";
  $("lib-version").value = e?.engineVersion || "";
  $("lib-lang").value = e?.language || "";
  $("lib-tags").value = (e?.tags || []).join(", ");
  $("lib-summary").value = e?.summary || "";
  $("lib-usage").value = e?.usage || "";
  $("lib-files").replaceChildren(...(e?.files?.length ? e.files : [{ name: "", text: "" }]).map(libFileRow));
  $("lib-entry").hidden = true;
  $("lib-editor").hidden = false;
  $("lib-title").focus();
}

$("lib-new").addEventListener("click", async () => { if (!lib.data) await loadLibrary(); editLibEntry(null); });
$("lib-edit").addEventListener("click", () => editLibEntry(lib.entry));
$("lib-close").addEventListener("click", () => { $("lib-entry").hidden = true; lib.entry = null; });
$("lib-add-file").addEventListener("click", () => $("lib-files").append(libFileRow()));
$("lib-cancel").addEventListener("click", () => { $("lib-editor").hidden = true; if (lib.editing) $("lib-entry").hidden = false; });
$("lib-save").addEventListener("click", async () => {
  const files = [...document.querySelectorAll("#lib-files .lib-file")].map((box) => ({
    name: box.querySelector(".lib-fname").value.trim(), text: box.querySelector(".lib-ftext").value }))
    .filter((f) => f.name || f.text.trim());
  const body = { title: $("lib-title").value, kind: $("lib-ekind").value, status: $("lib-status").value, engineVersion: $("lib-version").value,
    language: $("lib-lang").value, tags: $("lib-tags").value, summary: $("lib-summary").value, usage: $("lib-usage").value, files, by: "user" };
  try {
    const saved = lib.editing ? await api(`/library/${lib.editing.id}`, { method: "PUT", json: body })
      : await api("/library", { method: "POST", json: { ...body, engine: $("lib-engine").value, author: "user" } });
    $("lib-editor").hidden = true;
    await loadLibrary();
    openLibEntry(saved.id);
  } catch (error) { toast(error.message, "error"); }
});
$("lib-delete").addEventListener("click", async () => {
  if (!lib.entry || !confirm(`Delete the saved function "${lib.entry.title}"?`)) return;
  try { await api(`/library/${lib.entry.id}`, { method: "DELETE" }); $("lib-entry").hidden = true; lib.entry = null; loadLibrary(); }
  catch (error) { toast(error.message, "error"); }
});
let libTimer;
$("lib-q").addEventListener("input", () => { clearTimeout(libTimer); libTimer = setTimeout(() => { lib.q = $("lib-q").value.trim(); loadLibrary(); }, 250); });
$("lib-kind").addEventListener("change", () => { lib.kind = $("lib-kind").value; loadLibrary(); });

setInterval(() => { if (current()?.state === "analyzing") renderTarget(); }, 1000);

// Mod and rewrite projects use the same read-only browser as Decompiled.
const projectBrowsers = Object.fromEntries(["mod", "rewrite"].map((type) => [type,
  { type, id: "", files: [], root: null, open: new Set([""]), selected: "", query: "", kind: "all", serial: 0, viewer: null, blobs: [] }]));
const projectUrl = (browser, suffix) => browser.type === "mod" ? `/mods/${browser.id}/${suffix}` : `/mods/rewrites/${browser.id}/${suffix}`;
const projectRaw = (browser, file, download = false) => `${API}${projectUrl(browser, "raw")}?path=${encodeURIComponent(file.p)}${download ? "&download=1" : ""}`;

async function loadProjectBrowser(type, id) {
  const browser = projectBrowsers[type];
  if (browser.id !== id) {
    browser.viewer?.destroy(); browser.viewer = null;
    for (const url of browser.blobs.splice(0)) URL.revokeObjectURL(url);
    Object.assign(browser, { id, files: [], root: null, open: new Set([""]), selected: "", query: "", kind: "all" });
    $(`${type}-browser-filter`).value = "";
  }
  const serial = ++browser.serial;
  const base = `${type}-browser`;
  $(base + "-tree").replaceChildren(el("li", "hint", "Loading files…"));
  try {
    const data = await api(projectUrl(browser, "tree"));
    if (serial !== browser.serial || browser.id !== id) return;
    browser.dir = data.dir;
    browser.files = data.files;
    browser.truncated = data.truncated;
    renderProjectBrowser(browser);
  } catch (error) {
    if (serial === browser.serial) $(base + "-tree").replaceChildren(el("li", "error", error.message));
  }
}

function renderProjectBrowser(browser) {
  const base = `${browser.type}-browser`;
  const counts = {};
  for (const file of browser.files) counts[file.k] = (counts[file.k] || 0) + 1;
  $(base + "-summary").textContent = `${browser.files.length.toLocaleString()} files${browser.truncated ? " · list capped at 80,000" : ""}`;
  $(base + "-kinds").replaceChildren(...DX_KINDS.map(([kind, label]) => {
    const count = kind === "all" ? browser.files.length : kind === "scripts" ? (counts.code || 0) + (counts.project || 0) : counts[kind] || 0;
    if (!count && kind !== "all" && kind !== browser.kind) return null;
    const button = el("button", "", `${label} ${count.toLocaleString()}`);
    button.type = "button";
    button.setAttribute("aria-pressed", String(browser.kind === kind));
    button.addEventListener("click", () => { browser.kind = kind; renderProjectBrowser(browser); });
    return button;
  }).filter(Boolean));
  const filtered = browser.files.filter((file) =>
    (browser.kind === "all" || (browser.kind === "scripts" ? ["code", "project"].includes(file.k) : file.k === browser.kind)) &&
    (!browser.query || file.p.toLowerCase().includes(browser.query)));
  const root = { path: "", name: "Project", dirs: new Map(), files: [], count: 0 };
  for (const file of filtered) {
    let node = root;
    node.count++;
    const parts = file.p.split("/");
    for (const part of parts.slice(0, -1)) {
      if (!node.dirs.has(part)) node.dirs.set(part, { path: node.path ? `${node.path}/${part}` : part, name: part, dirs: new Map(), files: [], count: 0 });
      node = node.dirs.get(part);
      node.count++;
    }
    node.files.push({ ...file, name: parts.at(-1) });
  }
  browser.root = root;
  if (browser.query && filtered.length <= 400) {
    const expand = (node) => { browser.open.add(node.path); for (const child of node.dirs.values()) expand(child); };
    expand(root);
  }
  $(base + "-tree").replaceChildren(projectTreeRow(browser, root, 0));
  const selected = browser.selected && filtered.find((file) => file.p === browser.selected);
  if (selected) showProjectBrowserFile(browser, selected);
  else showProjectBrowserFolder(browser, root);
}

function projectTreeRow(browser, node, depth) {
  const li = el("li");
  li.setAttribute("role", "treeitem");
  const open = browser.open.has(node.path);
  li.setAttribute("aria-expanded", String(open));
  const row = el("button", `dx-row dir${depth ? "" : " root"}`, "");
  row.type = "button";
  row.style.paddingLeft = `${6 + depth * 14}px`;
  row.append(el("span", "dx-caret", open ? "▾" : "▸"), el("span", "dx-name", depth ? `${node.name}/` : node.name), el("span", "dx-count", node.count.toLocaleString()));
  row.addEventListener("click", () => {
    if (open) browser.open.delete(node.path); else browser.open.add(node.path);
    renderProjectBrowser(browser);
    showProjectBrowserFolder(browser, node);
  });
  li.append(row);
  if (open) {
    const group = el("ul"); group.setAttribute("role", "group");
    const dirs = [...node.dirs.values()].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
    for (const child of dirs) group.append(projectTreeRow(browser, child, depth + 1));
    for (const file of node.files.slice(0, 300)) {
      const child = el("li"); child.setAttribute("role", "treeitem");
      const button = el("button", `dx-row file${browser.selected === file.p ? " active" : ""}`);
      button.type = "button";
      button.style.paddingLeft = `${20 + (depth + 1) * 14}px`;
      button.append(el("span", `dx-glyph k-${file.k}`, DX_GLYPH[file.k] || "bin"), el("span", "dx-name", file.name));
      button.addEventListener("click", () => showProjectBrowserFile(browser, file));
      child.append(button); group.append(child);
    }
    if (node.files.length > 300) {
      const child = el("li");
      const button = el("button", "dx-row more", `${(node.files.length - 300).toLocaleString()} more in this folder…`);
      button.type = "button";
      button.addEventListener("click", () => showProjectBrowserFolder(browser, node));
      child.append(button); group.append(child);
    }
    li.append(group);
  }
  return li;
}

function showProjectBrowserFolder(browser, node) {
  browser.viewer?.destroy(); browser.viewer = null;
  for (const url of browser.blobs.splice(0)) URL.revokeObjectURL(url);
  browser.selected = "";
  const base = `${browser.type}-browser`;
  const view = $(base + "-view");
  const head = el("div", "dx-head");
  head.append(el("strong", "", node.path || "Project"), el("p", "hint", `${node.count.toLocaleString()} files · ${node.dirs.size} folders and ${node.files.length} files here`));
  view.replaceChildren(head);
  const grid = el("ul", "dx-grid");
  let limit = DX_PAGE;
  const fill = () => {
    const items = [...node.dirs.values()].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true })).map((x) => ["dir", x])
      .concat(node.files.map((x) => ["file", x]));
    grid.replaceChildren(...items.slice(0, limit).map(([type, item]) => {
      const li = el("li"), button = el("button", `dx-tile ${type}`);
      button.type = "button";
      button.append(el("span", `dx-thumb ${type === "dir" ? "folder" : `k-${item.k}`}`, type === "dir" ? "▤" : DX_GLYPH[item.k] || "bin"),
        el("span", "dx-tname", type === "dir" ? `${item.name}/` : item.name),
        el("span", "dx-tmeta", type === "dir" ? `${item.count.toLocaleString()} files` : size(item.s)));
      button.addEventListener("click", () => {
        if (type === "dir") { browser.open.add(item.path); $(base + "-tree").replaceChildren(projectTreeRow(browser, browser.root, 0)); showProjectBrowserFolder(browser, item); }
        else showProjectBrowserFile(browser, item);
      });
      li.append(button); return li;
    }));
    if (items.length > limit) {
      const li = el("li"), more = el("button", "dx-tile more", `Show ${Math.min(DX_PAGE, items.length - limit)} more`);
      more.type = "button"; more.addEventListener("click", () => { limit += DX_PAGE; fill(); });
      li.append(more); grid.append(li);
    }
  };
  fill();
  view.append(node.dirs.size || node.files.length ? grid : el("p", "hint", "Empty folder."));
}

async function showProjectBrowserFile(browser, file) {
  browser.viewer?.destroy(); browser.viewer = null;
  for (const url of browser.blobs.splice(0)) URL.revokeObjectURL(url);
  browser.selected = file.p;
  const base = `${browser.type}-browser`, id = browser.id;
  $(base + "-tree").querySelectorAll(".dx-row.active").forEach((row) => row.classList.remove("active"));
  const button = [...$(base + "-tree").querySelectorAll(".dx-row.file")].find((row) => row.querySelector(".dx-name")?.textContent === file.name);
  button?.classList.add("active");
  const head = el("div", "dx-head"), actions = el("div", "step-actions"), body = el("div", "dx-preview");
  head.append(el("strong", "", file.p), el("p", "hint", `${file.k} · ${size(file.s)}`));
  const download = el("a", "button", "Download"); download.href = projectRaw(browser, file, true); actions.append(download);
  if (state.local) {
    const show = el("button", "button", "Show in folder"); show.type = "button";
    show.addEventListener("click", () => openOnHost(`${browser.dir}/${file.p}`)); actions.append(show);
  }
  const copy = el("button", "button", "Copy path"); copy.type = "button";
  copy.addEventListener("click", async () => { try { await navigator.clipboard.writeText(`${browser.dir}/${file.p}`); toast("Path copied"); } catch { toast(`${browser.dir}/${file.p}`); } });
  actions.append(copy); head.append(actions);
  $(base + "-view").replaceChildren(head, body);
  if (matchMedia("(max-width: 860px)").matches) $(base + "-view").scrollIntoView({ block: "start" });
  const raw = projectRaw(browser, file), ext = dxExt(file.name);
  try {
    if (file.k === "image" && DX_DECODE_EXT.has(ext)) {
      const response = await fetch(raw); if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const canvas = dxDecodeImage(ext, await response.arrayBuffer()); canvas.className = "dx-image";
      const checker = el("div", "dx-checker"); checker.append(canvas); body.append(checker, el("p", "hint", `${canvas.width} × ${canvas.height} px`));
    } else if (file.k === "image" && !/^(psd|exr|hdr|ktx2?|tiff?|dib)$/.test(ext)) {
      const img = el("img", "dx-image"); img.alt = file.name; img.src = raw;
      const checker = el("div", "dx-checker"), dimensions = el("p", "hint"); checker.append(img); body.append(checker, dimensions);
      img.addEventListener("load", () => { dimensions.textContent = `${img.naturalWidth} × ${img.naturalHeight} px`; });
      img.addEventListener("error", () => { dimensions.textContent = "The browser can't show this image format."; });
    } else if (file.k === "audio" && /^(ogg|oga|opus|wav|mp3|flac|m4a|aac)$/.test(ext)) {
      const audio = el("audio"); audio.controls = true; audio.preload = "metadata"; audio.src = raw; body.append(audio);
    } else if (file.k === "video" && /^(webm|mp4|m4v|ogv)$/.test(ext)) {
      const video = el("video", "dx-video"); video.controls = true; video.preload = "metadata"; video.src = raw; body.append(video);
    } else if (file.k === "font" && /^(ttf|otf|woff2?)$/.test(ext)) {
      const family = `projectfont${Date.now()}`; const face = new FontFace(family, `url("${raw}")`); await face.load(); document.fonts.add(face);
      for (const px of [40, 24, 15]) { const line = el("p", "dx-font", "The quick brown fox jumps over the lazy dog 0123456789"); line.style.fontFamily = family; line.style.fontSize = `${px}px`; body.append(line); }
    } else if (DX_MODEL_EXT.has(ext) && file.s <= 96 * 1024 * 1024) {
      const nearby = (name) => {
        const clean = String(name).replace(/\\/g, "/").replace(/^res:\/\//, "").replace(/^\.\//, "");
        const parent = file.p.slice(0, file.p.lastIndexOf("/") + 1);
        return browser.files.find((item) => item.p === parent + clean || item.p === clean || item.p.endsWith("/" + clean));
      };
      const fetchRef = async (name) => {
        const found = nearby(name); if (!found) return null;
        const response = await fetch(projectRaw(browser, found)); return response.ok ? response.arrayBuffer() : null;
      };
      const response = await fetch(raw); if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const mesh = await dxParseModel(ext, await response.arrayBuffer(), {
        fetchRef, find: fetchRef, stem: file.name.replace(/\.[^.]+$/, ""),
      });
      for (const surface of mesh.surfaces) {
        if (surface.texBlob) { surface.texUrl = URL.createObjectURL(surface.texBlob); browser.blobs.push(surface.texUrl); delete surface.texBlob; }
        else if (surface.texRef) { const texture = nearby(surface.texRef); if (texture) surface.texUrl = projectRaw(browser, texture); }
      }
      const canvas = el("canvas", "dx-canvas");
      body.append(el("p", "hint", `${mesh.vertices.toLocaleString()} vertices · ${mesh.faces.toLocaleString()} faces · drag to turn, scroll to zoom`), canvas);
      browser.viewer = dxViewer(canvas, mesh);
      if (!browser.viewer) body.append(el("p", "hint", "This browser has no WebGL2 to show the model."));
      else browser.viewer.setLook(mesh.surfaces.map((surface) => ({ texture: surface.texCanvas || surface.texUrl || null, color: surface.color || [1, 1, 1, 1] })));
    } else {
      const response = await fetch(raw, file.s ? { headers: { Range: `bytes=0-${Math.min(file.s - 1, 524287)}` } } : {});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const bytes = new Uint8Array(await response.arrayBuffer());
      const decoded = new TextDecoder("utf-8").decode(bytes);
      const isText = !bytes.includes(0) && (DX_TEXT.has(file.k) || (!decoded.includes("�") && /^[\x09\x0a\x0d\x20-\x7e\u0080-\uffff]*$/.test(decoded)));
      if (isText) {
        if (file.s > bytes.length) body.append(el("p", "hint", `Showing the first ${size(bytes.length)} of ${size(file.s)}. Download for the rest.`));
        const pre = el("pre", "code dx-code"); pre.append(/\.(c|cs|cpp|cc|h|hpp|java|js|mjs|ts|gd|lua|py|rpy|hlsl|glsl|shader)$/i.test(file.name) ? highlight(decoded) : decoded); body.append(pre);
      } else {
        const lines = [];
        for (let i = 0; i < Math.min(bytes.length, 4096); i += 16) {
          const row = bytes.subarray(i, i + 16);
          lines.push(`${i.toString(16).padStart(8, "0")}  ${[...row].map((b) => b.toString(16).padStart(2, "0")).join(" ").padEnd(47)}  ${[...row].map((b) => b >= 32 && b < 127 ? String.fromCharCode(b) : ".").join("")}`);
        }
        body.append(el("p", "hint", `Binary file · first ${Math.min(bytes.length, 4096).toLocaleString()} bytes`), el("pre", "code dx-code", lines.join("\n")));
      }
    }
  } catch (error) { body.append(el("p", "error", `Couldn't preview: ${error.message}`)); }
  if (browser.id !== id || browser.selected !== file.p) body.replaceChildren();
}

for (const type of ["mod", "rewrite"]) {
  const browser = projectBrowsers[type], base = `${type}-browser`;
  $(base + "-refresh").addEventListener("click", () => loadProjectBrowser(type, browser.id));
  let timer;
  $(base + "-filter").addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => { browser.query = $(base + "-filter").value.trim().toLowerCase(); renderProjectBrowser(browser); }, 200);
  });
}

(async () => {
  try { state.local = Boolean((await gw("/api/rea/local")).local); } catch { state.local = false; }
  loadModelSettings();
  await loadTargets();
  let saved = null;
  try { saved = localStorage.getItem("rea.current"); } catch {}
  if (saved && state.targets.some((t) => t.id === saved)) await select(saved, { open: false });
  if (location.hash === "#library") setTopTab("library");
})();
