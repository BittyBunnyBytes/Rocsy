const $ = id => document.getElementById(id);
let engines = [];
let voices = [];
let posting = false;
let category = '';
let jobFilter = 'all';
let pollTimer = null;
let settingsSections = [];
let settingsSelected = '';
const settingsDrafts = {};
const fresh = new Set();  // generations started from this tab, highlighted once they land
const preview = new Audio();  // one character sample plays at a time
preview.preload = 'none';

async function api(path, options) {
  const response = await fetch('/Voice/api/' + path, options);
  let data;
  try { data = await response.json(); }
  catch { throw Error('Voice service unavailable'); }
  if (!response.ok) throw Error(data.detail || data.error || 'Request failed');
  return data;
}

function setStatus(message, kind = '') {
  $('status').textContent = message;
  $('status').className = 'footer-status' + (kind ? ' is-' + kind : '');
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

const ICONS = {
  play: '<path d="M8 5.5v13l10.5-6.5z" fill="currentColor" stroke="none"/>',
  reuse: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
  trash: '<path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l1 12.5h9l1-12.5"/>',
  cancel: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
};
function iconButton(name, label, className = '') {
  const button = el('button', 'secondary-button job-icon-button ' + className);
  button.type = 'button';
  button.title = label;
  button.setAttribute('aria-label', label);
  button.innerHTML = window.BunnyStudioIcons?.icon(name) ||
    `<svg class="studio-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ''}</svg>`;
  return button;
}

// Portrait if the character has one, else its emoji, else its initial.
function avatar(voice, className) {
  const box = el('span', className);
  if (voice?.portrait) {
    const img = el('img');
    img.src = voice.portrait;
    img.alt = '';
    img.loading = 'lazy';
    img.decoding = 'async';
    box.append(img);
  } else {
    box.textContent = voice?.icon || (voice?.name || '?').trim().charAt(0).toUpperCase();
    box.classList.add('is-glyph');
  }
  return box;
}

// ---- Views: Studio | Characters | Add character (H3 Machine style tabs) ----
const VIEWS = ['studio', 'characters', 'add', 'settings'];
function setView(view, {focus = false, keepHash = false} = {}) {
  if (!VIEWS.includes(view)) view = 'studio';
  if (view !== 'characters') stopPreview();
  document.body.dataset.view = view;
  document.querySelectorAll('[data-view-page]').forEach(page => { page.hidden = page.dataset.viewPage !== view; });
  document.querySelectorAll('.view-tab').forEach(tab => {
    const active = tab.dataset.view === view;
    tab.classList.toggle('is-active', active);
    tab.setAttribute('aria-selected', String(active));
  });
  if (!keepHash) try { history.replaceState(null, '', view === 'studio' ? location.pathname + location.search : '#' + view); } catch { /* ignore */ }
  if (view === 'add' && builder.duration) requestAnimationFrame(drawWave);
  if (view === 'settings') void ensureSettings();
  if (focus) window.scrollTo(0, 0);
}
document.querySelectorAll('.view-tab').forEach(tab => tab.addEventListener('click', () => setView(tab.dataset.view, {focus: true})));
document.querySelectorAll('[data-go-view]').forEach(button => button.addEventListener('click', () => setView(button.dataset.goView, {focus: true})));
$('browse-characters').addEventListener('click', () => setView('characters', {focus: true}));
$('open-engine-settings').addEventListener('click', () => {
  settingsSelected = $('engine').value;
  setView('settings', {focus: true});
});

// ---- Saved settings for each speech engine ----
function settingsStatus(message, kind = '') {
  $('settings-status').textContent = message;
  $('settings-status').className = 'field-hint' + (kind ? ' is-' + kind : '');
}

async function ensureSettings() {
  if (settingsSections.length) { renderSettings(); return; }
  settingsStatus('Loading settings…');
  try {
    const data = await api('settings');
    settingsSections = data.engines;
    settingsSelected ||= $('engine').value || settingsSections[0]?.id || '';
    renderSettings();
    settingsStatus('Changes are saved per engine.');
  } catch (error) { settingsStatus(error.message, 'error'); }
}

function currentSettings() {
  return settingsSections.find(item => item.id === settingsSelected);
}

function readSettingsForm() {
  const values = {};
  for (const input of $('settings-fields').querySelectorAll('[data-setting]')) {
    values[input.dataset.setting] = input.type === 'checkbox' ? input.checked : input.value;
  }
  return values;
}

function rememberSettingsDraft() {
  const section = currentSettings();
  if (!section) return false;
  const values = readSettingsForm();
  const dirty = section.fields.some(field => String(values[field.key]) !== String(section.values[field.key]));
  if (dirty) settingsDrafts[section.id] = values;
  else delete settingsDrafts[section.id];
  return dirty;
}

function renderSettings() {
  const section = currentSettings();
  if (!section) return;
  $('settings-engine-tabs').replaceChildren(...settingsSections.map(item => {
    const button = el('button', 'settings-engine-tab' + (item.id === settingsSelected ? ' is-active' : ''), item.name);
    button.type = 'button';
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-selected', String(item.id === settingsSelected));
    button.addEventListener('click', () => {
      rememberSettingsDraft();
      settingsSelected = item.id;
      renderSettings();
      settingsStatus(settingsDrafts[item.id] ? 'Unsaved changes.' : 'Changes are saved per engine.');
    });
    return button;
  }));
  $('settings-engine-name').textContent = section.name;
  $('settings-engine-description').textContent = section.description;
  const values = settingsDrafts[section.id] || section.values;
  const groups = new Map();
  for (const field of section.fields) {
    if (!groups.has(field.group)) groups.set(field.group, []);
    const row = el('div', 'settings-field');
    const id = 'setting-' + field.key;
    const input = el(field.kind === 'choice' ? 'select' : 'input');
    input.id = id;
    input.dataset.setting = field.key;
    if (field.kind === 'choice') {
      const label = el('label', '', field.label);
      label.htmlFor = id;
      input.append(...field.options.map(option => {
        const item = new Option(option.label, option.value);
        item.disabled = Boolean(option.disabled) && option.value !== values[field.key];
        return item;
      }));
      input.value = values[field.key] ?? field.default;
      row.append(label, input);
    } else if (field.kind === 'toggle') {
      input.type = 'checkbox';
      input.checked = Boolean(values[field.key]);
      const label = el('label', 'settings-toggle');
      label.htmlFor = id;
      label.append(input, el('span', '', field.label));
      row.append(label);
    } else {
      const label = el('label', '', field.label);
      label.htmlFor = id;
      input.type = field.kind === 'number' ? 'number' : 'text';
      input.value = values[field.key] ?? '';
      if (field.kind === 'number') {
        input.min = field.min;
        input.max = field.max;
        input.step = field.step;
        input.required = true;
      } else input.maxLength = 120;
      row.append(label, input);
    }
    row.append(el('small', 'field-hint', field.hint));
    groups.get(field.group).push(row);
  }
  $('settings-fields').replaceChildren(...[...groups].map(([title, fields]) => {
    const group = el('fieldset', 'settings-group');
    group.append(el('legend', '', title), el('div', 'settings-grid'));
    group.querySelector('.settings-grid').append(...fields);
    return group;
  }));
  $('settings-fields').oninput = () => {
    settingsStatus(rememberSettingsDraft() ? 'Unsaved changes.' : 'Changes are saved per engine.');
  };
}

$('settings-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!event.target.reportValidity()) return;
  const section = currentSettings();
  if (!section) return;
  const values = {};
  for (const field of section.fields) {
    const input = $('setting-' + field.key);
    values[field.key] = field.kind === 'toggle' ? input.checked
      : field.kind === 'number' ? Number(input.value) : input.value;
  }
  $('settings-save').disabled = true;
  try {
    const result = await api('settings/' + section.id, {method: 'PUT',
      headers: {'content-type': 'application/json'}, body: JSON.stringify(values)});
    section.values = result.values;
    delete settingsDrafts[section.id];
    renderSettings();
    settingsStatus(`${section.name} settings saved.`, 'success');
  } catch (error) { settingsStatus(error.message, 'error'); }
  finally { $('settings-save').disabled = false; }
});

