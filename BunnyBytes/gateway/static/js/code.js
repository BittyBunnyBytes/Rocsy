'use strict';
// Vibe Coding (/Code): the Hermes + Rocsy coding project library, laid out like REA (library sidebar,
// home with recent projects, open project with Files / Goals / Live preview / Stats). The open project is
// kept in the URL hash (#p=<id>) so a reload or a shared link reopens it. The Master Agent bar on an open
// project continues coding it (the project's last agent session, or a new one); Goals are the Agent's
// /goal milestones; Stats add up its sessions' events (code_agent.py on the gateway); History keeps
// versions of the files around agent turns and restores them (code_history.py).
const $ = id => document.getElementById(id);
let projects = [], selected = null, folder = '', showPreview = false, activeFile = '', activeMeta = '';
let tab = 'files', agent = null, agentTimer = 0, wasBusy = false, goalSplit = 'agent', models = null, codeReasoning = '';
let target = null, sessionsData = null;   // target = a session picked in the Sessions tab for the next message
let historyData = null;
let sourceFilter = '', sortBy = 'updated';

const SVG = {
  code: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><path d="M8 7L3.5 12 8 17M16 7l4.5 5-4.5 5M14 4l-4 16"/></svg>',
  site: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z"/></svg>',
  folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
};
const KINDS = {
  web: ['html', 'htm', 'css', 'scss', 'svelte', 'vue', 'jsx', 'tsx'],
  script: ['js', 'mjs', 'cjs', 'ts', 'py', 'rs', 'go', 'sh', 'rb', 'php', 'java', 'c', 'cpp', 'h', 'cs', 'lua', 'gd'],
  data: ['json', 'yaml', 'yml', 'toml', 'xml', 'csv', 'md', 'txt', 'ini', 'env', 'lock'],
  media: ['png', 'jpg', 'jpeg', 'gif', 'webp', 'avif', 'svg', 'mp4', 'webm', 'mp3', 'wav', 'ogg', 'ico'],
};
const IMAGE_EXT = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'avif', 'svg'];
const AV_EXT = ['mp4', 'webm', 'mp3', 'wav', 'ogg'];
const TEXT_EXT = ['html', 'css', 'js', 'jsx', 'ts', 'tsx', 'py', 'rs', 'go', 'vue', 'svelte', 'json', 'md', 'txt', 'yaml', 'yml', 'toml', 'sh', 'xml', 'csv'];

const viewer = () => $('preview-viewer');
const extOf = path => (path.includes('.') ? path.split('.').pop().toLowerCase() : '');
const kindOf = ext => Object.keys(KINDS).find(kind => KINDS[kind].includes(ext)) || '';
const sourceLabel = source => ({ hermes: 'Hermes', vibe: 'Vibe Coding' }[source] || 'Rocsy');
const api = (id, suffix) => `/Code/api/projects/${encodeURIComponent(id)}/${suffix}`;
const fileUrl = (path, download = false) => api(selected.id, 'file') + '?path=' + encodeURIComponent(path) + (download ? '&download=1' : '');

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function ago(seconds) {
  if (!seconds) return '';
  const diff = Date.now() / 1000 - seconds;
  if (diff < 60) return 'just now';
  const units = [[86400 * 365, 'y'], [86400 * 30, 'mo'], [86400 * 7, 'w'], [86400, 'd'], [3600, 'h'], [60, 'min']];
  const [size, unit] = units.find(([size]) => diff >= size);
  return `${Math.floor(diff / size)} ${unit} ago`;
}
function bytes(size) {
  if (size == null) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 ** 2) return `${(size / 1024).toFixed(size < 10240 ? 1 : 0)} KB`;
  return `${(size / 1024 ** 2).toFixed(1)} MB`;
}
function projectIcon(p, className = 'file-ic') {
  const box = el('span', `${className} ${p.preview ? 'site' : 'code'}`);
  box.setAttribute('aria-hidden', 'true');
  box.innerHTML = p.preview ? SVG.site : SVG.code;
  return box;
}
function fileIcon(entry) {
  const box = el('span', 't-ic');
  box.setAttribute('aria-hidden', 'true');
  if (entry.directory) { box.innerHTML = SVG.folder; return box; }
  const ext = extOf(entry.name);
  box.classList.add(kindOf(ext) || 'other');
  box.textContent = (ext || '·').slice(0, 3);
  return box;
}

function message(text, error = false) {
  $('message').textContent = text || '';
  $('message').classList.toggle('error', Boolean(text) && error);
}
function status(kind, text) {
  $('status-chip').className = `chip ${kind}`;
  $('status-text').textContent = text;
}
// What to say when the server gave no readable reason.
const STATUS_TEXT = {
  401: 'Your Rocsy login has expired. Reload the page to sign in again.',
  403: 'Rocsy refused this request. Reload the page and try again.',
  404: 'Not found: it may have been moved or deleted. Refresh the library.',
  413: 'That is too large for Rocsy to handle here.',
  500: 'Something went wrong in Rocsy. Try again; if it keeps happening, check the gateway log.',
  502: 'A Rocsy service answered with an error. Try again in a moment.',
  503: 'A Rocsy service is not running or not reachable. Try again in a moment.',
  504: 'A Rocsy service took too long to answer. Try again in a moment.',
};
function errorText(status, body) {
  const detail = typeof body.detail === 'object' ? body.detail?.message : body.detail;
  return detail || body.message || STATUS_TEXT[status] || `The request failed (HTTP ${status}).`;
}
async function json(url, body, method = body === undefined ? 'GET' : 'POST') {
  let response;
  try {
    response = await fetch(url, body === undefined && method === 'GET' ? { credentials: 'same-origin' } : {
      method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}),
    });
  } catch {
    throw Error('Can\'t reach Rocsy. Check your connection, then try again.');
  }
  if (!response.ok) throw Error(errorText(response.status, await response.json().catch(() => ({}))));
  return response.json();
}

