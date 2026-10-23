(() => {
  // RAG & Cache: pinned files (+ groups) are the Master Agent's RAG library; everything else is the 7-day upload cache.
  let files = [], groups = [], texts = new Map(), selected = new Set(), selectMode = false, lastPicked = null, shownIds = [];
  let pinIds = [], activeGroup = null, activeDoc = null, view = 'list', tab = 'rag';
  const $ = (s) => document.querySelector(s);
  const store = { get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } }, set(k, v) { try { localStorage.setItem(k, v); } catch { /* private window */ } } };
  const fmt = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : n < 1073741824 ? `${(n / 1048576).toFixed(1)} MB` : `${(n / 1073741824).toFixed(1)} GB`;
  const time = (s) => new Intl.DateTimeFormat(undefined, {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}).format(new Date(s * 1000));
  const left = (at) => { if (!at) return 'Kept'; const sec = Math.max(0, Math.floor(at - Date.now() / 1000)); const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60); return sec <= 0 ? 'Deleting now' : d ? `${d}d ${h}h left` : h ? `${h}h ${m}m left` : `${m}m left`; };
  const esc = (value) => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  const isImage = (f) => /\.(png|jpg|jpeg|webp|gif|bmp|svg)$/i.test(f.name);
  const isVideo = (f) => /\.(mp4|mov|webm|mkv)$/i.test(f.name);
  const isAudio = (f) => /\.(mp3|wav|flac|ogg|m4a|opus)$/i.test(f.name);
  const isText = (f) => /\.(txt|md|markdown|json|csv|log|py|js|ts|css|html|yaml|yml|xml|srt|vtt)$/i.test(f.name);
  const content = (f) => `/Cache/api/files/${f.id}/content`;
  const thumbOf = (f) => /\.svg$/i.test(f.name) ? content(f) : `/Cache/api/files/${f.id}/thumb?v=${f.version}`;
  const svg = (d, size = 16) => `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
  const ICON = {
    image: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><circle cx="9" cy="10" r="1.8"/><path d="M20.5 16l-5-5L6 20"/>',
    video: '<rect x="3" y="6" width="13" height="12" rx="2.5"/><path d="M16 10.5l5-3v9l-5-3"/>',
    audio: '<path d="M9 18V6l11-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>',
    text: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>',
    file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    spark: '<path d="M12 3l1.9 4.6 4.6 1.9-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9z"/><path d="M19 15v6M16 18h6"/>',
    edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
    note: '<path d="M5 4h14v12l-4 4H5z"/><path d="M9 9h6M9 13h4"/>',
    download: '<path d="M12 4v11M7 10.5l5 5 5-5"/><path d="M5 20h14"/>',
  };
  const kindIcon = (f) => f && isImage(f) ? ICON.image : f && isVideo(f) ? ICON.video : f && isAudio(f) ? ICON.audio : f && isText(f) ? ICON.text : ICON.file;
  const thumbHtml = (f) => `<span class="thumb">${f && isImage(f) ? `<img src="${thumbOf(f)}" alt="" loading="lazy" decoding="async">` : svg(kindIcon(f))}</span>`;
  const ragTerms = (selector) => $(selector).value.split(/[\n,]+/).map(term => term.trim().replace(/\s+/g, ' ')).filter(Boolean);
  const ragProblem = (terms) => terms.length > 30 ? 'Use up to 30 trigger terms.' : terms.some(term => term.length > 80) ? 'Each trigger term must be 80 characters or less.' : '';
  const termChips = (terms, max = 3) => !terms?.length ? '<span class="no-terms">@ only</span>' : terms.slice(0, max).map(t => `<span class="term" title="${esc(t)}">${esc(t)}</span>`).join('') + (terms.length > max ? `<span class="term more" title="${esc(terms.slice(max).join(', '))}">+${terms.length - max}</span>` : '');
  const soon = (f) => !f.pinned && f.deletes_at - Date.now() / 1000 < 86400;
  const kindOn = (f) => (f.cache_kind === 'uploaded' && $('#show-uploaded').getAttribute('aria-pressed') === 'true') || (f.cache_kind === 'generated' && $('#show-generated').getAttribute('aria-pressed') === 'true');
  const visibleFiles = () => files.filter(kindOn);
  const inTab = (f) => tab === 'all' || (tab === 'rag' ? f.pinned : !f.pinned);
  const matchesQuery = (f, q) => !q || [f.name, f.alias, f.note, ...(f.rag_terms || [])].some(v => (v || '').toLowerCase().includes(q));
  const lifeHtml = (f) => `<span class="life ${f.pinned ? 'kept' : soon(f) ? 'soon' : ''}" data-timer="${f.id}">${left(f.deletes_at)}</span>`;
  const downloadUrl = (f) => `${content(f)}?download=1`;
  const actionHtml = (f) => `<span class="row-actions"><a class="row-action" href="${downloadUrl(f)}" download="${esc(f.name)}" data-download title="Download" aria-label="Download ${esc(f.name)}">${svg(ICON.download)}</a>${f.editable
    ? `<button type="button" class="row-action ${f.pinned ? '' : 'add'}" data-doc="${f.id}" title="Edit text${f.pinned ? ', trigger terms and note' : ' · add to RAG'}" aria-label="Edit ${esc(f.alias || f.name)}">${svg(ICON.edit)}</button>`
    : f.pinned
    ? `<button type="button" class="row-action" data-pin="${f.id}" title="Edit alias, trigger terms and note" aria-label="Edit ${esc(f.alias || f.name)}">${svg(ICON.edit)}</button>`
    : `<button type="button" class="row-action add" data-pin="${f.id}" title="Add to RAG library" aria-label="Add ${esc(f.name)} to RAG">${svg(ICON.spark)}</button>`}</span>`;
  const namesHtml = (f) => {
    const title = f.pinned && f.alias ? f.alias : f.name;
    const sub = f.pinned && f.alias ? f.name : f.cache_kind === 'generated' ? 'Generated' : 'Uploaded';
    return `<span class="file-names"><span class="file-title" title="${esc(title)}">${esc(title)}</span><span class="file-sub">${f.note ? `<span class="note-flag" title="Has a context note">${svg(ICON.note, 13)}</span>` : ''}<span title="${esc(f.path || f.name)}">${esc(sub)}</span></span></span>`;
  };
  const checkHtml = (f) => `<input class="cache-check" type="checkbox" data-check="${f.id}" ${selected.has(f.id) ? 'checked' : ''} aria-label="Select ${esc(f.name)}">`;
  const preview = (f) => isImage(f) ? `<img src="${thumbOf(f)}" alt="${esc(f.name)}" loading="lazy" decoding="async">`
    : isVideo(f) ? `<video src="${content(f)}#t=1" controls preload="metadata"></video>`
    : isText(f) ? `<pre data-text="${f.id}">Loading preview…</pre>` : svg(kindIcon(f), 34);

  const TABS = {rag: 1, cache: 1, all: 1};

  function render() {
    const base = visibleFiles();
    const q = $('#search').value.trim().toLowerCase();
    const list = base.filter(f => inTab(f) && matchesQuery(f, q));
    $('#files').innerHTML = list.map(f => `<div class="file-row ${selected.has(f.id) ? 'is-selected' : ''}" data-card="${f.id}">${checkHtml(f)}<span class="file-main">${thumbHtml(f)}${namesHtml(f)}</span><span class="terms">${f.pinned ? termChips(f.rag_terms) : '<span class="badge cache">Cache</span>'}</span><span class="meta" title="${time(f.uploaded_at)}">${time(f.uploaded_at)}</span><span class="meta">${fmt(f.size)}</span>${lifeHtml(f)}${actionHtml(f)}</div>`).join('');
    $('#gallery-view').innerHTML = list.map(f => `<article class="card ${f.pinned ? 'rag' : ''} ${selected.has(f.id) ? 'is-selected' : ''}" data-card="${f.id}"><div class="card-preview">${preview(f)}<label class="card-check">${checkHtml(f)}</label>${f.pinned ? '<span class="badge rag">RAG</span>' : ''}</div><div class="card-body">${namesHtml(f)}${f.pinned ? `<div class="terms">${termChips(f.rag_terms, 2)}</div>` : ''}<div class="card-foot">${lifeHtml(f)}<span class="meta">${fmt(f.size)}</span>${actionHtml(f)}</div></div></article>`).join('');
    // Text previews are fetched once per file version, and only when the gallery is showing.
    if (view === 'gallery') document.querySelectorAll('[data-text]').forEach(async el => { const f = files.find(x => x.id === el.dataset.text); const key = `${f.id}:${f.version}`; try { if (!texts.has(key)) texts.set(key, fetch(content(f)).then(r => r.ok ? r.text() : Promise.reject()).then(text => text.slice(0, 5000) + (text.length > 5000 ? '\n…' : ''))); el.textContent = await texts.get(key); } catch { texts.delete(key); el.textContent = 'Preview unavailable'; } });
    $('#list-view').hidden = view !== 'list' || !list.length; $('#gallery-view').hidden = view !== 'gallery' || !list.length;
    $('#groups-block').hidden = tab === 'cache' || !groups.length;
    $('#new-group').hidden = tab === 'cache';
    renderEmpty(list, base, q);
    renderStats(base);
    shownIds = list.map(f => f.id); syncSelection();
  }
  function renderEmpty(list, base, q) {
    const empty = $('#empty'); empty.hidden = list.length > 0;
    empty.querySelector('.button')?.remove();
    if (list.length) return;
    const kindsOff = !base.length && files.some(f => f.cache_kind !== 'working');
    const [title, text, go] = q ? ['No matches', `Nothing in this view matches “${$('#search').value.trim()}”.`, null]
      : kindsOff ? ['Both origins are hidden', 'Turn on Uploaded or Generated to see files.', null]
      : tab === 'rag' ? ['Your RAG library is empty', 'Write a reusable text document with New doc, or open the upload cache and use Add to RAG on a file. They stay until you remove them.', 'cache']
      : tab === 'cache' ? ['The cache is empty', 'Drop files above, or attach them in Master Agent chat. Generated outputs show up here too.', null]
      : ['No files yet', 'Drop files above to upload them to the cache.', null];
    $('#empty-title').textContent = title; $('#empty-text').textContent = text;
    if (go) { const b = document.createElement('button'); b.type = 'button'; b.className = 'button'; b.textContent = 'Go to the upload cache'; b.onclick = () => setTab(go); empty.append(b); }
  }
  function renderStats(base) {
    const rag = base.filter(f => f.pinned), cached = base.filter(f => !f.pinned);
    const withTerms = rag.filter(f => f.rag_terms?.length).length;
    const terms = rag.reduce((n, f) => n + (f.rag_terms?.length || 0), 0) + groups.reduce((n, g) => n + (g.rag_terms?.length || 0), 0);
    const expiring = cached.filter(soon).length;
    $('#count-rag').textContent = rag.length; $('#count-cache').textContent = cached.length; $('#count-all').textContent = base.length;
    // Footer line: only the numbers that say something (no zeros).
    const working = files.filter(f => f.cache_kind === 'working').length;
    const bits = [
      rag.length && `${withTerms} of ${plural(rag.length, 'RAG file')} auto-attach`,
      terms && plural(terms, 'trigger term'),
      cached.length && `${fmt(cached.reduce((n, f) => n + f.size, 0))} in cache`,
      expiring && `<span class="hot">${expiring} expiring within 24 h</span>`,
      working && `${plural(working, 'working file')} hidden`,
    ].filter(Boolean);
    $('#foot-stats').classList.remove('bad'); $('#foot-stats').innerHTML = bits.join(' · ');
  }
  function setTab(next) {
    tab = TABS[next] ? next : 'rag'; store.set('cache-tab', tab);
    document.querySelectorAll('[data-tab]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.tab === tab)));
    endSelection(); render();
  }

  // Selection mode: on via the Select button or the first ticked box; while on, a click anywhere on a row/card toggles it.
  const selecting = () => selectMode || selected.size > 0;
  function syncSelection() {
    document.querySelectorAll('[data-card]').forEach(el => el.classList.toggle('is-selected', selected.has(el.dataset.card)));
    document.querySelectorAll('[data-check]').forEach(el => { el.checked = selected.has(el.dataset.check); });
    const on = selecting(); document.body.classList.toggle('cache-selecting', on);
    $('#select-mode').querySelector('span').textContent = on ? 'Done' : 'Select'; $('#select-mode').setAttribute('aria-pressed', String(on));
    $('#selection-count').textContent = selected.size; $('#selection-bar').hidden = !on;
    $('#selection-hint').hidden = selected.size > 0; $('#pin-selected').disabled = $('#group-selected').disabled = $('#download-selected').disabled = $('#delete-selected').disabled = !selected.size;
    if (!selected.size) disarm('#delete-selected');
    const allRag = selected.size && [...selected].every(id => files.find(f => f.id === id)?.pinned);
    $('#pin-selected').textContent = allRag ? 'Edit in RAG' : 'Add to RAG';
  }
  function pick(id, on, range) {
    if (range && lastPicked && shownIds.includes(lastPicked)) {
      const [a, b] = [shownIds.indexOf(lastPicked), shownIds.indexOf(id)].sort((x, y) => x - y);
      for (const other of shownIds.slice(a, b + 1)) on ? selected.add(other) : selected.delete(other);
    } else on ? selected.add(id) : selected.delete(id);
    lastPicked = id; syncSelection();
  }
  function endSelection() { selected.clear(); selectMode = false; lastPicked = null; syncSelection(); }
  function cardClick(e) {
    if (e.target.closest('[data-download]')) return; // plain link: the browser downloads it
    const pinButton = e.target.closest('[data-pin]'); if (pinButton) { openPin([pinButton.dataset.pin]); return; }
    const docButton = e.target.closest('[data-doc]'); if (docButton) { openDoc(files.find(f => f.id === docButton.dataset.doc)); return; }
    const check = e.target.closest('[data-check]');
    if (check) { pick(check.dataset.check, check.checked, e.shiftKey); return; }
    if (e.target.closest('label')) return; // the label forwards its click to the checkbox
    const card = e.target.closest('[data-card]');
    if (!card || !selecting() || e.target.closest('video')) return;
    e.preventDefault(); pick(card.dataset.card, !selected.has(card.dataset.card), e.shiftKey);
  }
  async function load() {
    try {
      const [r, g] = await Promise.all([fetch('/Cache/api/files', {cache:'no-store'}), fetch('/Cache/api/groups', {cache:'no-store'})]);
      if (!r.ok || !g.ok) throw new Error();
      files = await r.json(); groups = await g.json();
    } catch { $('#foot-stats').classList.add('bad'); $('#foot-stats').textContent = 'Could not load files'; toast('Could not load files'); return; }
    for (const id of [...selected]) if (!files.some(f => f.id === id)) selected.delete(id);
    renderGroups(); render();
  }

  // ── Groups ────────────────────────────────────────────────────────────────────────────────
  const tagFor = (name) => `@{${name}} all`;
  async function copyTag(name) { try { await navigator.clipboard.writeText(tagFor(name)); toast(`Copied ${tagFor(name)}`); } catch { toast('Copy failed — select the tag instead'); } }
  function renderGroups() {
    const host = $('#groups');
    if (!groups.length) {
      host.innerHTML = `<div class="group-empty-card"><span class="thumb">${svg('<rect x="4" y="7" width="13" height="13" rx="2"/><path d="M8 4h10a2 2 0 0 1 2 2v10"/>')}</span><span><strong>No groups yet</strong>Select a few files, then choose Group: they’re added to RAG and can be attached together in chat.</span></div>`;
      return;
    }
    const byId = new Map(files.map(f => [f.id, f]));
    host.replaceChildren(...groups.map(group => {
      const card = document.createElement('article'); card.className = 'group-card';
      const stack = group.files.slice(0, 4).map(id => thumbHtml(byId.get(id))).join('') || thumbHtml(null);
      const meta = [group.files.length ? plural(group.files.length, 'file') : 'Empty: its files left RAG'];
      if (group.note) meta.push('context note');
      card.innerHTML = `<div class="group-top"><span class="stack">${stack}</span><span class="group-info"><strong title="${esc(group.name)}">${esc(group.name)}</strong><small>${esc(meta.join(' · '))}</small></span></div><div class="terms">${termChips(group.rag_terms, 4)}</div><div class="group-foot"><button type="button" class="token" title="Copy chat tag">${esc(tagFor(group.name))}</button><button type="button" class="button small">Edit</button></div>`;
      card.querySelector('.token').onclick = () => copyTag(group.name);
      card.querySelector('.button').onclick = () => openGroup(group);
      return card;
    }));
  }
  // Members = RAG files, plus (when creating) any selected cache files, which are added to RAG on save.
  let groupPool = [], groupChosen = new Set(), deleteArmed = null;
  function groupProblem() {
    const name = $('#group-name').value.trim().replace(/\s+/g, ' ');
    if (!name) return 'Give the group a name.';
    if (/[{}@]/.test(name)) return 'Names can’t contain @ or braces.';
    if (groups.some(g => g.id !== activeGroup?.id && g.name.toLowerCase() === name.toLowerCase())) return `A group called “${name}” already exists.`;
    if (!groupChosen.size) return 'Choose at least one file.';
    if (groupChosen.size > 200) return 'Groups can hold up to 200 files.';
    return ragProblem(ragTerms('#group-rag-terms'));
  }
  function refreshGroupForm(showError = false) {
    const name = $('#group-name').value.trim().replace(/\s+/g, ' ');
    $('#group-usage').textContent = tagFor(name || 'Group name');
    const toPin = groupPool.filter(f => !f.pinned && groupChosen.has(f.id)).length;
    $('#group-count').textContent = `${groupChosen.size} of ${groupPool.length} chosen` + (toPin ? ` · ${toPin} will join RAG` : '') + (groupChosen.size > 20 ? ' · chat attaches 20 at a time' : '');
    termPreview('#group-rag-terms', '#group-term-preview');
    const problem = groupProblem();
    $('#save-group').disabled = !!problem;
    $('#group-error').textContent = showError || (problem && name && /already|braces|trigger/.test(problem)) ? problem : '';
  }
  function renderMembers() {
    const host = $('#group-members'); host.replaceChildren();
    if (!groupPool.length) {
      host.innerHTML = '<div class="group-empty"><strong>No RAG files yet</strong><span>Close this, select files in the upload cache, then choose Group: they’re added to RAG for you.</span></div>';
      return;
    }
    const q = $('#group-filter').value.trim().toLowerCase();
    const shown = groupPool.filter(f => matchesQuery(f, q));
    for (const file of shown) {
      const label = document.createElement('label'); label.className = 'group-member';
      label.innerHTML = `${thumbHtml(file)}<span class="file-names"><span class="file-title">${esc(file.alias || file.name)}</span><span class="file-sub ${file.pinned ? '' : 'group-will-pin'}"><span>${esc(file.pinned ? (file.alias ? file.name : fmt(file.size)) : 'In cache: joins RAG on save')}</span></span></span>`;
      const check = document.createElement('input'); check.type = 'checkbox'; check.value = file.id; check.checked = groupChosen.has(file.id);
      check.onchange = () => { check.checked ? groupChosen.add(file.id) : groupChosen.delete(file.id); refreshGroupForm(); };
      label.append(check); host.append(label);
    }
    if (!shown.length) host.innerHTML = '<div class="group-empty">No files match that filter.</div>';
  }
  function openGroup(group = null) {
    activeGroup = group;
    const fromSelection = group ? [] : files.filter(f => selected.has(f.id) && !f.pinned);
    const visible = new Set(visibleFiles().map(f => f.id));
    groupPool = [...files.filter(f => f.pinned && (visible.has(f.id) || group?.files.includes(f.id))), ...fromSelection];
    groupChosen = new Set((group?.files || [...selected]).filter(id => groupPool.some(f => f.id === id)));
    $('#group-title').textContent = group ? `Edit “${group.name}”` : 'New group';
    $('#save-group').textContent = group ? 'Save changes' : 'Create group';
    $('#group-name').value = group?.name || '';
    $('#group-rag-terms').value = (group?.rag_terms || []).join('\n');
    $('#group-note').value = group?.note || '';
    $('#group-filter').value = '';
    $('#delete-group').hidden = !group; disarmDelete();
    $('#group-dialog').querySelector('.group-tools').hidden = groupPool.length < 2;
    renderMembers(); refreshGroupForm();
    $('#group-dialog').showModal(); $('#group-name').focus();
  }
  function disarmDelete() { clearTimeout(deleteArmed); deleteArmed = null; $('#delete-group').textContent = 'Delete group'; $('#delete-group').classList.remove('armed'); }
  $('#new-group').onclick = () => openGroup();
  $('#group-selected').onclick = () => openGroup();
  $('#close-group').onclick = $('#cancel-group').onclick = () => $('#group-dialog').close();
  $('#group-dialog').addEventListener('click', e => { if (e.target === $('#group-dialog')) $('#group-dialog').close(); });
  $('#group-name').oninput = () => refreshGroupForm();
  $('#group-rag-terms').oninput = () => refreshGroupForm();
  $('#group-filter').oninput = renderMembers;
  $('#group-filter').onkeydown = e => { if (e.key === 'Enter') e.preventDefault(); };
  $('#group-usage').onclick = () => { const name = $('#group-name').value.trim(); if (name) copyTag(name); };
  $('#group-all').onclick = () => { $('#group-members').querySelectorAll('input').forEach(input => groupChosen.add(input.value)); renderMembers(); refreshGroupForm(); };
  $('#group-none').onclick = () => { $('#group-members').querySelectorAll('input').forEach(input => groupChosen.delete(input.value)); renderMembers(); refreshGroupForm(); };
  $('#group-form').onsubmit = async (event) => {
    event.preventDefault();
    const problem = groupProblem(); if (problem) { refreshGroupForm(true); return; }
    const save = $('#save-group'); save.disabled = true; const label = save.textContent; save.textContent = 'Saving…';
    try {
      for (const f of groupPool.filter(f => !f.pinned && groupChosen.has(f.id))) {
        const r = await fetch(`/Cache/api/files/${f.id}/pin`, {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({alias: f.name.replace(/\.[^.]+$/, '').slice(0, 80) || f.name.slice(0, 80)})});
        if (!r.ok) throw new Error(`Couldn’t add ${f.name} to RAG`);
        f.pinned = true;
      }
      const name = $('#group-name').value.trim();
      const url = activeGroup ? `/Cache/api/groups/${activeGroup.id}` : '/Cache/api/groups';
      const r = await fetch(url, {method:activeGroup ? 'PUT' : 'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({name, files:[...groupChosen], rag_terms:ragTerms('#group-rag-terms'), note:$('#group-note').value})});
      if (!r.ok) { const body = await r.json().catch(() => ({})); throw new Error(body.detail || 'Could not save group'); }
      $('#group-dialog').close(); selectMode = false; selected.clear(); await load();
      toast(`${activeGroup ? 'Saved' : 'Created'} “${name}” · ${plural(groupChosen.size, 'file')}`);
    } catch (error) {
      $('#group-error').textContent = error.message; await load();
    } finally { save.textContent = label; refreshGroupForm(); }
  };
  $('#delete-group').onclick = async () => {
    if (!activeGroup) return;
    if (!deleteArmed) { $('#delete-group').textContent = 'Click again to delete'; $('#delete-group').classList.add('armed'); deleteArmed = setTimeout(disarmDelete, 3000); return; }
    disarmDelete();
    const r = await fetch(`/Cache/api/groups/${activeGroup.id}`, {method:'DELETE'});
    if (r.ok) { $('#group-dialog').close(); await load(); toast(`Deleted “${activeGroup.name}” · its files stay in RAG`); }
    else $('#group-error').textContent = 'Could not delete group';
  };

  // ── Add to RAG / edit (one file, or a whole selection) ───────────────────────────────────────
  function termPreview(input, out) { const terms = ragTerms(input); $(out).innerHTML = terms.slice(0, 30).map(t => `<span class="term">${esc(t)}</span>`).join(''); }
  function openPin(ids) {
    pinIds = ids.filter(id => files.some(f => f.id === id)); if (!pinIds.length) return;
    const chosen = pinIds.map(id => files.find(f => f.id === id)), one = chosen.length === 1 ? chosen[0] : null;
    const pinned = chosen.filter(f => f.pinned).length;
    $('#pin-preview').innerHTML = one
      ? `${thumbHtml(one)}<span class="file-names"><span class="file-title">${esc(one.name)}</span><span class="file-sub"><span>${fmt(one.size)} · ${one.pinned ? 'in RAG' : `in cache · ${left(one.deletes_at)}`}</span></span></span>`
      : `<span class="stack">${chosen.slice(0, 4).map(thumbHtml).join('')}</span><span class="file-names"><span class="file-title">${plural(chosen.length, 'file')}</span><span class="file-sub"><span>${pinned ? `${pinned} already in RAG keep their alias` : 'All from the cache'}</span></span></span>`;
    $('#alias').value = one ? one.alias || one.name.replace(/\.[^.]+$/, '') : 'Pinned uploads';
    $('#alias-help').textContent = one ? 'How it shows up in the @ picker.' : 'New RAG files get this alias, numbered (“Alias 1”, “Alias 2”…).';
    $('#pin-rag-terms').value = one ? (one.rag_terms || []).join('\n') : '';
    $('#pin-note').value = one ? one.note || '' : '';
    $('#pin-rag-terms').placeholder = one ? 'One phrase per line, e.g.\nproduct launch\nblue sneaker' : 'One phrase per line. Leave empty to keep each file’s own terms.';
    $('#pin-note').placeholder = one ? 'What the file shows, identities, details to preserve, or how to use it.' : 'Leave empty to keep each file’s own note.';
    $('#pin-error').textContent = '';
    $('#pin-title').textContent = one?.pinned ? 'Edit RAG file' : one ? 'Add to RAG library' : pinned === chosen.length ? `Edit ${chosen.length} RAG files` : `Add ${chosen.length} files to RAG`;
    $('#save-pin').textContent = one?.pinned || pinned === chosen.length ? 'Save changes' : 'Add to RAG';
    $('#unpin-file').hidden = !one?.pinned; $('#delete-file').hidden = !one; disarm('#delete-file');
    termPreview('#pin-rag-terms', '#pin-term-preview');
    $('#pin-dialog').showModal(); setTimeout(() => $('#alias').focus(), 30);
  }
  $('#pin-rag-terms').oninput = () => termPreview('#pin-rag-terms', '#pin-term-preview');
  $('#save-pin').addEventListener('click', async e => {
    e.preventDefault();
    const alias = $('#alias').value.trim(), rag_terms = ragTerms('#pin-rag-terms'), note = $('#pin-note').value, single = pinIds.length === 1;
    const problem = !alias ? 'Give it an alias.' : ragProblem(rag_terms); if (problem) { $('#pin-error').textContent = problem; return; }
    const save = $('#save-pin'); save.disabled = true;
    let added = 0, updated = 0, n = 0; const failed = [];
    for (const id of pinIds) {
      const f = files.find(x => x.id === id); if (!f) continue;
      // Bulk: files already in RAG keep their alias, and empty terms/note fields keep what each file has.
      const body = single ? {alias, rag_terms, note} : {alias: f.pinned ? f.alias || alias : `${alias} ${++n}`.slice(0, 80), ...(rag_terms.length ? {rag_terms} : {}), ...(note.trim() ? {note} : {})};
      const r = await fetch(`/Cache/api/files/${id}/pin`, {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(body)}).catch(() => null);
      if (!r?.ok) { const b = r ? await r.json().catch(() => ({})) : {}; failed.push(b.detail || f.name); continue; }
      f.pinned ? updated++ : added++;
    }
    save.disabled = false;
    if (failed.length) { $('#pin-error').textContent = `Could not save ${failed.length}: ${failed.join(' · ')}`; await load(); return; }
    $('#pin-dialog').close(); selectMode = false; selected.clear(); await load();
    toast(added && updated ? `${plural(added, 'file')} added to RAG · ${updated} updated` : added ? (single ? 'Added to the RAG library' : `${plural(added, 'file')} added to RAG`) : 'RAG file updated');
  });
  $('#unpin-file').addEventListener('click', async () => {
    const [id] = pinIds; const r = await fetch(`/Cache/api/files/${id}/pin`, {method:'DELETE'});
    if (r.ok) { $('#pin-dialog').close(); selected.delete(id); await load(); const f = files.find(x => x.id === id); toast(f ? `Removed from RAG · back in the cache, ${left(f.deletes_at)}` : 'Removed from RAG'); }
    else $('#pin-error').textContent = 'Could not remove it from RAG';
  });
  $('#close-dialog').onclick = () => $('#pin-dialog').close();
  $('#pin-dialog').addEventListener('click', e => { if (e.target === $('#pin-dialog')) $('#pin-dialog').close(); });

  // ── Delete (two clicks: the first arms the button for 3 s) ─────────────────────────────────
  const armed = new Map();
  function disarm(sel) { const b = $(sel); clearTimeout(armed.get(sel)); armed.delete(sel); if (b.dataset.label) b.textContent = b.dataset.label; b.classList.remove('armed'); }
  function armOrGo(sel, prompt) {
    if (armed.has(sel)) { disarm(sel); return true; }
    const b = $(sel); b.dataset.label = b.textContent; b.textContent = prompt; b.classList.add('armed');
    armed.set(sel, setTimeout(() => disarm(sel), 3000)); return false;
  }
  async function deleteFiles(ids) {
    const failed = []; let done = 0;
    for (const id of ids) {
      const r = await fetch(`/Cache/api/files/${id}`, {method:'DELETE'}).catch(() => null);
      if (r?.ok) { done++; selected.delete(id); } else { const b = r ? await r.json().catch(() => ({})) : {}; failed.push(b.detail || files.find(f => f.id === id)?.name || id); }
    }
    await load();
    if (failed.length) toast(`${done ? `Deleted ${plural(done, 'file')} · ` : ''}couldn’t delete ${failed.length}: ${failed[0]}`);
    else toast(`Deleted ${plural(done, 'file')}`);
    return !failed.length;
  }
  $('#delete-file').addEventListener('click', async () => {
    const f = files.find(x => x.id === pinIds[0]); if (!f) return;
    if (!armOrGo('#delete-file', 'Click again to delete')) return;
    if (await deleteFiles([f.id])) $('#pin-dialog').close(); else $('#pin-error').textContent = 'Could not delete it — see the message below.';
  });
  $('#delete-selected').onclick = async () => {
    if (!selected.size || !armOrGo('#delete-selected', `Delete ${selected.size}?`)) return;
    await deleteFiles([...selected]); selectMode = false; syncSelection();
  };

  // ── Text documents: write new ones, or edit any text file (and its RAG details) ───────────────
  const docTerms = () => ragTerms('#doc-rag-terms');
  function docCount() { const t = $('#doc-text').value; const words = (t.match(/\S+/g) || []).length; $('#doc-count').textContent = `${words.toLocaleString()} word${words === 1 ? '' : 's'} · ${fmt(new Blob([t]).size)}`; }
  function docRagSync() { $('.doc-side').classList.toggle('off', !$('#doc-rag').checked); }
  const docFileName = () => { const n = $('#doc-name').value.trim().replace(/\s+/g, ' '); return n ? (/\.(md|txt|markdown|json|csv|ya?ml|srt|vtt|xml|html|css|js|py|log)$/i.test(n) ? n : `${n}.md`) : 'name.md'; };
  async function openDoc(f = null) {
    activeDoc = f; $('#doc-error').textContent = ''; disarm('#delete-doc');
    $('#doc-title').textContent = f ? `Edit ${f.pinned && f.alias ? f.alias : f.name}` : 'New text document';
    $('#doc-name').value = f ? (f.pinned && f.alias ? f.alias : f.name.replace(/\.[^.]+$/, '')) : '';
    $('#doc-file').textContent = f ? f.name : docFileName();
    $('#doc-rag').checked = f ? f.pinned : true;
    $('#doc-rag-terms').value = (f?.rag_terms || []).join('\n'); $('#doc-note').value = f?.note || '';
    $('#delete-doc').hidden = !f; $('#save-doc').textContent = f ? 'Save changes' : 'Create document';
    $('#doc-text').value = ''; $('#doc-text').disabled = !!f;
    termPreview('#doc-rag-terms', '#doc-term-preview'); docRagSync(); docCount();
    $('#doc-dialog').showModal();
    if (f) {
      $('#doc-text').placeholder = 'Loading…';
      try { const r = await fetch(content(f), {cache:'no-store'}); if (!r.ok) throw new Error(); $('#doc-text').value = await r.text(); }
      catch { $('#doc-error').textContent = 'Could not load the text.'; return; }
      finally { $('#doc-text').placeholder = 'Write anything the Master Agent should reuse. Markdown works.'; }
      $('#doc-text').disabled = false; docCount();
    }
    $('#doc-dialog').dataset.clean = $('#doc-text').value;
    setTimeout(() => (f ? $('#doc-text') : $('#doc-name')).focus(), 30);
  }
  $('#new-doc').onclick = () => openDoc();
  $('#doc-name').oninput = () => { if (!activeDoc) $('#doc-file').textContent = docFileName(); };
  $('#doc-text').oninput = docCount;
  $('#doc-rag').onchange = docRagSync;
  $('#doc-rag-terms').oninput = () => termPreview('#doc-rag-terms', '#doc-term-preview');
  // Tab indents inside the text instead of leaving it.
  $('#doc-text').addEventListener('keydown', e => {
    if (e.key === 'Tab' && !e.shiftKey && !e.ctrlKey && !e.metaKey) { e.preventDefault(); document.execCommand('insertText', false, '  '); }
  });
  $('#doc-dialog').addEventListener('keydown', e => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); $('#doc-form').requestSubmit(); } });
  const closeDoc = () => {
    if ($('#doc-text').value !== ($('#doc-dialog').dataset.clean || '') && !confirm('Discard the changes to this document?')) return;
    $('#doc-dialog').close();
  };
  $('#close-doc').onclick = $('#cancel-doc').onclick = closeDoc;
  $('#doc-dialog').addEventListener('cancel', e => { e.preventDefault(); closeDoc(); });
  $('#doc-dialog').addEventListener('click', e => { if (e.target === $('#doc-dialog')) closeDoc(); });
  $('#doc-form').onsubmit = async (event) => {
    event.preventDefault();
    const name = $('#doc-name').value.trim(), text = $('#doc-text').value, keep = $('#doc-rag').checked, rag_terms = docTerms();
    const problem = !name ? 'Give the document a name.' : !text.trim() ? 'Write some text first.' : keep ? ragProblem(rag_terms) : '';
    if (problem) { $('#doc-error').textContent = problem; return; }
    const save = $('#save-doc'), label = save.textContent; save.disabled = true; save.textContent = 'Saving…';
    try {
      const r = await fetch(activeDoc ? `/Cache/api/docs/${activeDoc.id}` : '/Cache/api/docs', {method: activeDoc ? 'PUT' : 'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({name, text})});
      const saved = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(saved.detail || 'Could not save the document');
      if (keep) {
        const p = await fetch(`/Cache/api/files/${saved.id}/pin`, {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({alias: name.slice(0, 80), rag_terms, note: $('#doc-note').value})});
        if (!p.ok) { const b = await p.json().catch(() => ({})); throw new Error(`Saved, but not added to RAG: ${b.detail || 'error'}`); }
      } else if (activeDoc?.pinned) await fetch(`/Cache/api/files/${saved.id}/pin`, {method:'DELETE'});
      $('#doc-dialog').dataset.clean = text; $('#doc-dialog').close();
      texts.clear(); await load();
      if (!activeDoc && keep && tab === 'cache') setTab('rag');
      toast(activeDoc ? 'Document saved' : keep ? `Created “${name}” in the RAG library` : `Created ${saved.name} in the cache`);
    } catch (error) { $('#doc-error').textContent = error.message; await load(); }
    finally { save.disabled = false; save.textContent = label; }
  };
  $('#delete-doc').onclick = async () => {
    if (!activeDoc || !armOrGo('#delete-doc', 'Click again to delete')) return;
    if (await deleteFiles([activeDoc.id])) { $('#doc-dialog').dataset.clean = $('#doc-text').value; $('#doc-dialog').close(); }
    else $('#doc-error').textContent = 'Could not delete it.';
  };

  // ── Uploads ──────────────────────────────────────────────────────────────────────────────
  async function upload(selection) {
    const picked = [...selection]; if (!picked.length) return;
    const status = $('#upload-status'); status.classList.remove('bad');
    if (tab === 'rag') setTab('cache');
    const failed = [];
    for (const [i, file] of picked.entries()) {
      status.textContent = `Uploading ${i + 1} of ${picked.length}: ${file.name}…`;
      const form = new FormData(); form.append('upload', file);
      const response = await fetch('/Cache/api/files/upload', {method:'POST', body:form}).catch(() => null);
      if (!response || !response.ok) { const body = response ? await response.json().catch(() => ({})) : {}; failed.push(`${file.name}: ${body.detail || body.message || 'upload failed'}`); }
    }
    status.textContent = failed.length ? `Could not upload ${failed.length} of ${picked.length} — ${failed.join(' · ')}` : `${plural(picked.length, 'file')} uploaded to the cache`;
    status.classList.toggle('bad', failed.length > 0);
    await load(); if (!failed.length) setTimeout(() => { status.textContent = ''; }, 2600);
  }
  let toastTimer = null;
  function toast(message) { const el = $('#toast'); el.textContent = message; el.classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove('show'), 2400); }
  $('#choose-files').onclick = $('#upload-top').onclick = () => $('#upload-input').click();
  $('#upload-input').onchange = e => { upload(e.target.files); e.target.value = ''; };
  // Drop anywhere on the page (dialogs excepted): a veil shows while files are dragged over.
  let dragDepth = 0; const veil = $('#drop-veil'), zone = $('#upload-zone');
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files') && !document.querySelector('dialog[open]');
  document.addEventListener('dragenter', e => { if (!hasFiles(e)) return; dragDepth++; veil.hidden = false; zone.classList.add('dragging'); });
  document.addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
  document.addEventListener('dragleave', e => { if (!hasFiles(e)) return; if (--dragDepth <= 0) { dragDepth = 0; veil.hidden = true; zone.classList.remove('dragging'); } });
  document.addEventListener('drop', e => { if (!hasFiles(e)) return; e.preventDefault(); dragDepth = 0; veil.hidden = true; zone.classList.remove('dragging'); upload(e.dataTransfer.files); });

  // ── Wiring ───────────────────────────────────────────────────────────────────────────────
  $('#files').addEventListener('click', cardClick); $('#gallery-view').addEventListener('click', cardClick);
  $('#select-mode').onclick = () => { if (selecting()) endSelection(); else { selectMode = true; syncSelection(); } };
  $('#select-all').onclick = () => { shownIds.forEach(id => selected.add(id)); syncSelection(); };
  $('#clear-selected').onclick = endSelection;
  $('#pin-selected').onclick = () => openPin([...selected]);
  $('#download-selected').onclick = () => {
    // One file downloads as itself; several come back as one zip so the browser doesn't block a burst of downloads.
    const chosen = files.filter(f => selected.has(f.id)); if (!chosen.length) return;
    const a = document.createElement('a');
    a.href = chosen.length === 1 ? downloadUrl(chosen[0]) : `/Cache/api/files/zip?ids=${chosen.map(f => f.id).join(',')}`;
    if (chosen.length === 1) a.download = chosen[0].name;
    document.body.append(a); a.click(); a.remove();
    if (chosen.length > 1) toast(`Zipping ${plural(chosen.length, 'file')}…`);
  };
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && selecting() && !document.querySelector('dialog[open]')) endSelection(); });
  $('#search').oninput = render;
  $('#refresh').onclick = async () => { await load(); toast('Refreshed'); };
  for (const id of ['#show-uploaded', '#show-generated']) $(id).onclick = () => { const b = $(id); b.setAttribute('aria-pressed', String(b.getAttribute('aria-pressed') !== 'true')); endSelection(); render(); };
  document.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => setTab(b.dataset.tab));
  const setView = (next) => { view = next === 'gallery' ? 'gallery' : 'list'; store.set('cache-view', view); document.querySelectorAll('[data-view]').forEach(item => item.setAttribute('aria-pressed', String(item.dataset.view === view))); render(); };
  document.querySelectorAll('[data-view]').forEach(button => button.onclick = () => setView(button.dataset.view));
  setInterval(() => { document.querySelectorAll('[data-timer]').forEach(el => { const f = files.find(x => x.id === el.dataset.timer); if (f) el.textContent = left(f.deletes_at); }); }, 30000);
  view = store.get('cache-view', 'list') === 'gallery' ? 'gallery' : 'list';
  document.querySelectorAll('[data-view]').forEach(item => item.setAttribute('aria-pressed', String(item.dataset.view === view)));
  // #rag / #cache / #all open that section (links from elsewhere); otherwise the last one used.
  const wanted = location.hash.slice(1) || store.get('cache-tab', 'rag');
  tab = TABS[wanted] ? wanted : 'rag';
  document.querySelectorAll('[data-tab]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.tab === tab)));
  load();
})();