$('settings-reset').addEventListener('click', async () => {
  const section = currentSettings();
  if (!section) return;
  $('settings-reset').disabled = true;
  try {
    const result = await api('settings/' + section.id, {method: 'DELETE'});
    section.values = result.values;
    delete settingsDrafts[section.id];
    renderSettings();
    settingsStatus(`${section.name} restored to defaults.`, 'success');
  } catch (error) { settingsStatus(error.message, 'error'); }
  finally { $('settings-reset').disabled = false; }
});

// ---- Character pick (Studio card) + gallery ----
function showPickedVoice() {
  const voice = voices.find(item => item.id === $('voice').value);
  $('voice-avatar').replaceWith(Object.assign(avatar(voice, 'picked-avatar'), {id: 'voice-avatar'}));
  $('character-badge').textContent = voice?.category || '';
  $('voice-summary-line').textContent = voice ? (voice.summary || voice.transcript_text || `${Math.round(voice.duration || 0)} s reference`) : 'Add a character to begin.';
  $('picked-play').disabled = !voice;
  document.querySelectorAll('.character').forEach(card =>
    card.classList.toggle('is-selected', card.dataset.voice === $('voice').value));
}

function stopPreview() {
  preview.pause();
  document.querySelectorAll('.character.is-playing').forEach(card => {
    card.classList.remove('is-playing');
    const button = card.querySelector('.play-button');
    button.setAttribute('aria-pressed', 'false');
    button.querySelector('span').textContent = 'Play sample';
  });
  $('picked-play').setAttribute('aria-pressed', 'false');
  $('picked-play').textContent = 'Play sample';
}
preview.addEventListener('ended', stopPreview);

$('picked-play').addEventListener('click', () => {
  const playing = $('picked-play').getAttribute('aria-pressed') === 'true';
  stopPreview();
  const voice = voices.find(item => item.id === $('voice').value);
  if (playing || !voice) return;
  preview.src = voice.reference;
  preview.play().then(() => {
    $('picked-play').setAttribute('aria-pressed', 'true');
    $('picked-play').textContent = 'Stop';
  }).catch(() => setStatus(`Couldn't play ${voice.name}'s sample.`, 'error'));
});

function useVoice(id) {
  $('voice').value = id;
  showPickedVoice();
  update();
  setView('studio', {focus: true});
  $('text').focus({preventScroll: true});
  setStatus(`Using ${voices.find(item => item.id === id)?.name || 'that character'}.`, 'success');
}