/* ── library ───────────────────────────────────────────────────────────────────────────────── */
function visibleProjects() {
  const query = $('project-filter').value.trim().toLowerCase();
  return projects
    .filter(p => (!sourceFilter || p.source === sourceFilter) && (!query || `${p.name} ${p.path}`.toLowerCase().includes(query)))
    .sort((a, b) => (sortBy === 'name' ? a.name.localeCompare(b.name) : (b.updated || 0) - (a.updated || 0)));
}
function renderLibrary() {
  const list = $('projects');
  list.replaceChildren();
  const shown = visibleProjects();
  $('count').textContent = projects.length || '';
  if (!shown.length) {
    list.append(el('li', 'empty', projects.length ? 'No project matches.' : 'No coding projects yet.'));
    return;
  }
  for (const source of ['vibe', 'hermes', 'rocsy']) {
    const group = shown.filter(p => p.source === source);
    if (!group.length) continue;
    list.append(el('li', 'group-head', `${sourceLabel(source)} workspace`));
    for (const p of group) {
      const item = el('li');
      const button = el('button', 'project');
      button.type = 'button';
      button.dataset.id = p.id;
      button.setAttribute('aria-current', String(selected?.id === p.id));
      const body = el('span', 'file-body');
      body.append(el('span', 'file-name', p.name), el('span', 'file-meta', [p.preview ? 'Live preview' : 'Files', ago(p.updated)].filter(Boolean).join(' · ')));
      button.append(projectIcon(p), body);
      button.title = p.path;
      button.addEventListener('click', () => selectProject(p));
      item.append(button);
      list.append(item);
    }
  }
}
function renderIntro() {
  const recent = [...projects].sort((a, b) => (b.updated || 0) - (a.updated || 0)).slice(0, 6);
  $('intro-projects').hidden = !recent.length;
  $('open-latest').hidden = !recent.length;
  $('intro-count').textContent = projects.length > recent.length ? `${recent.length} of ${projects.length}` : '';
  $('intro-list').replaceChildren(...recent.map(p => {
    const item = el('li');
    const card = el('button', 'game-card');
    card.type = 'button';
    const text = el('span', 'game-text');
    const badges = el('span', 'badges');
    badges.append(el('span', `badge ${p.source}`, sourceLabel(p.source)));
    if (p.preview) badges.append(el('span', 'badge live', 'Live preview'));
    text.append(el('strong', '', p.name), el('span', 'hint', ago(p.updated) || p.path), badges);
    card.append(projectIcon(p), text);
    card.addEventListener('click', () => selectProject(p));
    item.append(card);
    return item;
  }));
}
async function loadProjects() {
  status('busy', 'Scanning workspaces…');
  try {
    projects = (await json('/Code/api/projects')).projects;
    const wanted = selected?.id || new URLSearchParams(location.hash.slice(1)).get('p');
    renderLibrary();
    renderIntro();
    status('ok', `${projects.length} project${projects.length === 1 ? '' : 's'}`);
    message('');
    const again = projects.find(p => p.id === wanted);
    if (again) await selectProject(again);
    else if (selected) showHome();
  } catch (e) {
    status('bad', 'Unavailable');
    message(e.message, true);
  }
}

/* ── open project ──────────────────────────────────────────────────────────────────────────── */
function leaveFullscreen() { if (document.fullscreenElement === viewer()) void document.exitFullscreen(); }
function updateFullscreenButton() {
  $('fullscreen-preview').hidden = !showPreview;
  $('fullscreen-preview').textContent = document.fullscreenElement === viewer() ? 'Exit full screen' : 'Full screen';
}
function showHome() {
  leaveFullscreen();
  selected = null;
  agent = null;
  clearTimeout(agentTimer);
  $('preview-frame').removeAttribute('src');
  $('target').hidden = true;
  $('intro').hidden = false;
  history.replaceState(null, '', location.pathname + location.search);
  renderLibrary();
}
// Files and Live preview share the browser box; Goals and Stats are their own panels.
function setTab(next) {
  tab = next;
  for (const [id, name] of [['view-files', 'files'], ['view-goals', 'goals'], ['view-sessions', 'sessions'], ['preview-toggle', 'preview'], ['view-stats', 'stats'], ['view-history', 'history']])
    $(id).setAttribute('aria-pressed', String(name === next));
  $('browser').hidden = !['files', 'preview'].includes(next);
  $('goals-panel').hidden = next !== 'goals';
  $('sessions-panel').hidden = next !== 'sessions';
  $('stats-panel').hidden = next !== 'stats';
  $('history-panel').hidden = next !== 'history';
  if (next !== 'preview' && showPreview) setView(false, true);
  if (next === 'goals') renderGoal();
  if (next === 'stats') loadStats();
  if (next === 'sessions') loadSessions();
  if (next === 'history') loadHistory();
  const hints = { goals: 'The Master Agent works through the goal turn after turn; each milestone is checked off only with proof.',
    sessions: 'Pick one to continue it from the Master Agent bar, or open it on the Agent page.',
    stats: 'Agent working time, sessions and models across every Master Agent session of this project.',
    history: 'Restoring saves the current files as a version first, so a restore can be undone too.' };
  if (hints[next]) $('view-hint').textContent = hints[next];
}
function setView(preview, quiet = false) {
  showPreview = preview;
  if (!quiet) setTab(preview ? 'preview' : 'files');
  $('browser').classList.toggle('previewing', preview);
  $('preview-frame').hidden = !preview;
  $('file-content').hidden = preview;
  $('download-file').hidden = preview || !activeFile;
  $('file-ic').textContent = preview || !activeFile ? '' : (extOf(activeFile) || '·').slice(0, 3);
  $('file-name').textContent = preview ? 'Live preview · index.html' : activeFile || 'Choose a file';
  $('file-meta').textContent = preview ? 'sandboxed, scripts only' : activeMeta;
  $('view-hint').textContent = preview ? 'Runs the project\'s index.html in a sandbox (no cookies, no access to Rocsy).' : '';
  updateFullscreenButton();
  if (!preview) { leaveFullscreen(); $('preview-frame').removeAttribute('src'); }
}
async function selectProject(p) {
  leaveFullscreen();
  selected = p;
  folder = '';
  activeFile = activeMeta = '';
  history.replaceState(null, '', `#p=${encodeURIComponent(p.id)}`);
  $('intro').hidden = true;
  $('target').hidden = false;
  $('t-kind').className = `t-kind ${p.preview ? 'site' : 'code'}`;
  $('t-kind').innerHTML = p.preview ? SVG.site : SVG.code;
  $('source').textContent = `${sourceLabel(p.source)} workspace${p.updated ? ' · changed ' + ago(p.updated) : ''}`;
  $('project-name').textContent = p.name;
  $('project-path').textContent = p.path;
  $('download-project').href = api(p.id, 'download');
  $('download-project').hidden = false;
  $('delete-project').hidden = false;
  $('preview-toggle').hidden = !p.preview;
  $('browser').hidden = false;
  $('file-content').replaceChildren(el('p', 'empty', 'Select a file to view it here.'));
  agent = null;
  wasBusy = false;
  sessionsData = null;
  historyData = null;
  setTarget(null);
  $('sessions-count').textContent = '';
  $('history-count').textContent = '';
  renderAgent();
  setView(false);
  loadAgent();
  renderLibrary();
  if (window.matchMedia('(max-width: 860px)').matches) $('target').scrollIntoView({ block: 'start' });
  await openFolder('');
}
async function openFolder(path) {
  if (!selected) return;
  try {
    const data = await json(api(selected.id, 'files') + '?path=' + encodeURIComponent(path));
    folder = path;
    $('folder').textContent = path ? `/${path}` : '/';
    $('folder').title = path || selected.path;
    $('up').disabled = !path;
    const tree = $('files');
    tree.replaceChildren();
    for (const entry of data.entries) {
      const item = el('li', entry.directory ? 'folder' : '');
      const button = el('button');
      button.type = 'button';
      button.title = entry.path;
      button.dataset.path = entry.path;
      button.setAttribute('aria-current', String(entry.path === activeFile));
      button.append(fileIcon(entry), el('span', 't-name', entry.name));
      if (!entry.directory) button.append(el('span', 't-size', bytes(entry.size)));
      button.addEventListener('click', () => (entry.directory ? openFolder(entry.path) : openFile(entry.path, entry.size)));
      item.append(button);
      tree.append(item);
    }
    if (!data.entries.length) tree.append(el('li', 'empty', 'This folder is empty.'));
    message('');
  } catch (e) { message(e.message, true); }
}
async function openFile(path, size) {
  if (showPreview) setView(false);
  activeFile = path;
  document.querySelectorAll('#files button').forEach(b => b.setAttribute('aria-current', String(b.dataset.path === path)));
  const ext = extOf(path);
  $('file-ic').textContent = (ext || '·').slice(0, 3);
  $('file-name').textContent = path;
  $('file-meta').textContent = activeMeta = bytes(size);
  $('download-file').hidden = false;
  $('download-file').href = fileUrl(path, true);
  const area = $('file-content');
  area.replaceChildren();
  const url = fileUrl(path);
  if (IMAGE_EXT.includes(ext)) {
    const box = el('div', 'media');
    const img = el('img');
    img.src = url;
    img.alt = path;
    box.append(img);
    area.append(box);
  } else if (AV_EXT.includes(ext)) {
    const box = el('div', 'media');
    const media = el(ext === 'mp4' || ext === 'webm' ? 'video' : 'audio');
    media.src = url;
    media.controls = true;
    box.append(media);
    area.append(box);
  } else if (TEXT_EXT.includes(ext)) {
    try {
      const response = await fetch(url, { credentials: 'same-origin' });
      if (!response.ok) {
        throw Error(errorText(response.status, await response.json().catch(() => ({}))));
      }
      const text = await response.text();
      if (activeFile !== path) return;
      const pre = el('pre');
      const lines = text.split('\n');
      if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
      // Line numbers for readable sizes; one plain block for very long files.
      if (lines.length <= 20000) pre.append(...lines.map(line => el('span', 'ln', line)));
      else pre.textContent = text;
      area.classList.toggle('wrap', ['md', 'txt', 'csv'].includes(ext));
      area.append(pre);
      $('file-meta').textContent = activeMeta = [bytes(size), `${lines.length} line${lines.length === 1 ? '' : 's'}`].filter(Boolean).join(' · ');
    } catch (e) { area.append(el('p', 'note', e.message)); }
  } else {
    area.append(el('p', 'note', 'No preview for this file type. Use Download to open it on your computer.'));
  }
}