function characterCard(voice) {
  const card = el('article', 'character');
  card.dataset.voice = voice.id;
  card.classList.toggle('is-selected', voice.id === $('voice').value);

  const art = el('div', 'character-art');
  art.append(avatar(voice, 'character-portrait'));
  if (voice.category) art.append(el('span', 'character-tag', voice.category));
  art.append(el('span', 'character-current', 'In use'));

  const body = el('div', 'character-body');
  body.append(el('h3', 'character-name', voice.name));
  const facts = [voice.duration ? `${Math.round(voice.duration)} s reference` : 'Reference clip',
    voice.transcript ? 'Transcript' : 'No transcript'];
  body.append(el('p', 'character-facts', facts.join(' · ')));
  if (voice.summary) body.append(el('p', 'character-summary', voice.summary));
  if (voice.transcript_text) {
    const quote = el('blockquote', 'character-quote', `“${voice.transcript_text}”`);
    quote.title = voice.transcript_text;
    body.append(quote);
  }

  const actions = el('div', 'character-actions');
  const play = el('button', 'secondary-button play-button');
  play.type = 'button';
  play.setAttribute('aria-pressed', 'false');
  play.setAttribute('aria-label', `Play ${voice.name}'s reference sample`);
  play.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path class="i-play" d="M8 5.5v13l10.5-6.5z" fill="currentColor"/><path class="i-stop" d="M7 7h10v10H7z" fill="currentColor"/></svg>';
  play.append(el('span', '', 'Play sample'));
  play.addEventListener('click', () => {
    const playing = card.classList.contains('is-playing');
    stopPreview();
    if (playing) return;
    preview.src = voice.reference;
    preview.play().then(() => {
      card.classList.add('is-playing');
      play.setAttribute('aria-pressed', 'true');
      play.querySelector('span').textContent = 'Stop';
    }).catch(() => setStatus(`Couldn't play ${voice.name}'s sample.`, 'error'));
  });
  const use = el('button', 'primary-button use-button', 'Use voice');
  use.type = 'button';
  use.addEventListener('click', () => useVoice(voice.id));
  const remove = el('button', 'secondary-button icon-danger');
  remove.type = 'button';
  remove.title = `Delete ${voice.name}`;
  remove.setAttribute('aria-label', `Delete ${voice.name} and its reference audio`);
  remove.innerHTML = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" aria-hidden="true"><path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l1 12.5h9l1-12.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  remove.addEventListener('click', async () => {
    if (!confirm(`Delete ${voice.name} and its reference audio?`)) return;
    remove.disabled = true;
    try {
      stopPreview();
      await api('voices/' + encodeURIComponent(voice.id), {method: 'DELETE'});
      await refresh(false);
      setStatus(`${voice.name} deleted.`, 'success');
    } catch (error) {
      remove.disabled = false;
      setStatus(error.message, 'error');
    }
  });
  actions.append(play, use, remove);
  body.append(actions);
  card.append(art, body);
  return card;
}

function renderCharacters() {
  const categories = [...new Set(voices.map(voice => voice.category).filter(Boolean))].sort();
  $('category-list').replaceChildren(...categories.map(name => new Option(name)));
  $('character-count').textContent = voices.length ? String(voices.length) : '';
  if (category && !categories.includes(category)) category = '';
  $('category-chips').replaceChildren(...(categories.length < 2 ? [] : ['', ...categories].map(name => {
    const chip = el('button', 'chip', name || 'All');
    chip.type = 'button';
    chip.setAttribute('aria-pressed', String(name === category));
    chip.addEventListener('click', () => { category = name; renderCharacters(); });
    return chip;
  })));
  const query = $('character-search').value.trim().toLowerCase();
  const shown = voices.filter(voice => (!category || voice.category === category)
    && (!query || [voice.name, voice.category, voice.summary, voice.transcript_text].join(' ').toLowerCase().includes(query)));
  stopPreview();
  if (!shown.length) {
    $('character-grid').replaceChildren(el('p', 'library-empty',
      voices.length ? 'No characters match.' : 'No characters yet. Use Add character.'));
    return;
  }
  $('character-grid').replaceChildren(...shown.map(characterCard));
}
$('character-search').addEventListener('input', renderCharacters);

function update() {
  const selected = engines.find(item => item.id === $('engine').value);
  const indexSelected = selected?.id === 'indextts2';
  $('index-emotion-guide').hidden = !indexSelected;
  $('text').placeholder = indexSelected
    ? '[EMO=text,TEXT=warm and calm]Welcome home.[/]'
    : 'Write the words you want spoken…';
  $('generate').disabled = posting || !selected?.ready || !$('voice').value || !$('text').value.trim();
  const length = $('text').value.length;
  $('char-count').textContent = length ? `${length.toLocaleString()} chars` : '';
  $('model-badge').textContent = selected ? selected.name.replace(/ \d.*$/, '') : '';
  document.querySelectorAll('.engine').forEach(row => row.classList.toggle('is-current', row.dataset.engine === $('engine').value));
}

// ---- Generations: in-memory jobs (queued/running/failed) + saved WAVs ----
const ACTIVE = new Set(['queued', 'running']);
const STATE_LABEL = {queued: 'Queued', running: 'Generating', complete: 'Complete', failed: 'Failed'};
const STATE_CLASS = {queued: 'state-queued', running: 'state-rendering', complete: 'state-complete', failed: 'state-failed'};
const engineName = id => engines.find(item => item.id === id)?.name || id || 'Unknown engine';

function generationItems(state) {
  const items = new Map();
  for (const output of state.outputs) items.set(output.id, {...output, state: 'complete'});
  for (const job of state.jobs) {
    if (job.kind) continue;
    if (job.state === 'cancelled' || (job.state === 'complete' && !items.has(job.id))) continue;
    if (!items.has(job.id)) items.set(job.id, job);
  }
  return [...items.values()].sort((a, b) => b.created - a.created);
}