/* ── Master Agent ──────────────────────────────────────────────────────────────────────────── */
async function loadModels() {
  try {
    const options = await json('/Agent/api/options');
    const hermes = options.hermes;
    models = hermes?.providers || [];
    const pick = $('ask-model');
    const def = hermes?.default?.model ? ` (${hermes.default.model})` : '';
    pick.replaceChildren(new Option(`Agentic default model${def}`, ''));
    for (const pr of models) {
      const group = document.createElement('optgroup');
      group.label = `${pr.name}${pr.local ? ' · local GPU' : ''}`;
      for (const m of pr.models) group.append(new Option(m, JSON.stringify([pr.slug, m])));
      pick.append(group);
    }
    for (const c of options.clis || []) {   // coding CLIs installed on this machine (Claude Code, Codex …), last
      const group = document.createElement('optgroup');
      group.label = `${c.name} · coding CLI`;
      group.append(new Option(`${c.name} default${c.default ? ` (${c.default})` : ''}`, JSON.stringify([`cli:${c.id}`, 'default'])));
      for (const m of c.models) group.append(new Option(m.label === m.id ? m.id : `${m.label} (${m.id})`, JSON.stringify([`cli:${c.id}`, m.id])));
      pick.append(group);
    }
    // The default lives on the server (Settings → Sub-agents uses the same one).
    const saved = (await json('/Code/api/settings').catch(() => ({}))).model || {};
    const value = saved.model ? JSON.stringify([saved.provider, saved.model]) : '';
    if (value && ![...pick.options].some(o => o.value === value)) pick.append(new Option(`${saved.model} (${saved.provider || '?'})`, value));
    pick.value = value;
    codeReasoning = saved.reasoning || '';
  } catch { /* Agent service down: the default model is still offered */ }
}
function modelChoice() {
  const value = $('ask-model').value;
  if (!value) return {};
  const [provider, model] = JSON.parse(value);
  return { model: { model, provider, reasoning: '' } };
}
const AGENT_STATE = { thinking: 'Working', rendering: 'Rendering', starting: 'Starting', waiting: 'Waiting', done: 'Idle', interrupted: 'Interrupted', failed: 'Failed', stopped: 'Stopped' };
function renderAgent() {
  const latest = agent?.sessions?.find(x => !x.missing);
  const chip = $('agent-chip');
  chip.className = `agent-chip ${latest?.busy ? 'busy' : latest ? 'ok' : ''}`;
  const total = Math.max(agent?.total_sessions || 0, agent?.sessions?.length || 0, sessionsData?.sessions.length || 0);
  $('agent-text').textContent = latest
    ? `${latest.busy ? latest.stage || AGENT_STATE[latest.state] || 'Working' : AGENT_STATE[latest.state] || latest.state} · ${total} session${total === 1 ? '' : 's'}`
    : agent ? 'No session yet' : 'Checking…';
  $('agent-open').hidden = !latest;
  if (latest) { $('agent-open').href = latest.url; $('agent-open').title = latest.title; }
  $('ask-send').textContent = (latest || target) && !$('ask-fresh').checked ? 'Continue coding' : 'Start coding';
  $('ask-hint').textContent = latest?.busy ? 'Busy: your message waits until it can read it.' : '';
  const parts = agent?.goal?.parts || [];
  $('goals-count').textContent = parts.length ? `${parts.filter(x => x.status === 'done').length}/${parts.length}` : agent?.goal ? '•' : '';
  if (tab === 'goals') renderGoal();
  if (tab === 'history' && historyData) renderHistory();   // restore buttons follow the agent's busy state
}
async function loadAgent() {
  clearTimeout(agentTimer);
  if (!selected) return;
  const id = selected.id;
  try {
    const data = await json(api(id, 'agent'));
    if (selected?.id !== id) return;
    agent = data;
    renderAgent();
    if (wasBusy && !data.busy) {        // the agent just finished a turn: show what it changed
      await openFolder(folder);
      if (showPreview) await loadPreview();
      if (tab === 'stats') loadStats();
      if (tab === 'sessions') loadSessions();
      if (tab === 'history') loadHistory();
      void refreshProjectInfo();
    }
    wasBusy = data.busy;
  } catch (e) {
    if (selected?.id === id) { agent = agent || { sessions: [] }; $('agent-text').textContent = e.message; }
  }
  if (selected?.id === id) agentTimer = setTimeout(loadAgent, wasBusy ? 4000 : 20000);
}
async function refreshProjectInfo() {
  try {
    projects = (await json('/Code/api/projects')).projects;
    const fresh = projects.find(p => p.id === selected?.id);
    if (fresh && fresh.preview !== selected.preview) { selected = fresh; $('preview-toggle').hidden = !fresh.preview; }
    renderLibrary();
    renderIntro();
  } catch { /* next refresh */ }
}
async function sendToAgent(body, button) {
  if (!selected) return null;
  button.disabled = true;
  try {
    const result = await json(api(selected.id, 'agent'), { ...body, ...modelChoice() });
    message('');
    wasBusy = true;
    await loadAgent();
    return result;
  } catch (e) {
    message(e.message, true);
    return null;
  } finally { button.disabled = false; }
}
$('ask-form').addEventListener('submit', async e => {
  e.preventDefault();
  const text = $('ask-text').value.trim();
  if (!text) { $('ask-text').focus(); return; }
  const fresh = $('ask-fresh').checked;
  if (await sendToAgent({ text, mode: 'work', fresh, ...(target && !fresh ? { session: target.id } : {}) }, $('ask-send'))) {
    $('ask-text').value = '';
    $('ask-fresh').checked = false;
    renderAgent();
  }
});
$('ask-text').addEventListener('keydown', e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) $('ask-form').requestSubmit(); });
$('ask-fresh').addEventListener('change', renderAgent);
$('ask-model').addEventListener('change', () => {
  const value = $('ask-model').value, [provider, model] = value ? JSON.parse(value) : ['', ''];
  json('/Code/api/settings', { model: { model, provider, reasoning: codeReasoning } }, 'PUT').catch(() => {});
});
$('ask-goal').addEventListener('click', () => {
  if ($('ask-text').value.trim()) $('goal-text').value = $('ask-text').value.trim();
  setTab('goals');
  $('goal-text').focus();
});

/* ── sessions ──────────────────────────────────────────────────────────────────────────────── */
const KIND_LABEL = { work: 'Started here', goal: 'Goal', found: 'Worked on its files' };
function setTarget(s) {
  target = s;
  $('ask-target').hidden = !s;
  $('ask-target-name').textContent = s ? s.title || s.id.slice(0, 8) : '';
  if (s) $('ask-fresh').checked = false;
  if (agent) renderAgent();
}
$('ask-target-clear').addEventListener('click', () => { setTarget(null); if (tab === 'sessions') renderSessions(); });
async function loadSessions() {
  if (!selected) return;
  const id = selected.id;
  if (!sessionsData) $('session-list').replaceChildren(el('li', 'empty', 'Looking through the agent sessions…'));
  $('sessions-refresh').disabled = true;
  try {
    const data = await json(api(id, 'sessions'));
    if (selected?.id !== id) return;
    sessionsData = data;
    $('sessions-count').textContent = data.sessions.length || '';
    renderSessions();
    if (agent) renderAgent();
  } catch (e) {
    if (selected?.id === id) $('session-list').replaceChildren(el('li', 'note', e.message));
  } finally { $('sessions-refresh').disabled = false; }
}
function renderSessions() {
  const list = $('session-list');
  const items = sessionsData?.sessions || [];
  if (!items.length) {
    list.replaceChildren(el('li', 'empty', sessionsData?.missing?.length
      ? 'This project\'s agent sessions were deleted.'
      : 'No agent session has worked on this project yet. Ask the Master Agent above to start.'));
    return;
  }
  list.replaceChildren(...items.map(x => {
    const item = el('li', `session-card${x.busy ? ' busy' : ''}${target?.id === x.id ? ' targeted' : ''}`);
    const head = el('div', 'sc-head');
    const title = el('a', 'sc-title', x.title || x.id.slice(0, 8));
    title.href = x.url;
    title.target = '_blank';
    title.rel = 'noopener';
    const state = el('span', `pill ${x.busy ? 'run' : x.state === 'done' ? 'ok' : ''}`, x.busy ? x.stage || 'Working' : AGENT_STATE[x.state] || x.state);
    head.append(title, state, el('span', `badge k-${x.kind || 'work'}`, KIND_LABEL[x.kind] || 'Session'));
    const facts = el('div', 'sc-facts');
    for (const text of [`active ${ago(x.updated)}`, x.created ? `started ${new Date(x.created * 1000).toLocaleDateString()}` : '',
      x.seconds ? `${duration(x.seconds)} working` : '', `${x.runs} run${x.runs === 1 ? '' : 's'}`,
      x.changes ? `${x.changes} file change${x.changes === 1 ? '' : 's'} here` : 'no file changes here',
      x.jobs ? `${x.jobs} job${x.jobs === 1 ? '' : 's'}` : '']) if (text) facts.append(el('span', '', text));
    const models = el('div', 'sc-models');
    for (const name of x.models.length ? x.models : [x.model].filter(Boolean)) models.append(el('span', 'sc-model', name));
    const body = [head, facts];
    if (models.childElementCount) body.push(models);
    if (x.last_user) body.push(el('p', 'sc-line', `You: ${x.last_user}`));
    if (x.last_agent) body.push(el('p', 'sc-line agent', `Agent: ${x.last_agent}`));
    if (x.error) body.push(el('p', 'sc-line error', x.error));
    if (x.files.length) {
      const files = el('p', 'sc-files');
      files.append(el('span', 'hint', 'Files: '), document.createTextNode(x.files.join(', ')));
      body.push(files);
    }
    const actions = el('div', 'sc-actions');
    const pick = el('button', 'button small', target?.id === x.id ? 'Selected' : 'Continue in this session');
    pick.type = 'button';
    pick.disabled = target?.id === x.id;
    pick.addEventListener('click', () => { setTarget(x); renderSessions(); $('ask-text').focus(); });
    const open = el('a', 'button small', 'Open');
    open.href = x.url;
    open.target = '_blank';
    open.rel = 'noopener';
    actions.append(pick, open);
    body.push(actions);
    item.append(...body);
    return item;
  }));
}
$('view-sessions').addEventListener('click', () => setTab('sessions'));
$('sessions-refresh').addEventListener('click', loadSessions);

/* ── goals ─────────────────────────────────────────────────────────────────────────────────── */
function goalButton(label, action, extra = {}) {
  const b = el('button', 'button small', label);
  b.type = 'button';
  b.addEventListener('click', async () => {
    b.disabled = true;
    try { agent = await json(api(selected.id, 'goal'), { action, ...extra }); wasBusy = wasBusy || action === 'resume'; renderAgent(); message(''); }
    catch (e) { message(e.message, true); }
    finally { b.disabled = false; }
  });
  return b;
}
function renderGoal() {
  const box = $('goal-current');
  const g = agent?.goal;
  if (!g?.text) {
    box.replaceChildren(el('h2', '', 'Current goal'),
      el('p', 'hint', 'No goal yet. Write one on the right: the agent splits it into milestones (or you write them), works on one at a time and checks each off only with proof.'));
    return;
  }
  const parts = g.parts || [];
  const done = parts.filter(x => x.status === 'done').length;
  const state = g.achieved ? 'Achieved' : { active: 'Active', paused: 'Paused', done: 'Achieved', cleared: 'Cleared' }[g.status] || g.status;
  const head = el('div', 'goal-head');
  head.append(el('h2', '', 'Current goal'), el('span', `pill ${g.achieved || g.status === 'done' ? 'ok' : g.status === 'active' ? 'run' : ''}`, state));
  const meta = el('p', 'hint', `${parts.length ? `${done} of ${parts.length} milestones proven · ` : ''}${g.turns || 0} turn${g.turns === 1 ? '' : 's'}`
    + `${g.max_turns ? ` of ${g.max_turns}` : ' · no turn limit'}${g.live ? '' : ' · its session is gone'}`);
  const bar = el('div', 'goal-progress');
  bar.setAttribute('role', 'progressbar');
  bar.setAttribute('aria-valuemin', '0');
  bar.setAttribute('aria-valuemax', String(parts.length || 1));
  bar.setAttribute('aria-valuenow', String(done));
  const fill = el('span');
  fill.style.width = `${parts.length ? (done / parts.length) * 100 : 0}%`;
  bar.append(fill);
  const actions = el('div', 'goal-actions');
  if (g.live && !g.achieved) {
    if (g.status === 'active') actions.append(goalButton('Pause', 'pause'));
    if (g.status === 'paused') actions.append(goalButton('Resume', 'resume'));
  }
  if (g.live && g.status !== 'cleared') actions.append(goalButton('Clear', 'clear'));
  const list = el('ol', 'goal-parts');
  parts.forEach((part, i) => {
    const current = i === g.part && part.status !== 'done' && g.status === 'active' && !g.achieved;
    const item = el('li', part.status === 'done' ? 'done' : current ? 'current' : '');
    item.append(el('span', 'goal-check', part.status === 'done' ? '✓' : current ? '▶' : String(i + 1)), el('span', 'goal-text', part.text));
    if (part.evidence) item.append(el('span', 'goal-proof', `Proof: ${part.evidence}`));
    else if (current) item.append(el('span', 'goal-proof hint', 'Working on it now'));
    if (part.status === 'done' && g.live) {
      const again = goalButton('Reopen', 'reopen', { part: i });
      again.title = 'The proof was wrong: send this milestone back to the agent';
      item.append(again);
    }
    list.append(item);
  });
  const whole = el('details', 'goal-whole');
  whole.append(el('summary', '', 'The whole goal'), el('p', '', g.text.replace(/^[\s\S]*\nGOAL: /, '')));
  const out = [head, meta];
  if (parts.length) out.push(bar, list);
  else out.push(el('p', 'hint', 'Not split into milestones yet: the agent does that first on its next turn.'));
  if (g.evidence) out.push(el('p', 'goal-proof', `Final proof: ${g.evidence}`));
  out.push(whole, actions);
  box.replaceChildren(...out);
}
$('goal-split').addEventListener('click', e => {
  const button = e.target.closest('button[data-split]');
  if (!button) return;
  goalSplit = button.dataset.split;
  $('goal-split').querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b === button)));
  $('goal-steps').hidden = goalSplit !== 'me';
  $('goal-steps').required = goalSplit === 'me';
});
$('goal-steps').addEventListener('input', () => {
  const n = $('goal-steps').value.split('\n').filter(x => x.trim()).length;
  $('goal-hint').textContent = n ? `${n} milestone${n === 1 ? '' : 's'}${n < 3 || n > 12 ? ' (write 3 to 12)' : ''}` : '';
});
$('goal-form').addEventListener('submit', async e => {
  e.preventDefault();
  const text = $('goal-text').value.trim();
  const milestones = goalSplit === 'me' ? $('goal-steps').value.split('\n').map(x => x.replace(/^\s*(?:[-*•]|\d{1,2}[.)])\s+/, '').trim()).filter(Boolean) : [];
  if (!text) return;
  if (goalSplit === 'me' && (milestones.length < 3 || milestones.length > 12)) { message('Write 3 to 12 milestones, one per line.', true); return; }
  if (agent?.goal?.live && agent.goal.status === 'active' && !agent.goal.achieved
      && !confirm('This replaces the current goal (its proven milestones stay in the session history). Start the new goal?')) return;
  const result = await sendToAgent({ text, mode: 'goal', milestones, budget: $('goal-budget').value, fresh: $('goal-fresh').checked }, $('goal-start'));
  if (result) {
    $('goal-text').value = $('goal-steps').value = '';
    $('goal-fresh').checked = false;
    $('goal-hint').textContent = 'Goal sent: milestones appear here once the agent registers them.';
  }
});