function reuse(item) {
  if (item.voice_id && voices.some(voice => voice.id === item.voice_id)) $('voice').value = item.voice_id;
  if (item.engine && engines.some(engine => engine.id === item.engine)) $('engine').value = item.engine;
  if (item.language) $('language').value = item.language;
  if (item.text) $('text').value = item.text;
  showPickedVoice();
  update();
  document.body.dataset.mobileTab === 'jobs' && document.querySelector('.mobile-tab[data-mobile-tab="create"]')?.click();
  $('text').focus();
  setStatus('Script and model loaded from that generation.', 'success');
}

function jobCard(item) {
  const voice = voices.find(entry => entry.id === item.voice_id);
  const card = el('article', 'job-card');
  card.dataset.id = item.id;
  card.dataset.state = item.state;
  card.classList.toggle('is-queued', item.state === 'queued');
  card.classList.toggle('is-fresh', fresh.has(item.id) && item.state === 'complete');

  const head = el('div', 'job-head');
  head.append(avatar(voice || {name: item.voice_id ? '?' : '♪'}, 'job-avatar'));
  const who = el('div', 'job-who');
  who.append(el('strong', '', voice?.name || (item.voice_id ? 'Deleted character' : 'Generated speech')),
    el('small', '', new Date(item.created * 1000).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'})));
  head.append(who, el('span', 'job-state ' + (STATE_CLASS[item.state] || ''), STATE_LABEL[item.state] || item.state));
  card.append(head);

  const script = el('p', 'job-script' + (item.text ? '' : ' is-empty'), item.text || 'Script not recorded for this file.');
  if (item.text) script.title = item.text;
  card.append(script);
  const meta = el('div', 'job-meta');
  for (const part of [item.engine && engineName(item.engine), item.language, item.state === 'complete' && `#${item.id.slice(0, 8)}`]) {
    if (part) meta.append(el('span', '', part));
  }
  if (meta.childElementCount) card.append(meta);

  if (item.state === 'running' && item.chunks > 1) meta.append(el('span', 'job-chunk', `Part ${item.chunk || 1} / ${item.chunks}`));
  if (ACTIVE.has(item.state)) card.append(el('div', 'job-progress'));
  if (item.state === 'failed') card.append(el('div', 'job-error', item.error || 'Generation failed'));
  if (item.state === 'complete' && item.audio) {
    const player = el('audio');
    player.controls = true;
    player.preload = 'none';
    player.src = item.audio;
    player.setAttribute('aria-label', `Speech from ${who.querySelector('small').textContent}`);
    player.addEventListener('play', () => { stopPreview(); card.classList.remove('is-fresh'); fresh.delete(item.id); });
    card.append(player);
  }

  const actions = el('div', 'job-actions');
  if (item.text) {
    const again = iconButton('retry', 'Reuse script and settings');
    again.addEventListener('click', () => reuse(item));
    actions.append(again);
  }
  actions.append(el('span', 'grow'));
  if (item.state === 'complete' && item.audio) {
    const download = Object.assign(el('a', 'secondary-button job-icon-button'), {href: item.audio, download: `voice-${item.id.slice(0, 8)}.wav`, title: 'Download WAV'});
    download.setAttribute('aria-label', 'Download WAV');
    download.innerHTML = iconButton('download', '').innerHTML;
    const remove = iconButton('trash', 'Delete this audio', 'is-danger');
    remove.addEventListener('click', async () => {
      if (!confirm('Delete this generated audio file? This cannot be undone.')) return;
      remove.disabled = true;
      try {
        await api('audio/' + encodeURIComponent(item.id), {method: 'DELETE'});
        card.remove();
        await refresh(false);
        setStatus('Audio deleted.', 'success');
      } catch (error) {
        remove.disabled = false;
        setStatus(error.message, 'error');
      }
    });
    actions.append(download, remove);
  } else if (item.state === 'queued') {
    const cancel = iconButton('cancel', 'Cancel', 'is-danger');
    cancel.addEventListener('click', async () => {
      cancel.disabled = true;
      try { await api(`jobs/${item.id}/cancel`, {method: 'POST'}); await refresh(false); }
      catch (error) { cancel.disabled = false; setStatus(error.message, 'error'); }
    });
    actions.append(cancel);
  }
  card.append(actions);
  return card;
}

function applyJobFilter() {
  const query = $('job-search').value.trim().toLowerCase();
  let visible = 0;
  for (const card of $('jobs-list').querySelectorAll('.job-card')) {
    const state = card.dataset.state;
    const matchesTab = jobFilter === 'all' || (jobFilter === 'running' ? ACTIVE.has(state) : state === jobFilter);
    const show = matchesTab && (!query || card.textContent.toLowerCase().includes(query));
    card.hidden = !show;
    visible += show;
  }
  let empty = $('jobs-list').querySelector('.jobs-empty');
  const total = $('jobs-list').querySelectorAll('.job-card').length;
  if (!visible) {
    if (!empty) { empty = el('div', 'jobs-empty'); $('jobs-list').append(empty); }
    empty.textContent = total ? 'Nothing matches.' : 'No speech yet. Pick a character, write a script and hit Generate.';
  } else empty?.remove();
}

function renderGenerations(state) {
  const items = generationItems(state);
  const list = $('jobs-list');
  const existing = new Map([...list.querySelectorAll('.job-card')].map(card => [card.dataset.id, card]));
  // Keyed redraw: cards whose state didn't change stay put, so a playing clip keeps playing.
  const nodes = items.map(item => {
    const key = [item.state, item.chunk || '', item.audio || '', item.error || '', voices.find(v => v.id === item.voice_id)?.name || ''].join('|');
    const old = existing.get(item.id);
    if (old && old.dataset.key === key) return old;
    const card = jobCard(item);
    card.dataset.key = key;
    return card;
  });
  for (const [id, card] of existing) if (!items.some(item => item.id === id)) card.remove();
  nodes.forEach((node, index) => { if (list.children[index] !== node) list.insertBefore(node, list.children[index] || null); });
  const counts = {all: items.length, running: items.filter(item => ACTIVE.has(item.state)).length,
    complete: items.filter(item => item.state === 'complete').length};
  for (const [name, count] of Object.entries(counts)) {
    const badge = document.querySelector(`[data-job-count="${name}"]`);
    if (badge) badge.textContent = count ? String(count) : '';
  }
  applyJobFilter();
  return counts.running;
}
document.querySelectorAll('[data-job-filter]').forEach(tab => tab.addEventListener('click', () => {
  jobFilter = tab.dataset.jobFilter;
  document.querySelectorAll('[data-job-filter]').forEach(other => {
    other.classList.toggle('is-active', other === tab);
    other.setAttribute('aria-selected', String(other === tab));
  });
  applyJobFilter();
}));
$('job-search').addEventListener('input', applyJobFilter);

let lastActive = 0;
async function refresh(showReady = true) {
  try {
    const state = await api('state');
    engines = state.engines;
    renderCreators(state.creators || []);
    const selectedEngine = $('engine').value;
    const selectedVoice = $('voice').value;
    $('engine').replaceChildren(...engines.map(item => new Option(item.name, item.id)));
    if (engines.some(item => item.id === selectedEngine)) $('engine').value = selectedEngine;
    voices = state.voices;
    $('voice').replaceChildren(...voices.map(item => new Option(item.category ? `${item.name} · ${item.category}` : item.name, item.id)));
    if (voices.some(item => item.id === selectedVoice)) $('voice').value = selectedVoice;
    // Only redraw the gallery when the characters changed (it would cut off a playing sample).
    const key = JSON.stringify(voices);
    if (key !== renderCharacters.key) { renderCharacters.key = key; renderCharacters(); }
    showPickedVoice();
    $('models').replaceChildren(...engines.map(item => {
      const row = el('div', 'engine');
      row.dataset.engine = item.id;
      const info = el('div');
      info.append(el('strong', '', item.name), el('small', '', item.detail));
      row.append(info, el('span', 'availability' + (item.ready ? '' : ' bad'), item.ready ? 'Ready' : 'Offline'));
      return row;
    }));
    const active = renderGenerations(state);
    if (lastActive && !active) setStatus('Speech ready.', 'success');
    lastActive = active;
    $('service-state').querySelector('.status-dot').classList.add('is-online');
    $('service-label').textContent = 'Online';
    update();
    if (showReady) setStatus(state.voices.length ? 'Ready to generate.' : 'Add a character to begin.');
  } catch (error) {
    $('service-state').querySelector('.status-dot').classList.remove('is-online');
    $('service-label').textContent = 'Offline';
    setStatus(error.message, 'error');
  }
  clearTimeout(pollTimer);
  pollTimer = setTimeout(() => refresh(false), lastActive ? 2000 : 15000);
}

for (const id of ['engine', 'voice', 'text']) {
  $(id).addEventListener(id === 'text' ? 'input' : 'change', update);
}
$('voice').addEventListener('change', showPickedVoice);

$('generate').addEventListener('click', async () => {
  posting = true;
  update();
  setStatus('Queued for GPU…');
  try {
    const job = await api('generate', {method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({engine: $('engine').value, voice_id: $('voice').value,
        text: $('text').value, language: $('language').value})});
    fresh.add(job.id);
    lastActive = 1;
    await refresh(false);
    setStatus('Queued. It shows under Generations.');
  } catch (error) { setStatus(error.message, 'error'); }
  finally { posting = false; update(); }
});

// ---- Add a character: upload -> pick clip -> clean (GPU music removal) -> save ----
const builder = {upload: null, duration: 0, clip: null, peaks: null, source: new Audio(), stopAt: 0, token: 0};
builder.source.preload = 'auto';

async function waitJob(id, onStage) {
  for (;;) {
    const job = await api('jobs/' + id);
    if (job.state === 'complete') return job;
    if (job.state === 'failed') throw Error(job.error || 'Failed');
    onStage(job.stage || 'Working…');
    await new Promise(resolve => setTimeout(resolve, 1200));
  }
}

const fmt = seconds => `${(Math.round(seconds * 10) / 10).toFixed(1)} s`;

function clipWindow() {
  const start = Number($('clip-start').value);
  const length = Math.min(Number($('clip-length').value), Math.max(1, builder.duration - start));
  return {start, length};
}

function drawWave() {
  const canvas = $('wave');
  const width = canvas.clientWidth || 600;
  canvas.width = width * devicePixelRatio;
  canvas.height = 84 * devicePixelRatio;
  const ctx = canvas.getContext('2d');
  ctx.scale(devicePixelRatio, devicePixelRatio);
  const css = getComputedStyle(document.documentElement);
  const accent = css.getPropertyValue('--blue').trim() || '#7cb3dc';
  const muted = css.getPropertyValue('--border-strong').trim() || '#354550';
  const {start, length} = clipWindow();
  const x0 = start / builder.duration * width;
  const x1 = (start + length) / builder.duration * width;
  ctx.fillStyle = accent + '22';
  ctx.fillRect(x0, 0, x1 - x0, 84);
  const peaks = builder.peaks;
  for (let x = 0; x < width; x++) {
    const value = peaks ? peaks[Math.floor(x / width * peaks.length)] : 0.15;
    const h = Math.max(1, value * 76);
    ctx.fillStyle = x >= x0 && x <= x1 ? accent : muted;
    ctx.fillRect(x, 42 - h / 2, 1, h);
  }
  ctx.strokeStyle = accent;
  ctx.lineWidth = 2;
  ctx.strokeRect(x0 + 1, 1, Math.max(2, x1 - x0 - 2), 82);
}

function syncClipControls() {
  const max = Math.max(0, builder.duration - 1);
  $('clip-start').max = max.toFixed(1);
  $('clip-length').max = Math.min(15, Math.max(1, builder.duration)).toFixed(1);
  if (Number($('clip-start').value) > max) $('clip-start').value = max;
  const {start, length} = clipWindow();
  $('clip-start-out').textContent = fmt(start);
  $('clip-length-out').textContent = fmt(length);
  if (builder.duration) drawWave();
}

function markStale(message = 'Settings changed. Build the clip again.') {
  if (!builder.clip) return;
  builder.clip = null;
  $('save-character').disabled = true;
  $('transcribe').disabled = true;
  $('save-hint').textContent = message;
  $('build-state').textContent = message;
}

function updateBuildable() {
  $('build-clip').disabled = !builder.upload;
  $('fx-music-out').textContent = $('fx-music').value + '%';
  $('fx-noise-out').textContent = $('fx-noise').value + '%';
}

async function decodePeaks(file) {
  try {
    const context = new (window.AudioContext || window.webkitAudioContext)();
    const buffer = await context.decodeAudioData(await file.arrayBuffer());
    context.close();
    const data = buffer.getChannelData(0);
    const buckets = 1200, size = Math.max(1, Math.floor(data.length / buckets));
    const peaks = new Float32Array(buckets);
    let top = 0;
    for (let i = 0; i < buckets; i++) {
      let peak = 0;
      for (let j = i * size, end = Math.min(data.length, j + size); j < end; j += 8) peak = Math.max(peak, Math.abs(data[j]));
      peaks[i] = peak;
      top = Math.max(top, peak);
    }
    return top ? peaks.map(value => value / top) : peaks;
  } catch { return null; }  // video containers etc.: the clip sliders still work
}

async function chooseRecording(file) {
  if (!file) return;
  const token = ++builder.token;
  $('youtube-download').disabled = false;
  $('youtube-state').textContent = 'Paste a video link to import its voice recording (up to 30 minutes).';
  stopSelection();
  builder.upload = null;
  builder.duration = 0;
  builder.peaks = null;
  markStale('');
  builder.clip = null;
  $('clip-picker').hidden = true;
  $('compare').hidden = true;
  $('drop-title').textContent = file.name;
  $('drop-hint').textContent = 'Uploading…';
  updateBuildable();
  if (builder.source.src.startsWith('blob:')) URL.revokeObjectURL(builder.source.src);
  builder.source.src = URL.createObjectURL(file);
  const peaks = decodePeaks(file);
  try {
    const body = new FormData();
    body.append('audio', file);
    const uploaded = await api('reference/upload', {method: 'POST', body});
    if (token !== builder.token) return;
    builder.upload = uploaded.id;
    builder.duration = uploaded.duration;
    builder.peaks = await peaks;
    if (token !== builder.token) return;
    if (!builder.peaks) builder.peaks = await fetchWaveform(uploaded.id);
    if (token !== builder.token) return;
    $('clip-start').value = 0;
    $('clip-length').value = Math.min(15, uploaded.duration);
    $('clip-picker').hidden = false;
    $('drop-hint').textContent = `${fmt(uploaded.duration)} long. Pick the clip, then build it.`;
    if (!$('voice-name').value) $('voice-name').value = file.name.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ').slice(0, 100);
    syncClipControls();
  } catch (error) {
    if (token === builder.token) $('drop-hint').textContent = error.message;
  }
  if (token === builder.token) updateBuildable();
}

async function fetchWaveform(uploadId) {
  try {
    const data = await api(`reference/${uploadId}/waveform`);
    return data.peaks;
  } catch { return null; }
}

async function importYouTube() {
  if ($('youtube-download').disabled) return;
  const url = $('youtube-url').value.trim();
  if (!url) { $('youtube-state').textContent = 'Paste a YouTube video link first.'; return; }
  const token = ++builder.token;
  stopSelection();
  builder.upload = builder.clip = null;
  builder.duration = 0;
  builder.peaks = null;
  $('clip-picker').hidden = $('compare').hidden = true;
  $('save-character').disabled = true;
  $('transcribe').disabled = true;
  $('save-hint').textContent = 'Build the cleaned clip first.';
  $('youtube-download').disabled = true;
  $('youtube-state').textContent = 'Starting download…';
  updateBuildable();
  try {
    const job = await api('reference/youtube', {method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({url})});
    const done = await waitJob(job.id, stage => {
      if (token === builder.token) $('youtube-state').textContent = stage;
    });
    if (token !== builder.token) return;
    builder.upload = done.upload_id;
    builder.duration = done.duration;
    if (builder.source.src.startsWith('blob:')) URL.revokeObjectURL(builder.source.src);
    builder.source.src = `/Voice/api/reference/${done.upload_id}/source`;
    builder.peaks = await fetchWaveform(done.upload_id);
    if (token !== builder.token) return;
    $('clip-start').value = 0;
    $('clip-length').value = Math.min(15, done.duration);
    $('clip-picker').hidden = false;
    $('drop-title').textContent = done.name || 'YouTube audio';
    $('drop-hint').textContent = `${fmt(done.duration)} long. Pick the clip, then build it.`;
    $('youtube-state').replaceChildren(document.createTextNode('Audio ready. '));
    const download = document.createElement('a');
    download.href = builder.source.src;
    download.download = 'youtube-voice.mp3';
    download.textContent = 'Download audio file';
    $('youtube-state').append(download);
    if (!$('voice-name').value) $('voice-name').value = (done.name || 'YouTube voice').slice(0, 100);
    syncClipControls();
  } catch (error) {
    if (token === builder.token) $('youtube-state').textContent = error.message;
  } finally {
    if (token === builder.token) { $('youtube-download').disabled = false; updateBuildable(); }
  }
}

$('youtube-download').addEventListener('click', importYouTube);
$('youtube-url').addEventListener('keydown', event => {
  if (event.key === 'Enter') { event.preventDefault(); importYouTube(); }
});

function stopSelection() {
  builder.source.pause();
  $('play-selection').textContent = 'Play selection';
}
builder.source.addEventListener('timeupdate', () => {
  if (!builder.source.paused && builder.source.currentTime >= builder.stopAt) stopSelection();
});

$('voice-audio').addEventListener('change', event => chooseRecording(event.target.files[0]));
$('drop').addEventListener('dragover', event => { event.preventDefault(); $('drop').classList.add('is-over'); });
$('drop').addEventListener('dragleave', () => $('drop').classList.remove('is-over'));
$('drop').addEventListener('drop', event => {
  event.preventDefault();
  $('drop').classList.remove('is-over');
  chooseRecording(event.dataTransfer.files[0]);
});
for (const id of ['clip-start', 'clip-length']) {
  $(id).addEventListener('input', () => { syncClipControls(); markStale(); });
}
$('wave').addEventListener('click', event => {
  const box = $('wave').getBoundingClientRect();
  const {length} = clipWindow();
  $('clip-start').value = Math.max(0, (event.clientX - box.left) / box.width * builder.duration - length / 2).toFixed(1);
  syncClipControls();
  markStale();
});
window.addEventListener('resize', () => { if (builder.duration) drawWave(); });
$('play-selection').addEventListener('click', () => {
  if (!builder.source.paused) return stopSelection();
  const {start, length} = clipWindow();
  builder.source.currentTime = start;
  builder.stopAt = start + length;
  builder.source.play().then(() => { $('play-selection').textContent = 'Stop'; }).catch(() => {});
});
for (const id of ['fx-music', 'fx-noise', 'fx-normalize', 'fx-focus']) {
  $(id).addEventListener('input', () => { updateBuildable(); markStale(); });
}

$('build-clip').addEventListener('click', async () => {
  stopSelection();
  const {start, length} = clipWindow();
  $('build-clip').disabled = true;
  $('build-state').textContent = 'Starting…';
  try {
    const job = await api(`reference/${builder.upload}/clean`, {method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({start, length, remove_music: Number($('fx-music').value), noise: Number($('fx-noise').value),
        normalize: $('fx-normalize').checked, focus: $('fx-focus').checked})});
    const done = await waitJob(job.id, stage => { $('build-state').textContent = stage; });
    builder.clip = job.id;
    $('cleaned').src = done.audio;
    $('compare').hidden = false;
    $('build-state').textContent = `Cleaned ${fmt(done.duration)} clip ready. Listen, then save.`;
    $('save-character').disabled = false;
    $('transcribe').disabled = false;
    $('save-hint').textContent = '';
  } catch (error) {
    $('build-state').textContent = error.message;
  } finally { updateBuildable(); }
});

$('transcribe').addEventListener('click', async () => {
  const clip = builder.clip;
  $('transcribe').disabled = true;
  try {
    const job = await api(`reference/${clip}/transcribe`, {method: 'POST'});
    const done = await waitJob(job.id, stage => { $('transcribe').textContent = stage; });
    if (builder.clip === clip) $('voice-transcript').value = done.text;
  } catch (error) { setStatus(error.message, 'error'); }
  finally { $('transcribe').textContent = 'Auto-transcribe'; $('transcribe').disabled = !builder.clip; }
});

$('voice-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!builder.clip) return;
  $('save-character').disabled = true;
  $('save-hint').textContent = 'Saving…';
  try {
    const data = await api(`reference/${builder.clip}/save`, {method: 'POST', body: new FormData(event.target)});
    await refresh(false);
    $('voice').value = data.voice.id;
    showPickedVoice();
    update();
    event.target.reset();
    builder.upload = builder.clip = builder.recordClip = null;
    design.takes = [];
    design.autoSummary = '';
    renderTakes();
    $('design-state').textContent = '';
    $('clip-picker').hidden = $('compare').hidden = true;
    $('drop-title').textContent = 'Choose or drop an MP3, WAV, M4A…';
    $('drop-hint').textContent = 'Saved. Drop another recording to add one more.';
    $('build-state').textContent = '';
    updateBuildable();
    updateDesignable();
    syncSaveState();
    setStatus(`${data.voice.name} saved to the gallery.`, 'success');
    setView('characters');
    document.querySelector(`.character[data-voice="${data.voice.id}"]`)?.scrollIntoView({block: 'center'});
  } catch (error) {
    $('save-character').disabled = false;
    $('save-hint').textContent = error.message;
  }
});

// ---- Add a character → Create a voice: describe it, preview takes, keep one as the reference ----
// The kept take is a reference clip like a cleaned recording (same transcribe/save routes),
// and its sample line is the exact transcript.
const design = {creators: [], takes: [], busy: false, autoSummary: ''};
let addSource = 'record';

function syncSaveState() {
  $('save-character').disabled = !builder.clip;
  $('transcribe').disabled = !builder.clip;
  $('save-hint').textContent = builder.clip ? '' : addSource === 'create' ? 'Preview a voice first.' : 'Build the cleaned clip first.';
}

function setAddSource(source) {
  if (source === addSource) return;
  if (addSource === 'record') builder.recordClip = builder.clip;
  addSource = source;
  const create = source === 'create';
  for (const button of document.querySelectorAll('[data-add-source]')) {
    button.setAttribute('aria-pressed', String(button.dataset.addSource === source));
  }
  if (create) stopSelection();
  $('design-step').hidden = !create;
  $('record-step').hidden = $('clean-step').hidden = create;
  $('character-step-num').textContent = create ? '2' : '3';
  $('add-intro').textContent = create
    ? 'Describe a voice, preview takes until it sounds right, and save the one you like as a character.'
    : 'Upload a recording or import YouTube audio, trim a clean clip, and save it to the gallery.';
  const take = create && design.takes.find(item => item.id === design.chosen);
  builder.clip = create ? (take ? take.id : null) : builder.recordClip || null;
  if (take) $('voice-transcript').value = take.text;
  syncSaveState();
}

function renderCreators(creators) {
  const key = JSON.stringify(creators);
  if (key === renderCreators.key) return;
  renderCreators.key = key;
  design.creators = creators;
  const selected = $('design-engine').value;
  $('design-engine').replaceChildren(...creators.map(item => new Option(item.name, item.id)));
  if (creators.some(item => item.id === selected)) $('design-engine').value = selected;
  updateDesignable();
}

function updateDesignable() {
  const creator = design.creators.find(item => item.id === $('design-engine').value);
  $('design-engine-hint').textContent = creator ? creator.detail : 'No installed engine can create voices.';
  $('design-preview').disabled = design.busy || !creator?.ready;
  $('design-preview').textContent = design.takes.length ? 'Preview another take' : 'Preview voice';
}

function chooseTake(take) {
  design.chosen = builder.clip = take.id;
  for (const row of $('design-takes').children) {
    const chosen = row.dataset.take === take.id;
    row.classList.toggle('is-chosen', chosen);
    row.querySelector('input').checked = chosen;
  }
  $('voice-transcript').value = take.text;
  const summary = $('voice-summary');
  if (!summary.value.trim() || summary.value === design.autoSummary) {
    summary.value = design.autoSummary = take.description.slice(0, 300);
  }
  syncSaveState();
}

function renderTakes() {
  $('design-takes').hidden = !design.takes.length;
  $('design-takes').replaceChildren(...design.takes.map(take => {
    const row = el('label', 'design-take');
    row.dataset.take = take.id;
    row.title = take.description;
    const radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = 'design-take';
    radio.setAttribute('aria-label', `Use take ${take.number}`);
    radio.addEventListener('change', () => chooseTake(take));
    const audio = document.createElement('audio');
    audio.controls = true;
    audio.preload = 'metadata';
    audio.src = take.audio;
    row.append(radio, audio, el('small', '', `Take ${take.number} · ${fmt(take.duration)}`));
    return row;
  }));
}

for (const button of document.querySelectorAll('[data-add-source]')) {
  button.addEventListener('click', () => setAddSource(button.dataset.addSource));
}
$('design-engine').addEventListener('change', updateDesignable);
$('design-preview').addEventListener('click', async () => {
  const description = $('design-description').value.trim();
  const text = $('design-text').value.trim();
  if (!description || !text) { $('design-state').textContent = 'Describe the voice and write a sample line first.'; return; }
  design.busy = true;
  updateDesignable();
  $('design-state').textContent = 'Starting…';
  try {
    const job = await api('reference/design', {method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({engine: $('design-engine').value, description, text, language: $('design-language').value})});
    const done = await waitJob(job.id, stage => { $('design-state').textContent = stage; });
    const take = {id: job.id, number: (design.takes[0]?.number || 0) + 1, audio: done.audio,
      duration: done.duration, text: done.text, description: done.description};
    design.takes.unshift(take);
    renderTakes();
    if (addSource === 'create') chooseTake(take);
    $('design-takes').querySelector('audio')?.play().catch(() => {});
    $('design-state').textContent = `Take ${take.number} ready. Keep it, tweak the description and try again, or save it below.`;
  } catch (error) {
    $('design-state').textContent = error.message;
  } finally {
    design.busy = false;
    updateDesignable();
  }
});

const initial = location.hash.slice(1);
setView(initial, {keepHash: !VIEWS.includes(initial)});  // #hermes belongs to agentic-ui.js
if (initial === 'recent') document.querySelector('.mobile-tab[data-mobile-tab="jobs"]')?.click();
refresh();