/* ── stats ─────────────────────────────────────────────────────────────────────────────────── */
function duration(seconds) {
  seconds = Math.round(seconds || 0);
  if (seconds < 60) return `${seconds} s`;
  const h = Math.floor(seconds / 3600), m = Math.round((seconds % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min`;
}
const pct = share => `${Math.round(share * 1000) / 10}%`;
function tile(label, value, sub) {
  const box = el('div', 'stat');
  box.append(el('span', 'stat-label', label), el('strong', 'stat-value', value));
  if (sub) box.append(el('span', 'stat-sub', sub));
  return box;
}
async function loadStats() {
  if (!selected) return;
  const id = selected.id, panel = $('stats-panel');
  if (!panel.childElementCount || panel.dataset.project !== id) panel.replaceChildren(el('p', 'empty', 'Adding up the agent sessions…'));
  panel.dataset.project = id;
  let st;
  try { st = await json(api(id, 'stats')); } catch (e) { panel.replaceChildren(el('p', 'note', e.message)); return; }
  if (selected?.id !== id) return;
  if (!st.sessions) {
    panel.replaceChildren(el('p', 'empty', st.missing_sessions
      ? 'The agent sessions of this project were deleted, so there is nothing left to count.'
      : 'No Master Agent sessions for this project yet. Ask the agent above to start coding: its time, sessions and models show up here.'));
    return;
  }
  const tiles = el('div', 'stat-row');
  const span = st.first && st.last ? `${new Date(st.first * 1000).toLocaleDateString()} – ${new Date(st.last * 1000).toLocaleDateString()}` : '';
  tiles.append(tile('Time vibecoding', duration(st.seconds), 'agent working time'),
    tile('Agent sessions', String(st.sessions), st.missing_sessions ? `${st.missing_sessions} deleted` : span),
    tile('Agent runs', String(st.runs), `${st.messages} message${st.messages === 1 ? '' : 's'} from you`),
    tile('File changes', String(st.changes), `${st.tools} tool calls`),
    tile('Models', String(st.models.length), st.models[0] ? `most: ${st.models[0].model}` : ''));
  const byChanges = st.changes > 0;
  const models = el('section', 'stat-block');
  models.append(el('h2', '', 'Models'), el('p', 'hint', byChanges
    ? 'Contribution = share of the file writes and edits each model made in this project folder.'
    : 'Contribution = share of agent working time (no file changes in this folder were logged yet).'));
  const list = el('ul', 'model-bars');
  for (const m of st.models) {
    const item = el('li');
    const head = el('div', 'mb-head');
    head.append(el('strong', 'mb-name', m.model), el('span', 'mb-share', pct(m.share)));
    const track = el('div', 'mb-track');
    const fill = el('span');
    fill.style.width = `${Math.max(m.share * 100, m.share ? 1.5 : 0)}%`;
    track.append(fill);
    track.title = `${m.model}: ${pct(m.share)} of ${byChanges ? 'file changes' : 'working time'}`;
    track.setAttribute('role', 'img');
    track.setAttribute('aria-label', track.title);
    const facts = el('div', 'mb-facts');
    for (const text of [duration(m.seconds) + ` (${pct(m.share_time)} of time)`, `${m.runs} run${m.runs === 1 ? '' : 's'}`,
      `${m.sessions} session${m.sessions === 1 ? '' : 's'}`, `${m.writes} writes · ${m.edits} edits`, `${m.files_touched} file${m.files_touched === 1 ? '' : 's'}`,
      `${m.tools} tool calls`, m.provider]) if (text) facts.append(el('span', '', text));
    item.append(head, track, facts);
    if (m.top_files.length) {
      const files = el('p', 'mb-files');
      files.append(el('span', 'hint', 'Most changed: '), document.createTextNode(m.top_files.map(f => `${f.path} (${f.changes})`).join(', ')));
      item.append(files);
    }
    list.append(item);
  }
  models.append(list);
  const sessions = el('section', 'stat-block');
  sessions.append(el('h2', '', 'Sessions'));
  const table = el('table', 'stat-table');
  const thead = el('thead');
  const hr = el('tr');
  for (const h of ['Session', 'Last active', 'Working time', 'Models']) hr.append(el('th', '', h));
  thead.append(hr);
  const tbody = el('tbody');
  for (const x of st.session_list) {
    const tr = el('tr');
    const name = el('td');
    const link = el('a', '', x.title || x.id.slice(0, 8));
    link.href = `/Agent/?s=${x.id}`;
    link.target = '_blank';
    link.rel = 'noopener';
    name.append(link);
    tr.append(name, el('td', '', ago(x.last)), el('td', '', duration(x.seconds)), el('td', '', x.models.join(', ') || '—'));
    tbody.append(tr);
  }
  table.append(thead, tbody);
  sessions.append(table);
  panel.replaceChildren(tiles, models, sessions);
}

/* ── version history ───────────────────────────────────────────────────────────────────────── */
const VERSION_KIND = { before: ['Before agent turn', 'run'], agent: ['After agent turn', 'ok'], manual: ['Saved', ''],
  safety: ['Before a restore', ''], restored: ['Restored', 'ok'] };
const undoTarget = () => historyData?.versions.find(v => v.kind === 'before');
async function loadHistory() {
  if (!selected) return;
  const id = selected.id;
  if (!historyData) $('history-list').replaceChildren(el('li', 'empty', 'Loading versions…'));
  try {
    const data = await json(api(id, 'history'));
    if (selected?.id === id) { historyData = data; renderHistory(); }
  } catch (e) {
    if (selected?.id === id) $('history-list').replaceChildren(el('li', 'note', e.message));
  }
}
function renderHistory() {
  const h = historyData;
  const busy = Boolean(agent?.busy);
  $('history-count').textContent = h.versions.length || '';
  $('history-on').checked = h.enabled;
  $('history-on').disabled = false;
  $('history-save').disabled = !h.enabled;
  $('history-undo').disabled = !undoTarget() || busy;
  $('history-undo').title = busy ? 'The agent is working: wait for its turn to end before undoing'
    : undoTarget() ? `Put the files back as they were: ${undoTarget().label}` : 'No agent turn to undo yet';
  $('history-hint').textContent = h.enabled
    ? `Snapshots of the project's files, taken before each message to the Master Agent and after each of its turns (the newest ${h.max_versions} are kept).`
    : h.versions.length ? 'Paused: no new versions are saved. The ones below can still be restored.'
      : 'Off for this project. Turn it on to save versions before and after each agent turn, so you can undo them.';
  if (!h.versions.length) {
    $('history-list').replaceChildren(el('li', 'empty', h.enabled ? 'No versions yet: one is saved before your next message to the agent.' : 'No versions saved.'));
    return;
  }
  $('history-list').replaceChildren(...h.versions.map((v, i) => {
    const [kindLabel, pill] = VERSION_KIND[v.kind] || ['Version', ''];
    const item = el('li', `session-card version${i === 0 ? ' latest' : ''}`);
    const head = el('div', 'sc-head');
    head.append(el('strong', 'sc-title', v.label), el('span', `pill ${pill}`, kindLabel));
    if (i === 0) head.append(el('span', 'badge', 'Newest'));
    const facts = el('div', 'sc-facts');
    const delta = [v.added && `+${v.added} new`, v.changed && `${v.changed} changed`, v.removed && `−${v.removed} removed`].filter(Boolean).join(' · ');
    for (const text of [`${new Date(v.t * 1000).toLocaleString()} (${ago(v.t)})`, `${v.files} file${v.files === 1 ? '' : 's'} · ${bytes(v.bytes)}`,
      delta || (i === h.versions.length - 1 ? 'first version' : 'no file changes')]) facts.append(el('span', '', text));
    const body = [head, facts];
    if (v.changed_files?.length) {
      const files = el('details', 'version-files');
      files.append(el('summary', '', 'Changed files'), el('p', 'sc-files', v.changed_files.join('\n')));
      body.push(files);
    }
    if (v.skipped?.length) body.push(el('p', 'sc-line', `Too large to keep (left as they are on restore): ${v.skipped.join(', ')}`));
    const actions = el('div', 'sc-actions');
    const restore = el('button', 'button small', 'Restore this version');
    restore.type = 'button';
    restore.disabled = busy;
    if (busy) restore.title = 'The agent is working: wait for its turn to end';
    restore.addEventListener('click', () => restoreVersion(v, restore));
    actions.append(restore);
    body.push(actions);
    item.append(...body);
    return item;
  }));
}
async function historyAction(body, button) {
  const id = selected.id;
  if (button) button.disabled = true;
  try {
    const data = await json(api(id, 'history'), body);
    if (selected?.id !== id) return null;
    historyData = data;
    renderHistory();
    return data;
  } catch (e) {
    message(e.message, true);
    return null;
  } finally {
    // Re-rendering resets every history control; without data yet, just give the button back.
    if (historyData && selected?.id === id) renderHistory(); else if (button) button.disabled = false;
  }
}
async function restoreVersion(v, button) {
  if (!confirm(`Restore the files to “${v.label}” (${new Date(v.t * 1000).toLocaleString()})?\n\n`
    + 'The current files are saved as a version first, so you can undo this. Hidden files, dependencies and files too large for history are left as they are.')) return;
  if (await historyAction({ action: 'restore', version: v.id }, button)) {
    message(`Restored “${v.label}”.`);
    await openFolder(folder).catch(() => openFolder(''));
    if (activeFile) openFile(activeFile).catch(() => {});
    void refreshProjectInfo();
  }
}
$('view-history').addEventListener('click', () => setTab('history'));
$('history-on').addEventListener('change', async () => {
  const on = $('history-on').checked;
  if (!on && !confirm('Stop saving versions of this project? The versions already saved stay and can still be restored.')) {
    $('history-on').checked = true;
    return;
  }
  if (await historyAction({ action: on ? 'enable' : 'disable' }, $('history-on'))) message(on ? 'Version history is on for this project.' : 'Version history is paused for this project.');
  else $('history-on').checked = !on;
});
$('history-save').addEventListener('click', async () => {
  const data = await historyAction({ action: 'save' }, $('history-save'));
  if (data) message(data.saved ? 'Saved a version of the current files.' : 'Nothing changed since the last version, so there was nothing new to save.');
});
$('history-undo').addEventListener('click', () => { const v = undoTarget(); if (v) restoreVersion(v, $('history-undo')); });

/* ── new project ───────────────────────────────────────────────────────────────────────────── */
function openNew() {
  $('new-form').reset();
  $('new-error').textContent = '';
  $('new-dialog').showModal();
  $('new-name').focus();
}
$('new-project').addEventListener('click', openNew);
$('new-project-side').addEventListener('click', openNew);
$('cancel-new').addEventListener('click', () => $('new-dialog').close());
$('new-start').addEventListener('change', () => { $('new-goal').disabled = !$('new-start').checked; });
$('new-form').addEventListener('submit', async e => {
  e.preventDefault();
  const name = $('new-name').value.trim(), idea = $('new-idea').value.trim();
  if (!name) return;
  if ($('new-start').checked && !idea) { $('new-error').textContent = 'Describe what to build so the agent can start (or untick “Start the Master Agent now”).'; return; }
  $('confirm-new').disabled = true;
  $('new-error').textContent = '';
  try {
    const result = await json('/Code/api/projects', { name, idea, kind: $('new-kind').value, start: $('new-start').checked,
      goal: $('new-goal').checked, budget: $('new-goal').checked ? 'infinite' : '', ...modelChoice() });
    $('new-dialog').close();
    projects = (await json('/Code/api/projects')).projects;
    renderLibrary();
    renderIntro();
    const p = projects.find(x => x.id === result.project.id) || result.project;
    await selectProject(p);
    if (result.agent) { wasBusy = true; message(`Created “${p.name}”. The Master Agent is on it.`); if ($('new-goal').checked) setTab('goals'); }
    else if (result.agent_error) {
      // The folder was made but the agent never got the idea: keep it in the box so one click retries.
      $('ask-text').value = idea;
      message(`Created “${p.name}”, but the Master Agent didn't start: ${result.agent_error} Press “Start coding” to try again.`, true);
    } else message(`Created “${p.name}”. Tell the Master Agent what to build.`);
  } catch (error) {
    $('new-error').textContent = error.message;
  } finally { $('confirm-new').disabled = false; }
});

/* ── events ────────────────────────────────────────────────────────────────────────────────── */
$('up').addEventListener('click', () => openFolder(folder.includes('/') ? folder.slice(0, folder.lastIndexOf('/')) : ''));
$('view-files').addEventListener('click', () => setView(false));
$('view-goals').addEventListener('click', () => setTab('goals'));
$('view-stats').addEventListener('click', () => setTab('stats'));
$('preview-toggle').addEventListener('click', () => {
  if (!selected?.preview || showPreview) return;
  setView(true);
  void loadPreview();
});
// Also re-run after the agent finishes a turn, so the preview shows its latest changes.
async function loadPreview() {
  try {
    const projectId = selected.id;
    const result = await json(api(projectId, 'preview-url'));
    if (showPreview && selected?.id === projectId) $('preview-frame').src = result.url;
    message('');
  } catch (e) {
    setView(false);
    message(e.message, true);
  }
}
$('fullscreen-preview').addEventListener('click', async () => {
  try {
    if (document.fullscreenElement === viewer()) await document.exitFullscreen();
    else await viewer().requestFullscreen();
  } catch { message('Full screen is unavailable in this browser.', true); }
  updateFullscreenButton();
});
document.addEventListener('fullscreenchange', updateFullscreenButton);
document.addEventListener('keydown', e => { if (e.key === 'Escape' && document.fullscreenElement === viewer()) void document.exitFullscreen(); });
$('refresh').addEventListener('click', loadProjects);
document.querySelectorAll('[data-proxy]').forEach(button => button.addEventListener('click', () => $(button.dataset.proxy).click()));
$('open-latest').addEventListener('click', () => {
  const latest = [...projects].sort((a, b) => (b.updated || 0) - (a.updated || 0))[0];
  if (latest) selectProject(latest);
});
$('close-project').addEventListener('click', showHome);
$('project-filter').addEventListener('input', renderLibrary);
$('project-sort').addEventListener('change', () => {
  sortBy = $('project-sort').value;
  try { localStorage.setItem('code.sort', sortBy); } catch { /* storage blocked */ }
  renderLibrary();
});
$('source-seg').addEventListener('click', e => {
  const button = e.target.closest('button[data-source]');
  if (!button) return;
  sourceFilter = button.dataset.source;
  $('source-seg').querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b === button)));
  renderLibrary();
});
function setSidebar(open) {
  $('layout').classList.toggle('sidebar-hidden', !open);
  $('sidebar-open').hidden = open;
  $('sidebar-open').setAttribute('aria-expanded', String(open));
  try { localStorage.setItem('code.sidebar', open ? 'open' : 'closed'); } catch { /* storage blocked */ }
}
$('sidebar-close').addEventListener('click', () => setSidebar(false));
$('sidebar-open').addEventListener('click', () => setSidebar(true));
window.addEventListener('hashchange', () => {
  const id = new URLSearchParams(location.hash.slice(1)).get('p');
  const p = projects.find(item => item.id === id);
  if (p && p.id !== selected?.id) selectProject(p);
  else if (!id && selected) showHome();
});

$('delete-project').addEventListener('click', () => {
  if (!selected) return;
  $('delete-description').textContent = `This permanently deletes “${selected.name}” and every file in its project folder.`;
  $('delete-name').value = '';
  $('delete-description').textContent += ' Its Master Agent sessions stay on the Agent page.';
  $('delete-error').textContent = '';
  $('confirm-delete').disabled = true;
  $('delete-dialog').showModal();
  $('delete-name').focus();
});
$('cancel-delete').addEventListener('click', () => $('delete-dialog').close());
$('delete-name').addEventListener('input', () => { $('confirm-delete').disabled = !selected || $('delete-name').value !== selected.name; });
$('delete-form').addEventListener('submit', async e => {
  e.preventDefault();
  if (!selected || $('delete-name').value !== selected.name) return;
  const project = selected;
  $('confirm-delete').disabled = true;
  $('cancel-delete').disabled = true;
  $('delete-error').textContent = '';
  try {
    await json(`/Code/api/projects/${encodeURIComponent(project.id)}`, { name: $('delete-name').value }, 'DELETE');
    $('delete-dialog').close();
    showHome();
    await loadProjects();
    message(`Deleted “${project.name}” and all its files.`);
  } catch (error) {
    $('delete-error').textContent = error.message;
    $('confirm-delete').disabled = false;
  } finally {
    $('cancel-delete').disabled = false;
  }
});

try {
  sortBy = localStorage.getItem('code.sort') || sortBy;
  if (localStorage.getItem('code.sidebar') === 'closed') setSidebar(false);
} catch { /* storage blocked */ }
$('project-sort').value = sortBy;
loadModels();
loadProjects();
