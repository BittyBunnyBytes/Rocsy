// Rocsy Models: browse Civitai / Civitai Red / Hugging Face, download into
// ComfyUI's models folder, see what fits this machine, and update installed
// files from the Manager (checks never install anything by themselves).
const $ = id => document.getElementById(id);
// Shows /home/<user>/... paths from the server as ~/...
const HOME_RE = /^\/home\/[^/]+\//;
const state = {view: 'models', source: 'civitai', category: 'all', cursor: '', loading: false,
  hw: null, settings: {}, libFilter: 'updates', library: [], seq: 0};

async function api(path, options = {}) {
  const response = await fetch('/Models/api/' + path, options);
  if (response.status === 401) { location.href = '/login?next=/Models/'; throw Error('Signed out'); }
  let data = null;
  try { data = await response.json(); } catch { /* empty */ }
  if (!response.ok) {
    const detail = data?.detail;
    throw Error(Array.isArray(detail) ? detail.map(d => d.msg).join(', ') : detail || `Request failed (${response.status})`);
  }
  return data;
}
const post = (path, body) => api(path, {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body || {})});

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

const GB = 1024 ** 3;
function size(bytes) {
  if (!bytes) return '';
  if (bytes >= GB) return `${(bytes / GB).toFixed(bytes >= 10 * GB ? 0 : 1)} GB`;
  return `${Math.max(1, Math.round(bytes / 1024 ** 2))} MB`;
}
function count(n) {
  if (!n) return '0';
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)}k` : String(n);
}
function ago(seconds) {
  if (!seconds) return 'never';
  const s = Date.now() / 1000 - seconds;
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
function date(iso) { return iso ? new Date(iso).toLocaleDateString(undefined, {year: 'numeric', month: 'short', day: 'numeric'}) : ''; }

let toastTimer;
function toast(message, kind = '') {
  const node = $('toast');
  node.textContent = message;
  node.className = 'toast' + (kind ? ' is-' + kind : '');
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 4200);
}

function fitBadge(fit) {
  if (!fit) return el('span', 'fit fit-unknown', 'Open for fit');
  const tier = fit.tier === 'gpu' && fit.tight ? 'tight' : fit.tier;
  const badge = el('span', `fit fit-${tier}`, fit.label);
  if (fit.note) badge.title = fit.note;
  return badge;
}

// Card badge for the recommended quant: ★ + tag in its fit color when it runs here,
// red ✕ with the smallest quant when nothing fits.
function recBadge(rec) {
  const tier = rec.tier === 'gpu' && rec.tight ? 'tight' : rec.tier;
  const badge = el('span', `fit fit-${tier}`, tier === 'no' ? `✕ ${rec.tag}` : `★ ${rec.tag}`);
  badge.title = tier === 'no'
    ? `Even the smallest quant (${rec.tag}) doesn't run here${rec.note ? ': ' + rec.note : ''}`
    : `Recommended for your hardware: ${rec.tag} · ${rec.label}${rec.note ? ' — ' + rec.note : ''}`;
  return badge;
}

// The recommended quant spelled out under the badge: tag chip, size, and how it runs.
function recLine(rec) {
  const line = el('p', 'result-rec' + (rec.tier === 'no' ? ' is-no' : ''));
  if (rec.tier === 'no') {
    line.append('Nothing fits — smallest: ', el('span', 'tag quant', rec.tag));
    if (rec.size) line.append(` · ${size(rec.size)}`);
  } else {
    line.append('Recommended: ', el('span', 'tag quant', rec.tag));
    line.append(` · ${rec.size ? size(rec.size) + ' · ' : ''}${rec.label}`);
  }
  return line;
}

const CATEGORY = {image: 'Image', video: 'Video', llm: 'LLM', audio: 'Audio', other: 'Other'};
const SOURCE = {civitai: 'Civitai', civitai_red: 'Civitai Red', hf: 'Hugging Face', local: 'Made here'};
const ICONS = {
  image: '<path d="M3 5.5A2.5 2.5 0 0 1 5.5 3h13A2.5 2.5 0 0 1 21 5.5v13a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 18.5z"/><circle cx="9" cy="9.5" r="1.8"/><path d="M20.5 16l-5-5L6 20.5"/>',
  video: '<rect x="3" y="6" width="13" height="12" rx="2.5"/><path d="M16 10.5l5-3v9l-5-3"/>',
  llm: '<path d="M4 5h16v10H9l-5 4z"/><path d="M8 9h8M8 12h5"/>',
  audio: '<path d="M9 18V6l11-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>',
  other: '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5"/>',
};
function placeholder(category) {
  const box = el('div', 'thumb-placeholder');
  box.innerHTML = `<svg viewBox="0 0 24 24" width="40" height="40" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[category] || ICONS.other}</svg>`;
  return box;
}

// Hugging Face models without a preview show their lab's logo (Qwen, Mistral, ...); the cube if it won't load.
function labLogo(url, category) {
  const box = el('div', 'thumb');
  const frame = el('div', 'thumb-placeholder lab-logo');
  const img = el('img');
  img.loading = 'lazy';
  img.decoding = 'async';
  img.alt = '';
  img.src = url;
  img.onerror = () => { if (!retryOnce(img)) box.replaceChildren(placeholder(category)); };
  frame.append(img);
  box.append(frame);
  return box;
}

// Videos load lazily: the source is attached only near the screen and dropped again far away,
// so a long scroll of video cards doesn't keep hundreds of decoders and downloads alive.
// Until then (and always in list view) the card shows its still poster.
const onScreen = new IntersectionObserver(entries => {
  for (const entry of entries) {
    if (entry.isIntersecting && !document.hidden && entry.target.src) entry.target.play().catch(() => {});
    else entry.target.pause();
  }
}, {threshold: 0.25});
const nearScreen = new IntersectionObserver(entries => {
  for (const {target: video, isIntersecting} of entries) {
    if (isIntersecting && !video.closest('.is-list')) {
      if (!video.getAttribute('src')) {
        video.src = video.dataset.src;
        if (inView(video) && !document.hidden) video.play().catch(() => {});
      }
    } else if (video.getAttribute('src')) {
      video.pause();
      video.removeAttribute('src');
      video.load();  // frees the decoder; the bytes stay in the browser cache
    }
  }
}, {rootMargin: '600px 0px'});
function inView(node) {
  const box = node.getBoundingClientRect();
  return box.bottom > 0 && box.top < innerHeight && box.width > 0;
}
document.addEventListener('visibilitychange', () => {
  document.querySelectorAll('.thumb video[src]').forEach(video => {
    if (document.hidden) video.pause();
    else if (inView(video)) video.play().catch(() => {});
  });
});

// A preview that fails gets one more try a moment later (a dropped connection or a slow
// upstream fetch on the server) before the card falls back to its placeholder icon.
function retryOnce(node) {
  if (node.dataset.retried) return false;
  node.dataset.retried = '1';
  const src = node.getAttribute('src');
  setTimeout(() => {
    if (!node.isConnected || node.getAttribute('src') !== src) return;
    node.removeAttribute('src');
    node.src = src;
  }, 1500 + Math.random() * 1000);
  return true;
}

function media(url, kind, nsfw, poster) {
  const box = el('div', 'thumb');
  if (!url) { box.append(placeholder()); return box; }
  const node = kind === 'video' ? el('video') : el('img');
  if (kind === 'video') {
    // Silent looping preview, no controls; it only plays while on screen.
    Object.assign(node, {muted: true, defaultMuted: true, loop: true, playsInline: true, preload: 'none',
      disablePictureInPicture: true, disableRemotePlayback: true});
    if (poster) node.poster = poster;
    node.dataset.src = url;
    node.setAttribute('muted', '');
    node.setAttribute('playsinline', '');
    node.setAttribute('aria-hidden', 'true');
    node.tabIndex = -1;
    onScreen.observe(node);
    nearScreen.observe(node);
  } else {
    node.loading = 'lazy';
    node.decoding = 'async';
    node.alt = '';
    node.src = url;
  }
  node.onerror = () => {
    if (!node.getAttribute('src')) return;  // a video's source was just released (off screen)
    if (retryOnce(node)) return;
    node.replaceWith(placeholder());
  };
  box.append(node);
  if (nsfw && state.settings.blur_nsfw) {
    box.classList.add('is-blurred');
    const reveal = el('button', 'reveal', 'Mature · show');
    reveal.type = 'button';
    reveal.addEventListener('click', event => { event.stopPropagation(); box.classList.remove('is-blurred'); reveal.remove(); });
    box.append(reveal);
  }
  return box;
}

// Preview gallery on the model page: every image/video of the version: a main view with
// prev/next arrows + counter and a thumbnail strip (the H3 LoRA gallery's pattern).
// ←/→ or a swipe steps through it; clicking the main view expands it to the whole screen.
let activeGallery = null;  // the open page's gallery, for the arrow keys
function previewGallery(version) {
  const seen = new Set();
  const items = [];
  const add = (url, full, kind, nsfw, poster) => {
    if (url && !seen.has(url)) { seen.add(url); items.push({url, full: full || url, kind: kind === 'video' ? 'video' : 'image', nsfw: !!nsfw, poster}); }
  };
  add(version.preview, version.full, version.preview_kind, version.nsfw, version.poster);
  for (const image of version.images || []) add(image.url, image.full, image.type, version.nsfw || (image.nsfwLevel || 1) >= 4, image.poster);
  activeGallery = null;
  if (!items.length) return null;
  if (items.length < 2) return media(items[0].full, items[0].kind, items[0].nsfw, items[0].poster);

  const wrap = el('div', 'gallery');
  const main = el('div', 'gallery-main');
  const counter = el('span', 'gallery-count');
  const prev = el('button', 'gallery-nav gallery-prev', '‹');
  const next = el('button', 'gallery-nav gallery-next', '›');
  const close = el('button', 'gallery-close', '×');
  prev.type = next.type = close.type = 'button';
  prev.setAttribute('aria-label', 'Previous preview');
  next.setAttribute('aria-label', 'Next preview');
  close.setAttribute('aria-label', 'Close full screen');
  const strip = el('div', 'gallery-thumbs');
  let index = 0;

  const show = at => {
    index = (at + items.length) % items.length;
    const item = items[index];
    main.querySelectorAll('video').forEach(video => { onScreen.unobserve(video); nearScreen.unobserve(video); });
    main.replaceChildren(media(item.full, item.kind, item.nsfw, item.poster), prev, next, counter, close);
    counter.textContent = `${index + 1} / ${items.length}${item.kind === 'video' ? ' · video' : ''}`;
    strip.querySelectorAll('.gallery-thumb').forEach((thumb, i) => {
      thumb.classList.toggle('is-active', i === index);
      if (i === index) thumb.scrollIntoView({block: 'nearest', inline: 'nearest'});
    });
    // Warm the neighbours so stepping through images doesn't wait on the network.
    for (const near of [items[(index + 1) % items.length], items[(index - 1 + items.length) % items.length]]) {
      if (near.kind === 'image') new Image().src = near.full;
    }
  };
  const expand = on => {
    wrap.classList.toggle('is-expanded', on);
    document.body.classList.toggle('gallery-open', on);
  };
  prev.addEventListener('click', event => { event.stopPropagation(); show(index - 1); });
  next.addEventListener('click', event => { event.stopPropagation(); show(index + 1); });
  close.addEventListener('click', event => { event.stopPropagation(); expand(false); });
  main.addEventListener('click', event => {
    if (event.target.closest('.reveal')) return;  // "Mature · show" only unblurs
    if (!wrap.classList.contains('is-expanded')) expand(true);
  });

  let touch = null;
  main.addEventListener('touchstart', event => { touch = event.touches.length === 1 ? event.touches[0] : null; }, {passive: true});
  main.addEventListener('touchend', event => {
    const end = event.changedTouches[0];
    if (!touch || !end) return;
    const dx = end.clientX - touch.clientX, dy = end.clientY - touch.clientY;
    touch = null;
    if (Math.abs(dx) > 45 && Math.abs(dx) > Math.abs(dy) * 1.5) show(index + (dx < 0 ? 1 : -1));
  });

  items.forEach((item, i) => {
    const thumb = el('button', 'gallery-thumb' + (item.nsfw && state.settings.blur_nsfw ? ' is-blurred' : ''));
    thumb.type = 'button';
    thumb.setAttribute('aria-label', `Preview ${i + 1}${item.kind === 'video' ? ' (video)' : ''}`);
    // The strip shows stills: images, Civitai video posters, or a video's first frame.
    const still = item.kind === 'image' ? item.url : item.poster;
    if (still) {
      const node = el('img');
      node.loading = 'lazy';
      node.decoding = 'async';
      node.alt = '';
      node.src = still;
      thumb.append(node);
    } else {
      const node = el('video');
      Object.assign(node, {muted: true, playsInline: true, preload: 'metadata'});
      node.src = item.url + '#t=0.1';
      thumb.append(node);
    }
    if (item.kind === 'video') thumb.append(el('span', 'gallery-play', '▶'));
    thumb.addEventListener('click', () => show(i));
    strip.append(thumb);
  });

  wrap.append(main, strip);
  activeGallery = {
    step: delta => show(index + delta),
    expanded: () => wrap.classList.contains('is-expanded'),
    collapse: () => expand(false),
    alive: () => wrap.isConnected,
  };
  show(0);
  return wrap;
}

// Arrow keys step the open page's gallery; Esc leaves full screen before it closes the page.
document.addEventListener('keydown', event => {
  if (!activeGallery?.alive() || $('drawer').hidden) return;
  if (event.target.closest?.('input, select, textarea, [contenteditable]')) return;
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
    event.preventDefault();
    activeGallery.step(event.key === 'ArrowRight' ? 1 : -1);
  } else if (event.key === 'Escape' && activeGallery.expanded()) {
    event.stopImmediatePropagation();
    activeGallery.collapse();
  }
}, true);

// ---- views ------------------------------------------------------------------------

// Tabs and the model drawer are history steps, so the phone's Back button
// closes the drawer, then walks back through the tabs you visited (each keeps
// its results and scroll position) before leaving the page.
const scrollFor = {};
try { history.scrollRestoration = 'manual'; } catch { /* old browsers */ }  // setView restores each tab's scroll itself
const gridFor = {};  // models / loras keep their own results while you are elsewhere
function setView(view, {push = true} = {}) {
  if (view === state.view && state.started) return;
  if (state.started) scrollFor[state.view] = window.scrollY;
  if (state.started && ['models', 'loras'].includes(state.view)) {
    state.seq++;  // a search still loading belongs to the tab being left
    gridFor[state.view] = {nodes: [...$('results').children].filter(n => !n.classList.contains('skeleton') || !state.seen.size),
                           cursor: state.cursor, more: $('more').hidden, seen: state.seen};
  }
  const previous = state.view;
  state.view = view;
  state.started = true;
  document.querySelectorAll('.view-tab').forEach(tab => {
    const on = tab.dataset.view === view;
    tab.classList.toggle('is-active', on);
    tab.setAttribute('aria-selected', String(on));
  });
  $('view-browse').hidden = !['models', 'loras'].includes(view);
  $('view-hermes').hidden = view !== 'hermes';
  $('view-manager').hidden = view !== 'manager';
  $('view-hardware').hidden = view !== 'hardware';
  try {
    if (push && previous !== view) history.pushState({view}, '', '#' + view);
    else history.replaceState({view}, '', '#' + view);
  } catch { /* ignore */ }
  if (view === 'models' || view === 'loras') {
    $('q').placeholder = view === 'loras' ? 'Search LoRAs…' : 'Search models…';
    // Models and LoRAs share one result grid: put back what this tab showed last.
    const saved = gridFor[view];
    if (saved?.nodes.length && !saved.nodes[0].classList.contains('skeleton')) {
      $('results').replaceChildren(...saved.nodes);
      state.cursor = saved.cursor;
      state.seen = saved.seen || new Set();
      state.prefetched = null;
      $('more').hidden = saved.more;
      $('base-pick').hidden = view !== 'loras';
      $('category').hidden = view === 'loras' && !!$('base').value;
      requestAnimationFrame(fillScreen);   // a page that was loading when you left
    } else {
      search(true);
    }
  }
  if (view === 'manager') setSub(state.sub || 'updates');
  if (view === 'hermes') { loadHermesStatus(); searchHermes($('hermes-query').value); }
  if (view === 'hardware') renderHardware();
  const top = scrollFor[view] || 0;
  window.scrollTo(0, top);
  requestAnimationFrame(() => window.scrollTo(0, top));
}
document.querySelectorAll('.view-tab').forEach(tab => tab.addEventListener('click', () => setView(tab.dataset.view)));

// ---- Hermes add-ons ---------------------------------------------------------------

let hermesKind = 'skills';
let hermesSearchSeq = 0;
let hermesPollTimer;
let hermesResults = [];
let hermesInstalled = {};
let hermesLastQuery = '';
let hermesLastCompletedJob = '';
let hermesRunning = false;
let hermesInputTimer;

async function detectHermesTab() {
  try {
    const data = await api('hermes/addons');
    $('hermes-tab').hidden = !data.installed;
    if (data.installed && state.view === 'hermes') loadHermesStatus();
  } catch { /* Core Backend scan can still reveal the installation later. */ }
}

function updateHermesTab(extras) {
  const installed = !!extras?.hermes?.installed;
  $('hermes-tab').hidden = !installed;
  if (!installed && state.view === 'hermes') setView('models');
}

async function loadHermesStatus() {
  clearTimeout(hermesPollTimer);
  if (state.view !== 'hermes') return;
  try {
    const data = await api('hermes/addons');
    $('hermes-tab').hidden = !data.installed;
    if (!data.installed) { $('hermes-status').textContent = 'Install Hermes in Manager → Core Backend to use this tab.'; return; }
    hermesRunning = data.running;
    if (hermesResults.length) renderHermesResults();
    if (!data.running) { $('hermes-status').textContent = 'Start Rocsy Hermes to download add-ons. The catalog is still available to browse.'; return; }
    const job = data.job || {};
    $('hermes-job').hidden = !['running', 'failed', 'complete'].includes(job.phase);
    if (!$('hermes-job').hidden) {
      $('hermes-job-title').textContent = `${job.identifier || 'Add-on'} · ${job.phase}`;
      if (job.phase === 'running' || job.phase === 'failed') {
        const log = await api('hermes/addons/log');
        $('hermes-job-log').textContent = log.log || job.error || 'Waiting for Hermes…';
      } else $('hermes-job-log').textContent = job.kind === 'plugins' ? 'Downloaded. Enable this plugin in Hermes when ready.' : 'Downloaded. Available to new Hermes sessions.';
    }
    if (job.phase === 'running') hermesPollTimer = setTimeout(loadHermesStatus, 2500);
    else if (job.phase === 'complete' && job.finished && String(job.finished) !== hermesLastCompletedJob) {
      hermesLastCompletedJob = String(job.finished);
      if (hermesResults.length) searchHermes(hermesLastQuery, {quiet: true});
    }
  } catch (error) {
    $('hermes-status').textContent = error.message;
  }
}

function renderHermesResults() {
  const wrap = $('hermes-results');
  if (!hermesResults.length) { wrap.replaceChildren(el('p', 'empty', 'No add-ons found. Try another search.')); return; }
  const rows = hermesResults.map(item => {
    const identifier = hermesKind === 'skills' ? item.identifier : item.name;
    const installed = hermesKind === 'skills'
      ? Object.values(hermesInstalled).includes(identifier)
      : !!hermesInstalled[item.name];
    const row = el('article', 'hermes-addon');
    const visual = el('div', 'hermes-addon-visual');
    const imageUrl = hermesIconUrl(item);
    const fallback = el('span', 'hermes-addon-glyph', (item.title || item.name || 'H').slice(0, 1).toUpperCase());
    visual.append(fallback);
    if (imageUrl) {
      const picture = el('img', item.image ? 'hermes-addon-art' : 'hermes-addon-avatar');
      picture.alt = '';
      picture.loading = 'lazy';
      picture.decoding = 'async';
      picture.src = imageUrl;
      picture.onerror = () => {
        if (item.image && !picture.dataset.fallback) {
          const avatar = hermesAvatarUrl(item);
          if (avatar) {
            picture.dataset.fallback = '1';
            picture.className = 'hermes-addon-avatar';
            picture.src = avatar;
            return;
          }
        }
        picture.remove();
      };
      visual.append(picture);
    }
    const info = el('div', 'hermes-addon-info');
    info.append(el('strong', '', item.title || item.name || identifier));
    info.append(el('p', 'hint', item.description || 'No description supplied.'));
    const tags = el('div', 'hermes-addon-tags');
    tags.append(el('span', 'tag', hermesKind === 'skills' ? item.source || 'Skill' : item.category || 'Plugin'));
    tags.append(el('span', 'tag', item.trust_level || item.tier || 'community'));
    if (item.version) tags.append(el('span', 'tag', item.version));
    info.append(tags);
    const actions = el('div', 'hermes-addon-actions');
    if (item.repo && /^https:\/\//.test(item.repo)) {
      const link = el('a', 'secondary-button small', 'Source');
      link.href = item.repo; link.target = '_blank'; link.rel = 'noopener noreferrer';
      actions.append(link);
    }
    const button = el('button', installed ? 'secondary-button small' : 'primary-button small', installed ? 'Downloaded' : 'Download');
    button.type = 'button'; button.disabled = installed || !hermesRunning;
    button.addEventListener('click', () => installHermesAddon(identifier, button));
    actions.append(button);
    row.append(visual, info, actions);
    return row;
  });
  wrap.replaceChildren(...rows);
}

function hermesIconUrl(item) {
  if (item.image && /^https:\/\/raw\.githubusercontent\.com\/.*\.(png|jpe?g|webp|gif)$/i.test(item.image))
    return `/Models/api/hermes/catalog-image?u=${encodeURIComponent(item.image)}`;
  return hermesAvatarUrl(item);
}

function hermesAvatarUrl(item) {
  if (/^[A-Za-z0-9-]+$/.test(item.icon_owner || ''))
    return `/Models/api/thumb?u=${encodeURIComponent(`https://github.com/${item.icon_owner}.png?size=160`)}`;
  if (item.source === 'official' || item.tier === 'official') return '/Models/static/hermes-icon.svg';
  const repo = item.repo || '';
  let owner = /^https:\/\/github\.com\/([A-Za-z0-9-]+)/i.exec(repo)?.[1];
  if (!owner && /^[A-Za-z0-9-]+\/[A-Za-z0-9._-]+/.test(repo)) owner = repo.split('/')[0];
  if (!owner && item.identifier?.startsWith('skills-sh/')) owner = item.identifier.split('/')[1];
  if (!owner && /^[A-Za-z0-9-]+\/[A-Za-z0-9._-]+/.test(item.identifier || '')) owner = item.identifier.split('/')[0];
  return owner ? `/Models/api/thumb?u=${encodeURIComponent(`https://github.com/${owner}.png?size=160`)}` : '';
}

async function searchHermes(query, {quiet = false, remote = false} = {}) {
  const seq = ++hermesSearchSeq;
  hermesLastQuery = query.trim();
  const kind = hermesKind;
  if (!quiet) $('hermes-status').textContent = 'Loading catalog…';
  try {
    const data = await api(`hermes/addons/catalog/${kind}?q=${encodeURIComponent(hermesLastQuery)}`);
    if (seq !== hermesSearchSeq) return;
    hermesResults = data.results || [];
    hermesInstalled = data.installed || {};
    renderHermesResults();
    $('hermes-status').textContent = data.total > hermesResults.length
      ? `Showing ${hermesResults.length} of ${data.total} ${kind}; narrow the search for more.`
      : `${hermesResults.length} ${kind} found`;
  } catch (error) {
    if (seq === hermesSearchSeq) $('hermes-status').textContent = error.message;
    return;
  }
  if (remote && kind === 'skills' && hermesLastQuery && hermesRunning) {
    $('hermes-status').textContent = `${hermesResults.length} skills shown · checking online sources…`;
    try {
      const data = await api(`hermes/addons/search/skills?q=${encodeURIComponent(hermesLastQuery)}`);
      if (seq !== hermesSearchSeq) return;
      const seen = new Set(hermesResults.map(item => item.identifier));
      for (const item of data.results || []) if (!seen.has(item.identifier)) { hermesResults.push(item); seen.add(item.identifier); }
      hermesInstalled = {...hermesInstalled, ...(data.installed || {})};
      renderHermesResults();
      $('hermes-status').textContent = `${hermesResults.length} skills found, including online sources`;
    } catch (error) {
      if (seq === hermesSearchSeq) $('hermes-status').textContent = `Local results shown · online search: ${error.message}`;
    }
  }
}

async function installHermesAddon(identifier, button) {
  button.disabled = true;
  button.textContent = 'Starting…';
  try {
    await post(`hermes/addons/install/${hermesKind}/${encodeURIComponent(identifier)}`);
    button.textContent = 'Downloading…';
    $('hermes-status').textContent = `Downloading ${identifier} into Rocsy Hermes…`;
    loadHermesStatus();
  } catch (error) {
    button.disabled = false;
    button.textContent = 'Download';
    $('hermes-status').textContent = error.message;
  }
}

$('hermes-kind').addEventListener('click', event => {
  const button = event.target.closest('[data-hermes-kind]');
  if (!button || button.dataset.hermesKind === hermesKind) return;
  hermesKind = button.dataset.hermesKind;
  $('hermes-kind').querySelectorAll('[data-hermes-kind]').forEach(item => item.setAttribute('aria-checked', String(item === button)));
  $('hermes-query').placeholder = hermesKind === 'skills' ? 'Search skills online…' : 'Search reviewed plugins…';
  $('hermes-online-search').textContent = hermesKind === 'skills' ? 'Search online' : 'Search';
  hermesResults = [];
  searchHermes($('hermes-query').value);
});
$('hermes-query').addEventListener('input', () => {
  clearTimeout(hermesInputTimer);
  hermesInputTimer = setTimeout(() => searchHermes($('hermes-query').value), 160);
});
$('hermes-search-form').addEventListener('submit', event => {
  event.preventDefault();
  clearTimeout(hermesInputTimer);
  searchHermes($('hermes-query').value, {remote: hermesKind === 'skills'});
});

// ---- browse --------------------------------------------------------------------------

function radio(groupId, attr, onPick) {
  $(groupId).addEventListener('click', event => {
    const button = event.target.closest(`[data-${attr}]`);
    if (!button) return;
    $(groupId).querySelectorAll(`[data-${attr}]`).forEach(b => b.setAttribute('aria-checked', String(b === button)));
    onPick(button.dataset[attr]);
  });
}
radio('source', 'source', value => { state.source = value; if (bases) baseChanged(false); search(true); });
radio('category', 'category', value => { state.category = value; search(true); });
$('search-form').addEventListener('submit', event => { event.preventDefault(); search(true); });
$('sort').addEventListener('change', () => search(true));
$('gguf').addEventListener('change', () => search(true));

// "Fits this machine" hides cards whose best file won't fit in VRAM, RAM offload or NVMe ngram.
const fitsHere = item => (item.rec || item.fit || {}).tier !== 'no';
try { state.fitsOnly = localStorage.getItem('models.fits') === '1'; } catch { state.fitsOnly = false; }
$('fits').checked = state.fitsOnly;
$('fits').addEventListener('change', () => {
  state.fitsOnly = $('fits').checked;
  try { localStorage.setItem('models.fits', state.fitsOnly ? '1' : '0'); } catch { /* private mode */ }
  search(true);
});
// "Supported only" keeps cards a studio template, a saved ComfyUI workflow or llama.cpp can run.
try { state.supportedOnly = localStorage.getItem('models.supportedOnly') === '1'; } catch { state.supportedOnly = false; }
$('supported-only').checked = state.supportedOnly;
$('supported-only').addEventListener('change', () => {
  state.supportedOnly = $('supported-only').checked;
  try { localStorage.setItem('models.supportedOnly', state.supportedOnly ? '1' : '0'); } catch { /* private mode */ }
  search(true);
});
// "Hide installed" drops cards whose model already has files on this machine (per browser).
try { state.hideInstalled = localStorage.getItem('models.hideInstalled') === '1'; } catch { state.hideInstalled = false; }
$('hide-installed').checked = state.hideInstalled;
$('hide-installed').addEventListener('change', () => {
  state.hideInstalled = $('hide-installed').checked;
  try { localStorage.setItem('models.hideInstalled', state.hideInstalled ? '1' : '0'); } catch { /* private mode */ }
  search(true);
});
let typing;
$('q').addEventListener('input', () => { clearTimeout(typing); typing = setTimeout(() => search(true), 450); });
$('more').addEventListener('click', () => { $('more').textContent = 'Load more'; search(false); });

// ---- LoRA base model filter -------------------------------------------------------------

let bases = null;
const BASE_KEY = 'models.loraBase';
// Started at boot and awaited by LoRA searches + Installed: one shared request fills the picker once.
let basesLoading = null;
function loadBases() {
  return basesLoading || (basesLoading = fillBases());
}
async function fillBases() {
  let data;
  try { data = await api('bases'); } catch { data = {installed: [], all: []}; }
  bases = data;
  const select = $('base');
  const mine = el('optgroup');
  mine.label = 'On this machine';
  bases.installed.forEach(b => mine.append(new Option(`${b.base} · ${b.files.length} model${b.files.length === 1 ? '' : 's'}`, b.base)));
  const rest = el('optgroup');
  rest.label = 'Other base models';
  bases.all.forEach(name => rest.append(new Option(name, name)));
  if (bases.installed.length) select.append(mine);
  select.append(rest);
  let saved = '';
  try { saved = localStorage.getItem(BASE_KEY) || ''; } catch { /* private mode */ }
  if ([...select.options].some(o => o.value === saved)) select.value = saved;
  baseChanged(false);
}

function baseChanged(run = true) {
  const value = $('base').value;
  try { localStorage.setItem(BASE_KEY, value); } catch { /* private mode */ }
  const info = bases?.installed.find(b => b.base === value);
  const notes = [];
  if (info) notes.push(`Installed: ${info.files.slice(0, 3).join(', ')}${info.files.length > 3 ? '…' : ''}`);
  if (info?.also?.length) notes.push(`${info.also.join(', ')} LoRAs included: they load on ${value} too`);
  if (value && state.source === 'hf' && !(bases?.hf_mapped || []).includes(value))
    notes.push('Hugging Face has no tag for this base, so it searches by name');
  $('base-note').textContent = notes.join(' · ');
  // A base model already decides the category.
  $('category').hidden = !!value && state.view === 'loras';
  if (run) search(true);
}
$('base').addEventListener('change', () => baseChanged());

// Infinite scroll: a sentinel under the results asks for the next page well before you reach it,
// and the page after the one on screen is fetched in the background so appending is instant.
const AHEAD = 1600;  // px below the viewport at which the next page loads
state.seen = new Set();      // source:id already shown (sources sometimes repeat across pages)
state.prefetched = null;     // {key, promise} for the next page

function searchParams(cursor) {
  const kind = state.view === 'loras' ? 'lora' : 'model';
  const base = kind === 'lora' ? $('base').value : '';
  return new URLSearchParams({source: state.source, kind, category: base ? 'all' : state.category, q: $('q').value.trim(),
    sort: $('sort').value, cursor, gguf: $('gguf').checked, base});
}

function page(params) {
  const key = params.toString();
  if (state.prefetched?.key === key) {
    const {promise} = state.prefetched;
    state.prefetched = null;
    return promise;
  }
  return api('search?' + key);
}

function prefetchNext() {
  if (!state.cursor) return;
  const key = searchParams(state.cursor).toString();
  if (state.prefetched?.key === key) return;
  const promise = api('search?' + key);
  promise.catch(() => {});   // reported when the page is actually needed
  state.prefetched = {key, promise};
}

const isLoading = () => state.loadingSeq === state.seq;

async function search(reset) {
  const kind = state.view === 'loras' ? 'lora' : 'model';
  if (!reset && (isLoading() || !state.cursor)) return;
  $('base-pick').hidden = kind !== 'lora';
  if (kind === 'lora') await loadBases();
  $('category').hidden = kind === 'lora' && !!$('base').value;
  if (reset) {
    state.cursor = '';
    state.seen = new Set();
    state.prefetched = null;
    $('results').replaceChildren(...skeletons());
    window.scrollTo(0, Math.min(window.scrollY, $('view-browse').offsetTop));
  }
  $('gguf-wrap').hidden = !(state.source === 'hf' && state.category === 'llm' && kind === 'model');
  $('fits-wrap').hidden = kind === 'lora';  // LoRAs always load with their base model
  $('browse-note').textContent = state.source === 'civitai_red' ? 'Mature results included.'
    : state.source === 'hf' ? 'Open a model to see every quant and which one fits best.' : '';
  const seq = ++state.seq;
  state.loadingSeq = seq;
  const params = searchParams(reset ? '' : state.cursor);
  $('more').hidden = true;
  $('end-note').hidden = true;
  const tail = reset ? [] : skeletons(4);
  $('results').append(...tail);
  try {
    const data = await page(params);
    if (seq !== state.seq) return;
    tail.forEach(node => node.remove());
    if (reset) $('results').replaceChildren();
    state.cursor = data.next || '';
    const fresh = data.items.filter(item => {
      const key = `${item.source}:${item.id}`;
      if (state.seen.has(key)) return false;
      state.seen.add(key);
      return true;
    });
    if (!state.seen.size && !state.cursor) $('results').append(el('p', 'empty', 'Nothing found. Try another category or source.'));
    const batch = document.createDocumentFragment();
    fresh.filter(item => (!state.fitsOnly || fitsHere(item)) && !(state.hideInstalled && item.installed)
        && (!state.supportedOnly || item.supported))
      .forEach(item => batch.append(card(item)));
    $('results').append(batch);
    if ((state.fitsOnly || state.hideInstalled || state.supportedOnly) && !state.cursor && !$('results').querySelector('.result:not(.skeleton)')) {
      $('results').append(el('p', 'empty', state.hideInstalled && !state.fitsOnly && !state.supportedOnly
        ? 'Everything here is already installed — turn off "Hide installed" to see it.'
        : state.supportedOnly && !state.fitsOnly && !state.hideInstalled
        ? 'Nothing here runs on this machine yet — turn off "Supported only", or save a ComfyUI workflow for the family.'
        : 'Nothing left to show — turn off "Fits this machine", "Hide installed" or "Supported only" to see everything.'));
    }
    if (!state.cursor && state.seen.size > 24) {
      $('end-note').textContent = `That's all ${state.seen.size} results.`;
      $('end-note').hidden = false;
    }
  } catch (error) {
    if (seq !== state.seq) return;
    tail.forEach(node => node.remove());
    if (reset) $('results').replaceChildren(el('p', 'empty error', error.message));
    else {
      $('more').textContent = `Couldn't load more (${error.message}) · Retry`;
      $('more').hidden = false;
    }
    state.loadingSeq = 0;
    return;
  }
  state.loadingSeq = 0;
  if (state.cursor) {
    prefetchNext();
    requestAnimationFrame(fillScreen);
  }
}

// Short pages (tall screens, list view) don't move the sentinel: keep loading until it's off screen.
function fillScreen() {
  if ($('view-browse').hidden || !state.cursor || isLoading()) return;
  if ($('scroll-sentinel').getBoundingClientRect().top < window.innerHeight + AHEAD) search(false);
}

new IntersectionObserver(entries => {
  if (entries.some(entry => entry.isIntersecting)) search(false);
}, {rootMargin: `0px 0px ${AHEAD}px 0px`}).observe($('scroll-sentinel'));

function skeletons(count = 8) {
  return Array.from({length: count}, () => el('div', 'result skeleton'));
}

// Civitai early access: the creator charges Buzz for a version until a date (then it's free), or for good.
function untilText(iso) {
  const days = Math.ceil((new Date(iso) - Date.now()) / 86400000);
  const when = new Date(iso).toLocaleDateString(undefined, {month: 'short', day: 'numeric'});
  return days <= 1 ? `${when} (within a day)` : `${when} (in ${days} days)`;
}
function paywallTag(paywall, freeVersion) {
  const tag = el('span', 'tag paywall', paywall.permanent ? '🔒 Paid' : `🔒 Early access`);
  tag.title = (paywall.permanent ? 'Paid with Buzz on Civitai.' : `Free from ${untilText(paywall.ends)}; until then it costs Buzz on Civitai.`)
    + (freeVersion ? ' An older version is free.' : '');
  return tag;
}

function card(item) {
  const node = el('button', 'result');
  node.type = 'button';
  node.append(!item.preview && item.logo ? labLogo(item.logo, item.category) : media(item.preview, item.preview_kind, item.nsfw, item.poster));
  const body = el('div', 'result-body');
  const top = el('div', 'result-top');
  top.append(el('span', 'tag', item.base || CATEGORY[item.category] || ''));
  // Server-side (support.py): studio template, saved ComfyUI workflow or llama.cpp.
  if (item.kind === 'model' && item.supported) {
    const badge = el('span', 'tag studio-template', `✓ ${item.supported}`);
    badge.title = 'Something on this machine can run this model family.';
    top.append(badge);
  }
  if (item.rec) top.append(recBadge(item.rec));
  else if (item.fit || item.kind !== 'lora') top.append(fitBadge(item.fit));
  if (item.paywall) top.prepend(paywallTag(item.paywall, item.free_version));
  if (item.installed) {
    node.classList.add('is-installed');
    const badge = el('span', 'tag installed', '✓ Installed');
    const files = item.installed.files;
    badge.title = `On this machine: ${files.slice(0, 6).join(', ')}${files.length > 6 ? ` +${files.length - 6} more` : ''}`
      + (item.installed.older ? '\nThe newest version on Civitai is not installed (Manager → Updates says whether it is an update or a variant).' : '');
    top.prepend(badge);
  }
  body.append(top);
  if (item.rec) body.append(recLine(item.rec));
  body.append(el('h3', 'result-name', item.name));
  const meta = el('p', 'result-meta');
  meta.textContent = [item.author && `by ${item.author}`, item.size && size(item.size),
    `↓ ${count(item.stats?.downloads)}`, `♥ ${count(item.stats?.likes)}`].filter(Boolean).join(' · ');
  body.append(meta);
  if (item.gated) body.append(el('span', 'tag warn', 'Gated: needs HF token'));
  node.append(body);
  node.addEventListener('click', () => openModel(item));
  // Hover (or a finger landing on it) starts loading the model page, so the drawer opens filled.
  let intent;
  node.addEventListener('pointerenter', () => { intent = setTimeout(() => modelDetail(item), 120); });
  node.addEventListener('pointerleave', () => clearTimeout(intent));
  node.addEventListener('touchstart', () => modelDetail(item), {passive: true});
  return node;
}

// Gallery or list layout for the results (remembered per browser).
function setLayout(layout) {
  $('results').classList.toggle('is-list', layout === 'list');
  $('results').querySelectorAll('.thumb video').forEach(video => { nearScreen.unobserve(video); nearScreen.observe(video); });
  document.querySelectorAll('#layout [data-layout]').forEach(b => b.setAttribute('aria-checked', String(b.dataset.layout === layout)));
  try { localStorage.setItem('models.layout', layout); } catch { /* private mode */ }
}
$('layout').addEventListener('click', event => {
  const button = event.target.closest('[data-layout]');
  if (button) setLayout(button.dataset.layout);
});
try { setLayout(localStorage.getItem('models.layout') === 'list' ? 'list' : 'grid'); } catch { setLayout('grid'); }

// ---- detail drawer ----------------------------------------------------------------------

function hideDrawer() {
  $('drawer').hidden = $('scrim').hidden = true;
  document.documentElement.classList.remove('drawer-open');
  $('d-body').querySelectorAll('video').forEach(v => v.pause());
  activeGallery?.collapse();  // Back on a phone while the gallery is full screen
}
// The drawer owns a history entry: closing it any way goes back one step.
function closeDrawer() {
  if (history.state?.drawer) history.back();
  else hideDrawer();
}

window.addEventListener('popstate', event => {
  const target = event.state || {view: location.hash.slice(1) || 'models'};
  if (!$('drawer').hidden && !target.drawer) hideDrawer();
  if (['models', 'loras', 'manager', 'hardware'].includes(target.view) && target.view !== state.view) {
    setView(target.view, {push: false});
  }
});
$('d-close').addEventListener('click', closeDrawer);
$('scrim').addEventListener('click', closeDrawer);
document.addEventListener('keydown', event => { if (event.key === 'Escape' && !$('drawer').hidden) closeDrawer(); });

let destFolders = null;
async function folders() {
  if (!destFolders) destFolders = (await api('dests')).folders;
  return destFolders;
}

// One request per model page while it's fresh: shared by the hover prefetch and the click.
const detailRequests = new Map();
function modelDetail(item) {
  const key = `${item.source}|${item.id}|${item.kind}`;
  const hit = detailRequests.get(key);
  if (hit && Date.now() - hit.at < 240000) return hit.promise;
  const promise = api(`model?${new URLSearchParams({source: item.source, id: item.id, kind: item.kind})}`);
  promise.catch(() => detailRequests.delete(key));
  detailRequests.set(key, {at: Date.now(), promise});
  if (detailRequests.size > 80) detailRequests.delete(detailRequests.keys().next().value);
  return promise;
}

async function openModel(item) {
  $('d-name').textContent = item.name;
  $('d-meta').textContent = `${SOURCE[item.source]} · loading…`;
  $('d-body').replaceChildren(el('p', 'empty', 'Loading files…'));
  if ($('drawer').hidden) {
    try { history.pushState({view: state.view, drawer: true}, ''); } catch { /* ignore */ }
  }
  $('drawer').hidden = $('scrim').hidden = false;
  document.documentElement.classList.add('drawer-open');  // the results behind it stay put
  try {
    const [detail, dirs] = await Promise.all([modelDetail(item), folders()]);
    renderDetail(structuredClone(detail), dirs);
  } catch (error) {
    $('d-body').replaceChildren(el('p', 'empty error', error.message));
  }
}

function renderDetail(detail, dirs) {
  $('d-meta').replaceChildren();
  const link = el('a', '', `Open on ${SOURCE[detail.source]} ↗`);
  link.href = detail.url;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  $('d-meta').append(`${detail.author ? 'by ' + detail.author + ' · ' : ''}${CATEGORY[detail.category] || ''}${detail.license ? ' · ' + detail.license : ''} · `, link);
  const body = $('d-body');
  body.replaceChildren();
  const versions = detail.versions || [];
  if (!versions.length) { body.append(el('p', 'empty', 'No downloadable files.')); return; }

  let select = null;
  if (versions.length > 1) {
    const row = el('div', 'field');
    const label = el('label', '', 'Version');
    select = el('select');
    select.id = 'd-version';
    label.htmlFor = 'd-version';
    versions.forEach(v => select.append(new Option(`${v.name}${v.base ? ' · ' + v.base : ''}${v.published ? ' · ' + date(v.published) : ''}`, v.id)));
    row.append(label, select);
    body.append(row);
  }
  const holder = el('div');
  body.append(holder);
  if (detail.description) {
    const about = el('details', 'about');
    about.open = true;  // open the page with the description already showing
    about.append(el('summary', '', 'About'), el('p', 'about-text', detail.description));
    body.append(about);
  }
  const draw = () => renderVersion(holder, detail, versions.find(v => v.id === (select ? select.value : versions[0].id)), dirs);
  select?.addEventListener('change', draw);
  draw();
}

function renderVersion(holder, detail, version, dirs) {
  holder.replaceChildren();
  const gallery = previewGallery(version);
  if (gallery) holder.append(gallery);
  const facts = el('p', 'hint');
  facts.textContent = [version.base && `Base: ${version.base}`, version.published && `Published ${date(version.published)}`,
    version.paywall && (version.paywall.permanent ? '🔒 Paid with Buzz' : `🔒 Early access until ${untilText(version.paywall.ends)}`)]
    .filter(Boolean).join(' · ');
  holder.append(facts);
  if (version.triggers?.length) {
    const triggers = el('div', 'triggers');
    triggers.append(el('span', 'hint', 'Trigger words:'));
    version.triggers.forEach(word => {
      const chip = el('button', 'chip', word);
      chip.type = 'button';
      chip.title = 'Copy';
      chip.addEventListener('click', () => navigator.clipboard?.writeText(word).then(() => toast('Copied')));
      triggers.append(chip);
    });
    holder.append(triggers);
  }
  const variants = version.variants || [];
  if (!variants.length) { holder.append(el('p', 'empty', 'No files in this version.')); return; }
  // The model itself is picked by quant; MTP and vision heads get their own sections; draft models,
  // VAEs, encoders etc. are extras.
  const HEADS = ['MTP head', 'Vision head'];
  const main = variants.filter(v => !v.role);
  const mtpHeads = variants.filter(v => v.role === 'MTP head');
  const visionHeads = variants.filter(v => v.role === 'Vision head');
  const extras = variants.filter(v => v.role && !HEADS.includes(v.role));
  const pickFrom = main.length ? main : extras.length ? extras : [...mtpHeads, ...visionHeads];
  if (main.length) holder.append(headSupport(main, mtpHeads, visionHeads));
  holder.append(quantPicker(detail, version, pickFrom, dirs));
  if (main.length) {
    const auto = visionHeads.find(v => v.auto);
    holder.append(...[
      headSection('MTP prediction heads', `${mtpHeads.length}`, 'Speculative-decoding drafters made for this model: it writes several tokens per step. '
        + 'Download one, then pick it in Llama → the model → Parameters → Draft model (Draft type: MTP).', mtpHeads, detail, version, dirs),
      headSection('Vision heads (mmproj)', `${visionHeads.length}`, 'Let the model read pictures. To swap one in, pick it in Llama → the model → Parameters → Vision head.'
        + (auto ? ` Downloading a quant above already brings ${auto.file_label} with it; get another precision here.` : ''),
        visionHeads, detail, version, dirs),
    ].filter(Boolean));
  }
  if (main.length && extras.length) {
    const more = el('details', 'extras');
    more.append(el('summary', '', `Other files (${extras.length}) · ${[...new Set(extras.map(v => v.role))].join(', ')}`));
    const list = el('div', 'variants');
    const TIER = {gpu: 0, multi: 1, nvme: 2, offload: 3, unknown: 4, no: 5};
    [...extras].sort((a, b) => (TIER[a.fit?.tier] ?? 3) - (TIER[b.fit?.tier] ?? 3) || b.quant.rank - a.quant.rank)
      .forEach(variant => list.append(variantRow(detail, version, variant, dirs)));
    more.append(list);
    holder.append(more);
  }
}

// What the quants support, at a glance: MTP built in or as separate heads, and vision heads.
function headSupport(main, mtpHeads, visionHeads) {
  const line = el('div', 'head-support');
  const builtIn = main.filter(v => v.mtp).length;
  const chip = (cls, text, title) => { const c = el('span', `support-chip ${cls}`, text); c.title = title; return c; };
  if (builtIn) line.append(chip('is-mtp', builtIn === main.length ? 'MTP built in' : `MTP built into ${builtIn} of ${main.length} quants`,
    'These quants carry their own multi-token-prediction head: Draft type MTP in Llama → Parameters needs no extra file.'));
  if (mtpHeads.length) line.append(chip('is-mtp', `MTP heads: ${mtpHeads.length} separate file${mtpHeads.length === 1 ? '' : 's'}`,
    'Download one from MTP prediction heads below and pick it as the Draft model.'));
  if (!builtIn && !mtpHeads.length) line.append(chip('is-none', 'No MTP', 'No MTP head in this repo: n-gram or a small draft model can still speed it up.'));
  line.append(visionHeads.length ? chip('is-vision', `Vision: ${visionHeads.length} head${visionHeads.length === 1 ? '' : 's'}`, 'This model can read pictures with a vision head (mmproj).')
    : chip('is-none', 'Text only', 'No vision head (mmproj) in this repo.'));
  return line;
}

function headSection(title, count, hint, list, detail, version, dirs) {
  if (!list.length) return null;
  const section = el('section', 'head-section');
  const head = el('div', 'quant-head');
  head.append(el('h3', '', title), el('span', 'hint', count));
  const rows = el('div', 'variants');
  list.forEach(variant => rows.append(variantRow(detail, version, variant, dirs)));
  section.append(head, el('p', 'hint', hint), rows);
  return section;
}

// ---- quant picker ---------------------------------------------------------------------------

function quantName(variant) {
  return variant.quant_tag || variant.quant?.label || (variant.whole_repo ? 'Full repo' : variant.label);
}

// Bit class from the tag (Q4_K_M -> 4, BF16 -> 16), else from the quant table.
function quantBits(variant) {
  const tag = quantName(variant).toUpperCase();
  const q = tag.match(/(?:^|[^A-Z])I?Q(\d)/) || tag.match(/^I?Q(\d)/);
  if (q) return Number(q[1]);
  if (/F32|FP32/.test(tag)) return 32;
  if (/BF16|F16|FP16/.test(tag)) return 16;
  if (/FP8|INT8/.test(tag)) return 8;
  if (/FP4|NF4|INT4/.test(tag)) return 4;
  return Math.round(variant.quant?.bits || 16);
}

const BIT_GROUPS = [[32, '32-bit'], [16, '16-bit'], [8, '8-bit'], [6, '6-bit'], [5, '5-bit'], [4, '4-bit'], [3, '3-bit'], [2, '2-bit'], [1, '1-bit']];

function quantPicker(detail, version, variants, dirs) {
  const wrap = el('section', 'quant-picker');
  const panel = el('div', 'quant-panel');
  let chosen = variants.find(v => v.key === version.best) || variants.find(v => v.primary) || variants[0];
  const show = variant => {
    chosen = variant;
    wrap.querySelectorAll('.quant-tile').forEach(tile => tile.setAttribute('aria-checked', String(tile.dataset.key === variant.key)));
    panel.replaceChildren(quantPanel(detail, version, variant, dirs));
  };

  if (variants.length > 1) {
    const head = el('div', 'quant-head');
    head.append(el('h3', '', detail.category === 'llm' || variants.some(v => /gguf/i.test(v.format)) ? 'Choose a quant' : 'Choose a file'),
      el('span', 'hint', `${variants.length} options${version.best ? ' · ★ = best fit for your GPU' : ''}`));
    wrap.append(head);
    const legend = el('p', 'quant-legend');
    [['gpu', 'Fits in VRAM'], ['offload', 'Needs offload'], ['no', 'Too big / unsupported']].forEach(([tier, text]) => {
      legend.append(el('span', `dot fit-${tier}`), text);
    });
    wrap.append(legend);

    // Quants that share a name (an MTP build beside the plain one counts as different: it has the badge).
    const nameKey = v => quantName(v) + (v.mtp ? '+mtp' : '');
    const names = variants.map(nameKey);
    // For real duplicates, only the part of the file name that differs (not the whole long stem).
    const stems = variants.map(v => (v.file_label || quantName(v)).split('/').pop().replace(/\.(gguf|safetensors)$/i, ''));
    let shared = stems[0] || '';
    for (const stem of stems) while (shared && !stem.startsWith(shared)) shared = shared.slice(0, -1);
    shared = shared.replace(/[^-_.]*$/, '');  // cut at a separator, not mid-word
    const groups = new Map();
    for (const variant of variants) {
      const bits = quantBits(variant);
      const group = BIT_GROUPS.find(([b]) => bits >= b)?.[1] || 'Other';
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(variant);
    }
    const order = [...BIT_GROUPS.map(g => g[1]), 'Other'];
    const grid = el('div', 'quant-groups');
    grid.setAttribute('role', 'radiogroup');
    grid.setAttribute('aria-label', 'Quant');
    [...groups.keys()].sort((a, b) => order.indexOf(a) - order.indexOf(b)).forEach(group => {
      const row = el('div', 'quant-row');
      row.append(el('span', 'quant-bits', group));
      const tiles = el('div', 'quant-tiles');
      groups.get(group).sort((a, b) => b.size - a.size).forEach(variant => {
        const name = quantName(variant);
        const tier = variant.fit?.tier === 'gpu' && variant.fit?.tight ? 'tight' : (variant.fit?.tier || 'unknown');
        const tile = el('button', `quant-tile tier-${tier}`);
        tile.type = 'button';
        tile.dataset.key = variant.key;
        tile.setAttribute('role', 'radio');
        tile.title = `${variant.file_label || variant.files[0].name}${variant.fit ? ' · ' + variant.fit.label : ''}`;
        const label = el('span', 'quant-name');
        if (variant.key === version.best) label.append(el('span', 'star', '★'));
        // Two files with the same quant: say which (e.g. a different source folder).
        const text = el('span', 'quant-label');
        text.textContent = names.filter(n => n === nameKey(variant)).length > 1
          ? (variant.file_label || name).split('/').pop().replace(/\.(gguf|safetensors)$/i, '').slice(shared.length) || name : name;
        // The MTP badge already says it: "LOW-MTP-Q6_K" reads "LOW-Q6_K".
        if (variant.mtp) text.textContent = text.textContent.replace(/(^|[-_.])MTP(?:[-_.]|$)/i, '$1').replace(/[-_.]$/, '') || name;
        label.append(text);
        const sub = el('span', 'quant-size');
        sub.append(el('span', `dot fit-${tier}`), size(variant.size + (variant.companions || []).reduce((n, f) => n + f.size, 0)));
        // On the size line, so the quant name keeps the tile's whole width.
        if (variant.mtp) { const badge = el('span', 'mtp-badge', 'MTP'); badge.title = 'MTP head built in'; sub.append(badge); }
        if (variant.installed) sub.append(el('span', 'quant-have', '✓'));
        tile.append(label, sub);
        tile.addEventListener('click', () => show(variant));
        tiles.append(tile);
      });
      row.append(tiles);
      grid.append(row);
    });
    // Arrow keys move between tiles like a radio group.
    grid.addEventListener('keydown', event => {
      const all = [...grid.querySelectorAll('.quant-tile')];
      const at = all.indexOf(document.activeElement);
      const step = {ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1}[event.key];
      if (at < 0 || !step) return;
      event.preventDefault();
      const next = all[(at + step + all.length) % all.length];
      next.focus();
      next.click();
    });
    wrap.append(grid);
  } else {
    const head = el('div', 'quant-head');
    head.append(el('h3', '', 'Download'));
    wrap.append(head);
  }
  wrap.append(panel);
  show(chosen);
  return wrap;
}

function quantPanel(detail, version, variant, dirs) {
  const card = el('div', 'quant-card' + (variant.key === version.best ? ' is-best' : ''));
  const top = el('div', 'quant-card-top');
  const title = el('div', 'quant-card-title');
  title.append(el('strong', '', quantName(variant)));
  if (variant.key === version.best) title.append(el('span', 'best', '★ Best for your hardware'));
  top.append(title, el('span', 'quant-card-size', size(variant.size + (variant.companions || []).reduce((n, f) => n + f.size, 0))));
  card.append(top);
  card.append(el('code', 'quant-file', variant.file_label || variant.files[0].name));
  const meta = el('div', 'variant-meta');
  meta.append(fitBadge(variant.fit));
  if (variant.label && !variant.label.startsWith(variant.quant?.label || quantName(variant))) meta.append(el('span', 'tag', variant.label));
  if (variant.files.length > 1 && !variant.whole_repo) meta.append(el('span', 'tag', `${variant.files.length} parts`));
  card.append(meta);
  if (variant.fit?.note) card.append(el('p', 'fit-note', variant.fit.note));
  if (variant.companions?.length) card.append(el('p', 'hint', `+ ${variant.companions[0].name} (vision projector, ${size(variant.companions[0].size)})`));
  if (variant.whole_repo) card.append(el('p', 'hint', `${variant.files.length} files, saved as one folder`));
  card.append(downloadControls(detail, version, variant, dirs, card));
  return card;
}

// Destination folder + Download button; a "goes to" line is added to `info`.
function downloadControls(detail, version, variant, dirs, info) {
  const actions = el('div', 'variant-actions');
  const dest = el('select');
  dest.setAttribute('aria-label', 'Save into');
  const target = variant.target;
  const options = new Set([target?.dest || variant.dest, variant.dest, ...dirs]);
  options.forEach(folder => dest.append(new Option(`models/${folder}`, folder)));
  dest.value = target?.dest || variant.dest;
  if (target) {
    const goes = el('p', 'goes-to', target.type === 'gallery'
      ? `→ Added to the ${target.label} with its triggers, description and previews`
      : '→ Added to llama-swap as a new model');
    info.append(goes);
    dest.addEventListener('change', () => { goes.hidden = dest.value !== target.dest; });
  }
  const locked = version.paywall && version.access !== 'yes' && !variant.installed;
  const button = el('button', variant.installed ? 'secondary-button' : 'primary-button',
    variant.installed ? 'Installed' : locked ? (version.paywall.permanent ? 'Locked' : 'Download when free') : 'Download');
  button.type = 'button';
  if (variant.installed) { button.disabled = true; button.title = variant.installed; }
  if (locked && version.paywall.permanent) button.disabled = true;
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const result = await post('download', {source: detail.source, model_id: detail.id, version_id: version.id, variant: variant.key,
        dest: dest.value, schedule: !!locked});
      const scheduled = result.jobs?.[0]?.state === 'scheduled';
      button.textContent = scheduled ? 'Scheduled' : 'Queued';
      toast(scheduled ? `Scheduled: starts by itself on ${untilText(version.paywall.ends)}` : `Downloading ${variant.file_label || variant.files[0].name}`, 'ok');
      refreshState();
    } catch (error) {
      button.disabled = false;
      toast(error.message, 'error');
    }
  });
  actions.append(dest, button);
  if (!version.paywall || variant.installed) return actions;

  // Locked: buy it on Civitai (Buzz is spent there, never here), then check again, or wait.
  const box = el('div', 'paywall-box');
  const line = el('p', 'paywall-line');
  if (version.access === 'yes') {
    box.classList.add('is-unlocked');
    line.textContent = '🔓 Unlocked with your Civitai account';
    box.append(line, actions);
    return box;
  }
  line.textContent = version.paywall.permanent
    ? '🔒 Paid on Civitai: the creator charges Buzz for this version.'
    : `🔒 Early access on Civitai until ${untilText(version.paywall.ends)}. Unlock it now with Buzz, or download it automatically once it's free.`;
  box.append(line);
  if (version.access === 'no_key') box.append(el('p', 'hint', 'Add your Civitai API key (Manager → Settings) so a purchase on your account is recognised here.'));
  const links = el('div', 'paywall-actions');
  const buy = el('a', 'link-button', 'Unlock on Civitai ↗');
  buy.href = `${detail.url}?modelVersionId=${version.id}`;
  buy.target = '_blank';
  buy.rel = 'noopener noreferrer';
  const recheck = el('button', 'link-button', 'I bought it · check again');
  recheck.type = 'button';
  recheck.addEventListener('click', async () => {
    recheck.disabled = true;
    try {
      const {access} = await api(`civitai-access?${new URLSearchParams({version_id: version.id, source: detail.source})}`);
      if (access === 'yes') {
        version.access = 'yes';
        box.replaceWith(downloadControls(detail, version, variant, dirs, el('div')));
        toast('Unlocked: you can download it now', 'ok');
      } else {
        toast(access === 'no_key' ? 'Add your Civitai API key first (Manager → Settings)' : 'Still locked for this Civitai account', 'error');
      }
    } catch (error) { toast(error.message, 'error'); }
    recheck.disabled = false;
  });
  links.append(buy, recheck);
  box.append(actions, links);
  return box;
}

function variantRow(detail, version, variant, dirs) {
  const row = el('div', 'variant' + (variant.key === version.best ? ' is-best' : ''));
  const info = el('div', 'variant-info');
  const title = el('div', 'variant-title');
  if (variant.key === version.best) title.append(el('span', 'best', '★ Best for your hardware'));
  title.append(el('strong', '', variant.file_label || variant.files[0].name));
  info.append(title);
  const meta = el('div', 'variant-meta');
  meta.append(el('span', 'tag', variant.label));
  if (variant.quant?.label && !variant.label.startsWith(variant.quant.label)) meta.append(el('span', 'tag quant', variant.quant.label));
  if (variant.role) meta.append(el('span', 'tag', variant.role));
  meta.append(el('span', 'hint', size(variant.size + (variant.companions || []).reduce((n, f) => n + f.size, 0))));
  meta.append(fitBadge(variant.fit));
  info.append(meta);
  if (variant.fit?.note) info.append(el('p', 'fit-note', variant.fit.note));
  if (variant.companions?.length) info.append(el('p', 'hint', `+ ${variant.companions[0].name} (vision projector, ${size(variant.companions[0].size)})`));
  if (variant.whole_repo) info.append(el('p', 'hint', `${variant.files.length} files, saved as one folder`));

  const actions = downloadControls(detail, version, variant, dirs, info);
  row.append(info, actions);
  return row;
}

// ---- manager sub-tabs ---------------------------------------------------------------------

function setSub(sub) {
  state.sub = sub;
  document.querySelectorAll('#subtabs [data-sub]').forEach(tab => tab.setAttribute('aria-selected', String(tab.dataset.sub === sub)));
  document.querySelectorAll('[data-sub-panel]').forEach(panel => {
    panel.hidden = panel.dataset.subPanel !== sub || (panel.id === 'olds-card' && !state.hasOlds);
  });
  try { localStorage.setItem('models.sub', sub); } catch { /* private mode */ }
  if (sub === 'updates') { loadLibrary(); loadComfyUpdate(); loadComfyNodes(); }
  if (sub === 'installed') loadInstalled();
  if (sub === 'downloads') renderDownloads(state.downloads || []);
  if (sub === 'core') { loadCore(); loadRuntime(); loadComfySetup(); loadComfyNodes(); }
}
$('subtabs').addEventListener('click', event => {
  const tab = event.target.closest('[data-sub]');
  if (tab) setSub(tab.dataset.sub);
});
try { state.sub = localStorage.getItem('models.sub') || ''; } catch { /* private mode */ }

// Core Backend sections: one group of cards at a time (language models / ComfyUI / agent & apps).
function setCoreGroup(group) {
  if (!document.querySelector(`.core-group[data-group="${group}"]`)) group = 'llm';
  document.querySelectorAll('#core-nav [data-group]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.group === group)));
  document.querySelectorAll('.core-group').forEach(g => { g.hidden = g.dataset.group !== group; });
  try { localStorage.setItem('models.coreGroup', group); } catch { /* private mode */ }
}
$('core-nav').addEventListener('click', event => {
  const jump = event.target.closest('[data-jump]');
  if (jump) { $(jump.dataset.jump)?.scrollIntoView({block: 'start', behavior: 'smooth'}); return; }
  const button = event.target.closest('[data-group]');
  if (!button) return;
  setCoreGroup(button.dataset.group);
  // The rail is sticky, so check the layout itself: if we're scrolled into the old group, start the new one at its top.
  const layout = $('core-nav').closest('.core-layout');
  if (layout.getBoundingClientRect().top < 0) layout.scrollIntoView({block: 'start'});
});
try { setCoreGroup(localStorage.getItem('models.coreGroup') || 'llm'); } catch { setCoreGroup('llm'); }
// Highlight the jump link for the section currently at the top of the screen.
const coreSpy = new IntersectionObserver(entries => {
  entries.forEach(entry => {
    const link = document.querySelector(`#core-nav [data-jump="${entry.target.id}"]`);
    if (link) link.classList.toggle('is-here', entry.isIntersecting);
  });
}, {rootMargin: '-80px 0px -60% 0px'});
document.querySelectorAll('#core-nav [data-jump]').forEach(link => { const target = $(link.dataset.jump); if (target) coreSpy.observe(target); });

// ---- core backend: llama.cpp releases converted to RDNA Boost (built on the host) -----------

const CORE_DONE = ['ready', 'failed', 'cancelled'];
const CORE_PHASE = {queued: 'Queued', source: 'Preparing source', configure: 'Configuring', compile: 'Compiling',
  check: 'Checking', ready: 'Ready', failed: 'Failed', cancelled: 'Cancelled', unknown: 'Unknown'};
const CORE_PAGE = 60;
const NO_BOOST = 'Stock llama.cpp: built as released, WITHOUT RDNA Boost (no RDNA3 kernel tuning, adaptive MTP, direct lazy loading or MoE expert cache). RDNA Boost is not applied to it.';
let coreTimer;
let coreInitialSnapshot = false;
let comfyInitialSnapshot = false;
state.coreList = 'compatible';
state.coreShown = CORE_PAGE;
try { state.coreList = localStorage.getItem('models.coreList') || 'compatible'; } catch { /* private mode */ }

async function loadCore(fresh = false) {
  clearTimeout(coreTimer);
  try {
    // The page-load snapshot may stand in for the first load only, never for a reload after an action.
    if (coreInitialSnapshot && !fresh) coreInitialSnapshot = false;
    else { coreInitialSnapshot = false; [state.core, state.coreExtras, state.swap, state.engines, state.voiceEngines] = await Promise.all([api('core/llamacpp'), api('core/extras'), api('core/swap'), api('core/engines'), api('core/voice')]); }
    renderCore();
    renderEngines();
    renderBenchInstall();
    renderCoreExtras();
    renderVoiceEngines();
    renderSwap();
  } catch (error) {
    $('core-builds').replaceChildren(el('p', 'empty error', error.message));
    $('core-releases').replaceChildren();
    $('core-comfy').replaceChildren(el('p', 'empty error', error.message));
    $('core-flash').replaceChildren(el('p', 'empty error', error.message));
    $('core-hermes').replaceChildren(el('p', 'empty error', error.message));
    $('core-motion').replaceChildren(el('p', 'empty error', error.message));
    $('core-rea').replaceChildren(el('p', 'empty error', error.message));
    $('core-voice').replaceChildren(el('p', 'empty error', error.message));
    $('swap-builds').replaceChildren(el('p', 'empty error', error.message));
    $('swap-releases').replaceChildren();
    for (const id of ['core-rocmfpx', 'core-strata', 'core-hipblaslt', 'core-llama-bench']) $(id).replaceChildren(el('p', 'empty error', error.message));
  }
  const busy = (state.core && (state.core.building || state.core.fetch.running)) ||
    (state.coreExtras && (state.coreExtras.comfy.download_running || state.coreExtras.comfy.update_running ||
      state.coreExtras.flash.running || state.coreExtras.hermes.download_running || state.coreExtras.hermes.update_running ||
      state.coreExtras.personal_hermes?.update_running || state.coreExtras.motion?.install_running ||
      state.coreExtras.rea?.install_running || state.coreExtras.rea?.checking || state.coreExtras.ytdlp?.running)) ||
    (state.swap && (state.swap.building || state.swap.fetch.running)) ||
    (state.engines && (state.engines.strata.installing || state.engines.strata.ngram?.running || state.engines.rocmfpx.building || state.engines.hipblaslt.running)) ||
    state.voiceEngines?.engines?.some(engine => engine.running || engine.deepspeed?.running || engine.flash?.running);
  if (state.view === 'manager' && state.sub === 'core') coreTimer = setTimeout(loadCore, busy ? 3000 : 30000);
}

// A page refresh makes one local install scan. Only an absent, idle llama.cpp
// installation needs a remote version fetch; status polling never starts one.
let coreRefreshScanned = false;
async function scanCoreOnRefresh() {
  if (coreRefreshScanned) return;
  coreRefreshScanned = true;
  try {
    const [llama, extras, swap, engines, voiceEngines] = await Promise.all([api('core/llamacpp'), api('core/extras'), api('core/swap'), api('core/engines'), api('core/voice')]);
    state.core = llama;
    state.coreExtras = extras;
    updateHermesTab(extras);
    state.swap = swap;
    state.engines = engines;
    state.voiceEngines = voiceEngines;
    coreInitialSnapshot = true;
    comfyInitialSnapshot = true;
    if (state.view === 'manager' && state.sub === 'core') { coreInitialSnapshot = false; renderCore(); renderEngines(); renderBenchInstall(); renderCoreExtras(); renderVoiceEngines(); renderSwap(); }
    if (state.view === 'manager' && state.sub === 'updates') { renderComfyUpdate(extras); renderHermes(extras, 'hermes-updates'); renderRea(extras, 'rea-updates', true); renderSwapUpdate(); }
    const installed = llama.builds.some(build => build.phase === 'ready' && (build.exists ?? true));
    const inProgress = llama.building || llama.builds.some(build => !CORE_DONE.includes(build.phase));
    if (!installed && !inProgress && !llama.fetch.running) await post('core/llamacpp/fetch', {});
  } catch (error) {
    if (state.view === 'manager' && state.sub === 'core') $('core-task').textContent = `Backend scan failed: ${error.message}`;
  }
}

async function coreExtraAction(job) {
  if (job.startsWith('hermes-')) {
    const installingPersonal = job === 'hermes-download' && !state.coreExtras?.personal_hermes?.installed;
    const summary = installingPersonal ? 'Install clean Personal Hermes and a separate Rocsy Hermes + Rocsy Addons?' :
      `${job === 'hermes-update' ? 'Update' : 'Create'} Rocsy Hermes + Rocsy Addons?`;
    const impact = installingPersonal ? 'The personal terminal/Desktop install is created first. Rocsy gets its own source checkout and Docker image.' :
      'The existing Personal Hermes checkout is not changed.';
    if (!confirm(`${summary}\n\n${impact} The Rocsy agent and dashboard restart after a successful build; running sessions may be interrupted.`)) return;
  }
  if ((job === 'rea-install' || job === 'rea-update') && state.coreExtras?.rea?.installed &&
      !confirm(`${job === 'rea-update' ? 'Update REA and its tools' : 'Rebuild REA'}?\n\nThe image is rebuilt with fresh Debian tools, then the REA service restarts. It refuses to restart while an analysis (Ghidra, JADX, browser…) is running.`)) return;
  if (job === 'personal-hermes-update' && !confirm('Update Personal Hermes to the latest stable release?\n\nThis updates only your terminal/Desktop Hermes installation. Rocsy Hermes and its Docker services are not changed.')) return;
  try {
    const reply = await post(`core/extras/${job}`, {});
    toast(reply.message || 'Started', 'ok');
  } catch (error) { toast(error.message, 'error'); }
  if (state.view === 'manager' && state.sub === 'updates') loadComfyUpdate();
  if (state.view === 'manager' && state.sub === 'core') loadCore();
}

async function godotDownload(version) {
  if (!/^(3|4)\.\d+(?:\.\d+)?$/.test(version)) { toast('Enter a stable Godot 3 or 4 version, such as 4.2.2', 'error'); return; }
  try {
    const reply = await post(`core/extras/rea-godot-download/${version}`, {});
    toast(reply.message || 'Started', 'ok');
  } catch (error) { toast(error.message, 'error'); }
  loadCore();
}

async function reaEngineUninstall(name, version = '') {
  const cached = name.endsWith('-cache');
  const portable = {love2d: 'LÖVE 11.5', tic80: 'TIC-80 1.2.0',
    ...Object.fromEntries((state.coreExtras?.rea?.addons || []).map(addon => [addon.id, `${addon.label} ${addon.version}`]))};
  const label = portable[name] || (name.startsWith('godotsteam') ? 'GodotSteam 3.5.2' : `Godot ${version}`);
  const again = portable[name] ? '' : ' A later test run may download it again.';
  if (!confirm(`Uninstall ${label}${cached ? ' test cache' : ''}? Its downloaded runtime files will be removed.${again}`)) return;
  try {
    const path = portable[name] ? `rea-${name}-uninstall` : name.startsWith('godotsteam') ?
      `rea-godotsteam${cached ? '-cache' : ''}-uninstall` : `rea-godot${cached ? '-cache' : ''}-uninstall/${version}`;
    const reply = await post(`core/extras/${path}`, {});
    toast(reply.message || 'Uninstalled', 'ok');
  } catch (error) { toast(error.message, 'error'); }
  loadCore();
}

async function checkCoreUpdates() {
  const button = $('core-check-updates');
  button.disabled = true;
  button.textContent = 'Checking…';
  try {
    [state.coreExtras] = await Promise.all([post('core/extras/check', {}), checkComfyNodes(true)]);
    renderComfyUpdate(state.coreExtras);
    renderHermes(state.coreExtras, 'hermes-updates'); renderRea(state.coreExtras, 'rea-updates', true);
    if (state.view === 'manager' && state.sub === 'core') renderCoreExtras();
  } catch (error) { toast(error.message, 'error'); }
  finally {
    button.disabled = false;
    button.textContent = 'Check for updates';
  }
}
$('core-check-updates').addEventListener('click', checkCoreUpdates);

function coreExtraButton(label, job, disabled) {
  const button = el('button', 'secondary-button', label);
  button.type = 'button';
  button.disabled = disabled;
  button.addEventListener('click', () => coreExtraAction(job));
  return button;
}

// Install/update logs (ComfyUI, FlashAttention, Hermes, llama-swap builds): the rows
// re-render on every poll, which used to destroy an open log seconds after it opened.
// The open log lives in state.viewLogs and is re-attached after each render; a timer
// re-fetches it so a running job streams. (The llama.cpp build log has its own panel
// for the same reason.)
const VIEW_LOGS = {
  extras: {path: key => `core/extras/log/${key}`,
    running: key => Boolean(({
      'comfy-download': state.coreExtras?.comfy?.download_running,
      'comfy-update': state.coreExtras?.comfy?.update_running,
      'flash-install': state.coreExtras?.flash?.running,
      'hermes-download': state.coreExtras?.hermes?.download_running,
      'hermes-update': state.coreExtras?.hermes?.update_running,
      'motion-install': state.coreExtras?.motion?.install_running,
      'comfy-supervisor': state.coreExtras?.comfy?.supervisor_running,
      'trainer-install': state.coreExtras?.trainer?.install_running,
      'ytdlp-install': state.coreExtras?.ytdlp?.running,
      'rea-install': state.coreExtras?.rea?.install_running,
      'rea-update': state.coreExtras?.rea?.install_running,
      'rea-godot-download': state.coreExtras?.rea?.godot?.running,
      'rea-godotsteam-download': state.coreExtras?.rea?.godotsteam?.running,
      'rea-love2d-download': state.coreExtras?.rea?.portable?.find(engine => engine.id === 'love2d')?.running,
      'rea-tic80-download': state.coreExtras?.rea?.portable?.find(engine => engine.id === 'tic80')?.running,
      ...Object.fromEntries((state.coreExtras?.rea?.addons || []).map(addon => [`rea-${addon.id}-download`, addon.running])),
      'personal-hermes-update': state.coreExtras?.personal_hermes?.update_running})[key])},
  voice: {path: key => key === 'indextts2-deepspeed' ? 'core/voice/indextts2/deepspeed/log' :
      key === 'qwen3-flash' ? 'core/voice/qwen3/flash/log' : `core/voice/${key}/log`,
    running: key => key === 'indextts2-deepspeed' ? Boolean(state.voiceEngines?.engines?.find(engine => engine.id === 'indextts2')?.deepspeed?.running) :
      key === 'qwen3-flash' ? Boolean(state.voiceEngines?.engines?.find(engine => engine.id === 'qwen3')?.flash?.running) :
      Boolean(state.voiceEngines?.engines?.find(engine => engine.id === key)?.running)},
  nodes: {path: () => 'comfy-setup/log', running: () => Boolean(state.nodes?.job?.running || state.setup?.host?.job?.running)},
  swap: {path: key => `core/swap/log/${key}`,
    running: key => (state.swap?.builds || []).some(build => build.id === key && !['ready', 'failed', 'cancelled'].includes(build.phase))},
};
const viewLogTimers = {};

function viewLogState(kind) {
  return state.viewLogs?.[kind];
}

function toggleViewLog(kind, key) {
  const logs = state.viewLogs ||= {};
  if (logs[kind]?.key === key) { closeViewLog(kind); return; }
  logs[kind] = {key, text: '', stick: true, scroll: 0};
  rerenderManager();
  pollViewLog(kind);
}

function closeViewLog(kind) {
  clearTimeout(viewLogTimers[kind]);
  if (state.viewLogs) delete state.viewLogs[kind];
  rerenderManager();
}

// Re-render whichever Manager sub-view is up so an open log re-attaches with fresh text.
function rerenderManager() {
  if (state.view === 'manager' && state.sub === 'core' && state.setup) renderComfySetup();
  if (state.view === 'manager' && state.nodes) renderComfyNodes();
  if (state.view !== 'manager' || !state.coreExtras) return;
  if (state.sub === 'updates') { renderComfyUpdate(state.coreExtras); renderHermes(state.coreExtras, 'hermes-updates'); renderRea(state.coreExtras, 'rea-updates', true); renderSwapUpdate(); }
  if (state.sub === 'core') { renderCoreExtras(); renderVoiceEngines(); renderSwap(); renderComfySetup(); }
}

// The <pre> for a row's open log, or null when closed. It is re-created on every render,
// so it restores scroll: the bottom while stuck to it, the saved position otherwise.
function viewLogPre(kind, key) {
  const log = viewLogState(kind);
  if (!log || log.key !== key) return null;
  const pre = el('pre', 'core-log', log.text || 'Loading…');
  pre.addEventListener('scroll', () => {
    log.stick = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
    log.scroll = pre.scrollTop;
  });
  requestAnimationFrame(() => { pre.scrollTop = log.stick ? pre.scrollHeight : log.scroll || 0; });
  return pre;
}

async function pollViewLog(kind) {
  clearTimeout(viewLogTimers[kind]);
  const log = viewLogState(kind);
  if (!log) return;
  try {
    const text = (await api(VIEW_LOGS[kind].path(log.key))).log || '(empty)';
    if (viewLogState(kind) !== log) return;
    if (text !== log.text) { log.text = text; rerenderManager(); }
  } catch (error) {
    if (viewLogState(kind) !== log) return;
    log.text = error.message;
    rerenderManager();
  }
  if (viewLogState(kind) === log)
    viewLogTimers[kind] = setTimeout(() => pollViewLog(kind), VIEW_LOGS[kind].running(log.key) ? 2000 : 15000);
}

function coreExtraLog(job) {
  const button = el('button', 'link-button', viewLogState('extras')?.key === job ? 'Hide log' : 'Log');
  button.type = 'button';
  button.addEventListener('click', () => toggleViewLog('extras', job));
  return button;
}

function renderComfyUpdate(data) {
  const {comfy, flash, hermes} = data;
  const row = el('div', 'lib-row core-build');
  const info = el('div', 'core-info');
  info.append(el('strong', '', comfy.installed ? `ComfyUI · ${comfy.version || 'installed'}` : 'ComfyUI · not downloaded'),
    el('p', 'hint', comfy.download_running ? 'Downloading…' : comfy.update_running ? 'Updating…' :
      comfy.update_available === true ? 'Update available' : comfy.update_available === false ? 'Up to date' :
      comfy.update_error || 'Update status unavailable'));
  const actions = el('div', 'dl-actions');
  if (comfy.installed && comfy.update_available === true) actions.append(coreExtraButton('Update ComfyUI', 'comfy-update',
    comfy.download_running || comfy.update_running || flash.running || hermes.download_running || hermes.update_running ||
    data.personal_hermes?.update_running));
  const job = comfy.installed ? 'comfy-update' : 'comfy-download';
  actions.append(coreExtraLog(job));
  row.append(info, actions);
  $('comfy-updates').replaceChildren(...[row, viewLogPre('extras', job)].filter(Boolean));
}

function renderHermes(data, target) {
  const {hermes, comfy, flash, personal_hermes: personal} = data;
  const anyBusy = hermes.download_running || hermes.update_running || personal?.update_running ||
    comfy.download_running || comfy.update_running || flash.running || data.motion?.install_running || data.rea?.install_running;
  const row = el('div', 'lib-row core-build');
  const info = el('div', 'core-info');
  info.append(el('strong', '', hermes.installed ?
    `Rocsy Hermes · ${hermes.version || 'installed'}${hermes.addons_applied ? ' + Rocsy Addons' : ''}` :
    'Rocsy Hermes · not downloaded'));
  const detail = hermes.download_running || hermes.update_running ?
    `${hermes.phase || 'Working'}…` : hermes.phase === 'failed' ? `Failed: ${hermes.error || 'see log'}` :
    !hermes.installed ? personal?.installed ? 'Separate Rocsy version will be created from the personal install' :
      'Install creates separate Personal and Rocsy versions' : hermes.addons_error ? hermes.addons_error :
    !hermes.addons_applied ? 'Rocsy Addons patch not applied' : hermes.update_available === true ? 'Stable update available' :
    hermes.update_available === false ? 'Up to date on stable' : hermes.update_error || 'Update status unavailable';
  info.append(el('p', `hint${hermes.phase === 'failed' ? ' error' : ''}`, detail));
  const actions = el('div', 'dl-actions');
  const job = hermes.installed ? 'hermes-update' : 'hermes-download';
  const applyAddons = hermes.installed && !hermes.addons_applied && !hermes.addons_error;
  if (!hermes.installed || hermes.update_available === true || applyAddons) actions.append(coreExtraButton(
    !hermes.installed ? personal?.installed ? 'Create Rocsy Hermes + Addons' : 'Install Personal + Rocsy Hermes' : applyAddons && hermes.update_available !== true ?
      'Apply Rocsy Addons' : 'Update Hermes + Rocsy Addons', job,
    anyBusy));
  actions.append(coreExtraLog(job));
  row.append(info, actions);
  const rows = [row, viewLogPre('extras', job)];
  if (personal) {
    const personalRow = el('div', 'lib-row core-build');
    const personalInfo = el('div', 'core-info');
    personalInfo.append(el('strong', '', personal.installed ?
      `Personal Hermes · ${personal.version || 'installed'}` : 'Personal Hermes · not installed'));
    const personalDetail = personal.update_running ? `${personal.phase || 'Updating'}…` :
      personal.phase === 'failed' ? `Failed: ${personal.error || 'see log'}` :
      !personal.installed ? 'Terminal/Desktop installation is not present' :
      !personal.managed ? 'Detected outside the managed terminal checkout; Rocsy remains separate' :
      personal.update_available === true ? 'Stable update available' :
      personal.update_available === false ? 'Up to date on stable' :
      personal.update_error || 'Update status unavailable';
    personalInfo.append(el('p', `hint${personal.phase === 'failed' ? ' error' : ''}`, personalDetail));
    const personalActions = el('div', 'dl-actions');
    if (personal.managed && personal.update_available === true)
      personalActions.append(coreExtraButton('Update Personal Hermes', 'personal-hermes-update', anyBusy));
    personalActions.append(coreExtraLog('personal-hermes-update'));
    personalRow.append(personalInfo, personalActions);
    rows.push(personalRow, viewLogPre('extras', 'personal-hermes-update'));
  }
  $(target).replaceChildren(...rows.filter(Boolean));
}

let backendUpdatesTimer;
async function loadComfyUpdate() {
  clearTimeout(backendUpdatesTimer);
  try {
    if (comfyInitialSnapshot) comfyInitialSnapshot = false;
    else [state.coreExtras, state.swap] = await Promise.all([api('core/extras'), api('core/swap')]);
    updateHermesTab(state.coreExtras);
    renderComfyUpdate(state.coreExtras);
    renderHermes(state.coreExtras, 'hermes-updates'); renderRea(state.coreExtras, 'rea-updates', true);
    renderSwapUpdate();
    const hermes = state.coreExtras.hermes;
    if (state.view === 'manager' && state.sub === 'updates') {
      const busy = hermes.download_running || hermes.update_running || state.coreExtras.personal_hermes?.update_running ||
        ['queued', 'downloading', 'installing-personal', 'updating', 'patching', 'building', 'deploying'].includes(hermes.phase) ||
        state.swap?.building || state.swap?.fetch.running || state.coreExtras.rea?.install_running || state.coreExtras.rea?.checking;
      backendUpdatesTimer = setTimeout(loadComfyUpdate, busy ? 3000 : 30000);
    }
  } catch (error) {
    $('comfy-updates').replaceChildren(el('p', 'empty error', error.message));
    $('hermes-updates').replaceChildren(el('p', 'empty error', error.message));
    $('rea-updates').replaceChildren();
    $('swap-updates').replaceChildren(el('p', 'empty error', error.message));
    if (state.view === 'manager' && state.sub === 'updates')
      backendUpdatesTimer = setTimeout(loadComfyUpdate, 30000);
  }
}

// REA (Reverse Engineer Anything): service + prerequisites in bunny-rea (hostctl/coreextras.py).
// compact = the one-line row in Manager → Updates; the Core Backend card also lists every component.
function renderRea(data, target, compact) {
  const rea = data.rea;
  const box = $(target);
  if (!rea || !box) return;
  const others = data.comfy.download_running || data.comfy.update_running || data.flash.running ||
    data.hermes.download_running || data.hermes.update_running || data.personal_hermes?.update_running || data.motion?.install_running;
  const updates = rea.tools.filter(tool => tool.latest && tool.latest !== tool.pinned);
  if (compact && !rea.installed && !rea.install_running) { box.replaceChildren(); return; }
  const row = el('div', 'lib-row core-build');
  const info = el('div', 'core-info');
  const status = rea.install_running ? `${rea.phase || 'Installing'}…` :
    rea.phase === 'failed' ? `Failed: ${rea.error || 'see log'}` :
    !rea.installed ? 'Not installed' :
    !rea.running ? 'Installed · service stopped' :
    rea.healthy ? 'Running · MCP on 127.0.0.1:8975' : `Running · service ${rea.health || 'starting'}`;
  const update = rea.checking && rea.update_available == null ? 'Checking for updates…' :
    rea.update_available ? `Update available: ${updates.map(tool => `${tool.label} ${tool.latest}`).join(', ')}` :
    rea.update_available === false ? 'Up to date' : rea.update_error ? `Update check failed: ${rea.update_error}` : '';
  info.append(el('strong', '', `REA${rea.version ? ` · ${rea.version}` : ''}`),
    el('p', `hint${rea.phase === 'failed' ? ' error' : ''}`, [status, update].filter(Boolean).join(' · ')));
  if (!compact) {
    if (rea.note) info.append(el('p', 'hint', rea.note));
    else if (rea.installed && !rea.hermes_registered) info.append(el('p', 'hint error', 'Not registered in Hermes yet: run Rebuild REA while Hermes is running.'));
    const list = el('ul', 'rea-tools');
    for (const tool of rea.tools) {
      const item = el('li', tool.latest && tool.latest !== tool.pinned ? 'has-update' : '');
      const shown = tool.installed || tool.pinned || '—';
      item.append(el('strong', '', tool.label),
        el('span', '', tool.latest && tool.latest !== tool.pinned ? `${shown} → ${tool.latest}` : shown));
      list.append(item);
    }
    info.append(list);
    const apt = Object.entries(rea.apt || {});
    if (apt.length) {
      const details = el('details', 'rea-apt');
      details.append(el('summary', '', `System tools from Debian (refreshed on every rebuild${rea.built ? `, last ${new Date(rea.built).toLocaleDateString()}` : ''})`));
      const aptList = el('ul', 'rea-tools');
      for (const [name, version] of apt) { const item = el('li'); item.append(el('strong', '', name), el('span', '', version)); aptList.append(item); }
      details.append(aptList);
      info.append(details);
    }
  }
  const actions = el('div', 'dl-actions');
  const busy = rea.install_running || others;
  if (rea.update_available) actions.append(coreExtraButton(rea.installed ? 'Update REA + tools' : 'Download & install REA (latest)', 'rea-update', busy));
  if (!compact && !rea.update_available) actions.append(coreExtraButton(rea.installed ? 'Rebuild REA' : 'Download & install REA', 'rea-install', busy));
  const job = rea.update_available ? 'rea-update' : 'rea-install';
  actions.append(coreExtraLog(job));
  row.append(info, actions);
  box.replaceChildren(...[row, viewLogPre('extras', job)].filter(Boolean));
  if (!compact) {
    const engines = $('core-rea-engines');
    const steam = rea.godotsteam;
    if (engines && steam) {
      const rows = [];
      const godot = rea.godot;
      const engineBusy = busy || steam.running || godot?.running || rea.portable?.some(engine => engine.running) ||
        rea.addons?.some(addon => addon.running);
      if (godot) {
        const row = el('div', 'lib-row core-build');
        const info = el('div', 'core-info');
        const installed = godot.installed?.length ? `Installed: ${godot.installed.join(', ')}` : 'No official Godot runtimes installed';
        info.append(el('strong', '', 'Godot · official Linux runtimes'),
          el('p', `hint${godot.phase === 'failed' ? ' error' : ''}`,
            godot.running ? `Downloading Godot ${godot.version} · ${godot.phase || 'starting'}…` : godot.error || installed),
          el('p', 'hint', 'Enter the exact stable version used by a game. Test runs may download missing runtimes again.'));
        const actions = el('div', 'dl-actions');
        const version = el('input');
        version.type = 'text'; version.inputMode = 'decimal'; version.placeholder = 'e.g. 4.2.2';
        version.setAttribute('aria-label', 'Godot stable version');
        version.value = state.reaGodotVersion ?? godot.version ?? '';
        version.addEventListener('input', () => { state.reaGodotVersion = version.value; });
        const download = el('button', 'secondary-button', 'Download & install');
        download.type = 'button'; download.disabled = engineBusy;
        download.addEventListener('click', () => godotDownload(version.value.trim()));
        version.addEventListener('keydown', event => { if (event.key === 'Enter') download.click(); });
        actions.append(version, download, coreExtraLog('rea-godot-download'));
        row.append(info, actions);
        rows.push(row, viewLogPre('extras', 'rea-godot-download'));
        for (const installedVersion of godot.installed || []) {
          const installedRow = el('div', 'lib-row core-build');
          const installedInfo = el('div', 'core-info');
          installedInfo.append(el('strong', '', `Godot ${installedVersion} · Linux`),
            el('p', 'hint', 'Installed for REA test runs'));
          const installedActions = el('div', 'dl-actions');
          const remove = el('button', 'secondary-button', 'Uninstall');
          remove.type = 'button'; remove.disabled = engineBusy;
          remove.addEventListener('click', () => reaEngineUninstall('godot', installedVersion));
          installedActions.append(remove);
          installedRow.append(installedInfo, installedActions);
          rows.push(installedRow);
        }
        for (const cachedVersion of godot.cached || []) {
          const cachedRow = el('div', 'lib-row core-build');
          const cachedInfo = el('div', 'core-info');
          cachedInfo.append(el('strong', '', `Godot ${cachedVersion} · REA test cache`),
            el('p', 'hint', 'Downloaded by an earlier test run'));
          const cachedActions = el('div', 'dl-actions');
          const remove = el('button', 'secondary-button', 'Uninstall');
          remove.type = 'button'; remove.disabled = engineBusy;
          remove.addEventListener('click', () => reaEngineUninstall('godot-cache', cachedVersion));
          cachedActions.append(remove);
          cachedRow.append(cachedInfo, cachedActions);
          rows.push(cachedRow);
        }
      }
      const engineRow = el('div', 'lib-row core-build');
      const engineInfo = el('div', 'core-info');
      engineInfo.append(el('strong', '', `GodotSteam ${steam.version} · Linux`),
        el('p', `hint${steam.phase === 'failed' ? ' error' : ''}`,
          steam.running ? `${steam.phase || 'Downloading'}…` : steam.installed ? 'Installed for REA test runs' :
            steam.error || 'Needed for games built on GodotSteam 3.5.2.'));
      const engineActions = el('div', 'dl-actions');
      engineActions.append(coreExtraButton(steam.installed ? 'Download again' : 'Download GodotSteam',
        'rea-godotsteam-download', engineBusy));
      if (steam.installed) {
        const remove = el('button', 'secondary-button', 'Uninstall');
        remove.type = 'button'; remove.disabled = engineBusy;
        remove.addEventListener('click', () => reaEngineUninstall('godotsteam'));
        engineActions.append(remove);
      }
      engineActions.append(coreExtraLog('rea-godotsteam-download'));
      engineRow.append(engineInfo, engineActions);
      rows.push(engineRow, viewLogPre('extras', 'rea-godotsteam-download'));
      if (steam.cached) {
        const cachedRow = el('div', 'lib-row core-build');
        const cachedInfo = el('div', 'core-info');
        cachedInfo.append(el('strong', '', `GodotSteam ${steam.version} · REA test cache`),
          el('p', 'hint', 'Downloaded by an earlier test run'));
        const cachedActions = el('div', 'dl-actions');
        const remove = el('button', 'secondary-button', 'Uninstall');
        remove.type = 'button'; remove.disabled = engineBusy;
        remove.addEventListener('click', () => reaEngineUninstall('godotsteam-cache'));
        cachedActions.append(remove);
        cachedRow.append(cachedInfo, cachedActions);
        rows.push(cachedRow);
      }
      for (const portable of rea.portable || []) {
        const portableRow = el('div', 'lib-row core-build');
        const portableInfo = el('div', 'core-info');
        const use = portable.id === 'love2d' ? 'Lua games and .love packages' : 'TIC-80 cartridges';
        portableInfo.append(el('strong', '', `${portable.label} ${portable.version} · Linux x64 · ${portable.size_mb} MB download`),
          el('p', `hint${portable.phase === 'failed' ? ' error' : ''}`,
            portable.running ? `${portable.phase || 'Downloading'}…` : portable.error ||
              (portable.installed ? 'Installed for manual REA runs' : `Optional runtime for ${use}`)));
        if (portable.installed) portableInfo.append(el('p', 'hint', `Inside REA: ${portable.path}`));
        const portableActions = el('div', 'dl-actions');
        portableActions.append(coreExtraButton(portable.installed ? 'Download again' : 'Download & install',
          `rea-${portable.id}-download`, engineBusy));
        if (portable.installed) {
          const remove = el('button', 'secondary-button', 'Uninstall');
          remove.type = 'button'; remove.disabled = engineBusy;
          remove.addEventListener('click', () => reaEngineUninstall(portable.id));
          portableActions.append(remove);
        }
        portableActions.append(coreExtraLog(`rea-${portable.id}-download`));
        portableRow.append(portableInfo, portableActions);
        rows.push(portableRow, viewLogPre('extras', `rea-${portable.id}-download`));
      }
      engines.replaceChildren(...rows.filter(Boolean));
    }
    const addonBox = $('core-rea-addons');
    if (addonBox && rea.addons) {
      const engineBusy = busy || steam?.running || rea.godot?.running || rea.portable?.some(engine => engine.running) ||
        rea.addons.some(addon => addon.running);
      const rows = [];
      for (const addon of rea.addons) {
        const row = el('div', 'lib-row core-build');
        const info = el('div', 'core-info');
        info.append(el('strong', '', `${addon.label} ${addon.version} · Linux x64 · ${addon.size_mb} MB download`),
          el('p', `hint${addon.phase === 'failed' ? ' error' : ''}`,
            addon.running ? `${addon.phase || 'Downloading'}…` : addon.error ||
              (addon.installed ? `Installed · ${addon.use}` : `${addon.use} (${addon.repo})`)));
        if (addon.installed) info.append(el('p', 'hint', `Inside REA: ${addon.path}`));
        const actions = el('div', 'dl-actions');
        actions.append(coreExtraButton(addon.installed ? 'Download again' : 'Download & install',
          `rea-${addon.id}-download`, engineBusy));
        if (addon.installed) {
          const remove = el('button', 'secondary-button', 'Uninstall');
          remove.type = 'button'; remove.disabled = engineBusy;
          remove.addEventListener('click', () => reaEngineUninstall(addon.id));
          actions.append(remove);
        }
        actions.append(coreExtraLog(`rea-${addon.id}-download`));
        row.append(info, actions);
        rows.push(row, viewLogPre('extras', `rea-${addon.id}-download`));
      }
      addonBox.replaceChildren(...(rows.length ? rows.filter(Boolean) : [el('p', 'empty', 'No tool addons available')]));
    }
  }
}

function renderCoreYtdlp() {
  const y = state.coreExtras?.ytdlp;
  if (!y || !$('core-ytdlp')) return;
  const row = el('div', 'lib-row core-build');
  const info = el('div', 'core-info');
  const detail = y.running ? `${y.phase === 'queued' ? 'Queued' : 'Downloading'}…` :
    y.phase === 'failed' ? `Failed: ${y.error || 'see log'}` :
    !y.installed ? `Not installed${y.latest ? ` · latest ${y.latest}` : ''}` :
    y.update_available ? `Update available: ${y.latest}` :
    y.update_error ? `Installed · couldn't check for updates (${y.update_error})` : 'Installed · up to date';
  info.append(el('strong', '', `yt-dlp${y.version ? ` · ${y.version}` : ''}`),
    el('p', `hint${y.phase === 'failed' ? ' error' : ''}`, detail));
  const actions = el('div', 'dl-actions');
  actions.append(coreExtraButton(!y.installed ? 'Download yt-dlp' : y.update_available ? `Update to ${y.latest}` : 'Reinstall latest',
    'ytdlp-install', y.running));
  actions.append(coreExtraLog('ytdlp-install'));
  row.append(info, actions);
  $('core-ytdlp').replaceChildren(...[row, viewLogPre('extras', 'ytdlp-install')].filter(Boolean));
}

function renderCoreExtras() {
  renderCoreYtdlp();
  const {comfy, flash, hermes, motion} = state.coreExtras;
  if (motion) {
    const row = el('div', 'lib-row core-build');
    const info = el('div', 'core-info');
    const detail = motion.install_running ? `${motion.phase || 'Installing'}…` :
      motion.phase === 'failed' ? `Failed: ${motion.error || 'see log'}` :
      motion.running ? motion.healthy ? 'Installed · service healthy' :
        `Installed · service ${motion.health || 'starting'}` :
      motion.installed ? 'Installed · service stopped' : 'Not installed';
    info.append(el('strong', '', `Remotion${motion.version ? ` · ${motion.version}` : ''}`),
      el('p', `hint${motion.phase === 'failed' ? ' error' : ''}`, detail));
    const actions = el('div', 'dl-actions');
    actions.append(coreExtraButton(motion.installed ? 'Rebuild & start Remotion' : 'Download & install Remotion',
      'motion-install', motion.install_running || state.coreExtras.rea?.install_running || comfy.download_running || comfy.update_running || flash.running ||
        hermes.download_running || hermes.update_running || state.coreExtras.personal_hermes?.update_running));
    actions.append(coreExtraLog('motion-install'));
    row.append(info, actions);
    $('core-motion').replaceChildren(...[row, viewLogPre('extras', 'motion-install')].filter(Boolean));
  }
  const trainer = state.coreExtras.trainer;
  if (trainer) {
    const row = el('div', 'lib-row core-build');
    const info = el('div', 'core-info');
    info.append(el('strong', '', 'Trainer'), el('p', 'hint', trainer.install_running ? 'Building and starting…' :
      trainer.running ? 'Installed · running at /Trainer' : trainer.installed ? 'Installed · stopped (start it in Settings → Services)' : 'Not installed'));
    const actions = el('div', 'dl-actions');
    actions.append(coreExtraButton(trainer.installed ? 'Rebuild & start Trainer' : 'Install Trainer', 'trainer-install',
      trainer.install_running || motion?.install_running || state.coreExtras.rea?.install_running || comfy.download_running || comfy.update_running ||
        flash.running || hermes.download_running || hermes.update_running));
    actions.append(coreExtraLog('trainer-install'));
    row.append(info, actions);
    $('core-trainer').replaceChildren(...[row, viewLogPre('extras', 'trainer-install')].filter(Boolean));
  }
  const comfyRow = el('div', 'lib-row core-build');
  const comfyInfo = el('div', 'core-info');
  // installed = a git checkout; ready = it also has the .venv the supervisor launches.
  const comfySetup = !comfy.ready;
  comfyInfo.append(el('strong', '', comfy.installed ? `ComfyUI · ${comfy.version || 'installed'}` : 'ComfyUI · not downloaded'),
    el('p', 'hint', comfy.download_running ? 'Downloading and setting up…' : comfy.update_running ? 'Updating…' :
      comfy.installed && comfySetup ? `${comfy.path.replace(HOME_RE, '~/')} · no Python environment yet: finish the setup to use it` :
      comfy.path.replace(HOME_RE, '~/')));
  const comfyActions = el('div', 'dl-actions');
  const comfyJob = comfySetup ? 'comfy-download' : 'comfy-update';
  if (comfySetup || comfy.update_available === true) comfyActions.append(coreExtraButton(
    !comfy.installed ? 'Download ComfyUI' : comfySetup ? 'Finish setup' : 'Update ComfyUI', comfyJob,
    comfy.download_running || comfy.update_running || flash.running ||
      hermes.download_running || hermes.update_running || state.coreExtras.personal_hermes?.update_running || motion?.install_running || state.coreExtras.rea?.install_running));
  comfyActions.append(coreExtraLog(comfyJob));
  comfyRow.append(comfyInfo, comfyActions);
  // The supervisor (comfyui-supervisor user unit) that starts ComfyUI when a studio needs it.
  const sup = comfy.supervisor || {state: 'none'};
  const supRow = el('div', 'lib-row core-build');
  const supInfo = el('div', 'core-info');
  supInfo.append(el('strong', '', 'ComfyUI supervisor'), el('p', 'hint', comfy.supervisor_running ? 'Setting up…' :
    sup.state === 'ours' ? (sup.active ? 'Running · the studios start ComfyUI through it' : 'Set up, but not running: start it in Settings → Services') :
    sup.state === 'other' ? `Set up outside this Rocsy (${(sup.script || 'another ComfyUI').replace(HOME_RE, '~/')}); left as it is` :
    comfy.ready ? 'Not set up: the studios can’t start ComfyUI on their own' : 'Set up together with ComfyUI'));
  const supActions = el('div', 'dl-actions');
  if (sup.state === 'none' && comfy.ready) supActions.append(coreExtraButton('Set up supervisor', 'comfy-supervisor',
    comfy.download_running || comfy.update_running || comfy.supervisor_running || flash.running));
  supActions.append(coreExtraLog('comfy-supervisor'));
  supRow.append(supInfo, supActions);
  $('core-comfy').replaceChildren(...[comfyRow, viewLogPre('extras', comfyJob), supRow, viewLogPre('extras', 'comfy-supervisor')].filter(Boolean));

  const flashRow = el('div', 'lib-row core-build');
  const flashInfo = el('div', 'core-info');
  const archs = flash.current_archs.join(', ');
  flashInfo.append(el('strong', '', `FlashAttention · ${archs || 'GPU not detected'}`),
    el('p', 'hint', flash.running ? `Building for ${flash.built_archs.join(', ')}…` :
      flash.rebuild_needed ? `GPU architecture changed. Built for ${flash.built_archs.join(', ')}; rebuild for ${archs}.` :
      flash.installed ? `Installed for ${flash.built_archs.join(', ') || 'an unknown GPU architecture'}` : flash.error || 'Not installed'));
  const flashActions = el('div', 'dl-actions');
  flashActions.append(coreExtraButton(flash.rebuild_needed ? `Rebuild for ${archs}` : `Download & build for ${archs || 'GPU'}`,
    'flash-install', !archs || flash.running || comfy.download_running || comfy.update_running ||
      hermes.download_running || hermes.update_running || state.coreExtras.personal_hermes?.update_running || motion?.install_running || state.coreExtras.rea?.install_running));
  flashActions.append(coreExtraLog('flash-install'));
  flashRow.append(flashInfo, flashActions);
  $('core-flash').replaceChildren(...[flashRow, viewLogPre('extras', 'flash-install')].filter(Boolean));
  renderHermes(state.coreExtras, 'core-hermes');
  renderRea(state.coreExtras, 'core-rea', false);
}

function renderVoiceEngines() {
  const container = $('core-voice');
  if (!container || !state.voiceEngines) return;
  const engines = state.voiceEngines.engines || [];
  const busy = engines.some(engine => engine.running || engine.deepspeed?.running || engine.flash?.running);
  const rows = [];
  for (const engine of engines) {
    const row = el('div', 'lib-row core-build');
    const info = el('div', 'core-info');
    const detail = engine.running ? `${engine.phase || 'Installing'}…` :
      engine.phase === 'failed' ? `Failed: ${engine.error || 'see log'}` :
      engine.installed ? 'Installed · backend and model ready' :
      [engine.backend ? 'Backend ready' : 'Backend missing', engine.weights ? 'model ready' : 'model missing'].join(' · ');
    info.append(el('strong', '', engine.name),
      el('p', `hint${engine.phase === 'failed' ? ' error' : ''}`, detail),
      el('p', 'hint', `${engine.repo} · ${engine.path.replace(HOME_RE, '~/')}`));
    if (engine.creates_voices) info.append(el('p', 'hint', 'Voice creation: adds “Create a voice” to Voice → Add character'));
    const actions = el('div', 'dl-actions');
    if (!engine.installed) {
      const button = el('button', 'secondary-button', 'Download & install');
      button.type = 'button'; button.disabled = busy;
      button.addEventListener('click', async () => {
        button.disabled = true;
        try { const reply = await post(`core/voice/${engine.id}/install`, {}); toast(reply.message || 'Started', 'ok'); }
        catch (error) { toast(error.message, 'error'); }
        loadCore(true);
      });
      actions.append(button);
    }
    const log = el('button', 'link-button', viewLogState('voice')?.key === engine.id ? 'Hide log' : 'Log');
    log.type = 'button'; log.addEventListener('click', () => toggleViewLog('voice', engine.id));
    actions.append(log);
    row.append(info, actions);
    rows.push(row, ...[viewLogPre('voice', engine.id)].filter(Boolean));
    if (engine.id === 'qwen3' && engine.flash) {
      const fa = engine.flash;
      const addon = el('div', 'lib-row core-build');
      const addonInfo = el('div', 'core-info');
      const faDetail = fa.running ? `${fa.phase || 'Building'}…` :
        fa.phase === 'failed' ? `Failed: ${fa.error || 'see log'}` :
        fa.installed ? `Installed · built for torch ${fa.torch || '?'} · pick it in Voice → Settings → Attention` :
        'Optional · ROCm GPU build for the Voice container, compiled on the CPU (30–90 min)';
      addonInfo.append(el('strong', '', 'Qwen3-TTS · FlashAttention 2'),
        el('p', `hint${fa.phase === 'failed' ? ' error' : ''}`, faDetail));
      const addonActions = el('div', 'dl-actions');
      const build = el('button', 'secondary-button', fa.installed ? 'Rebuild' : 'Build & install FlashAttention');
      build.type = 'button'; build.disabled = busy || !engine.installed;
      build.addEventListener('click', async () => {
        build.disabled = true;
        try { const reply = await post('core/voice/qwen3/flash', {}); toast(reply.message || 'Started', 'ok'); }
        catch (error) { toast(error.message, 'error'); }
        loadCore(true);
      });
      const faLog = el('button', 'link-button', viewLogState('voice')?.key === 'qwen3-flash' ? 'Hide log' : 'Log');
      faLog.type = 'button'; faLog.addEventListener('click', () => toggleViewLog('voice', 'qwen3-flash'));
      addonActions.append(build, faLog);
      addon.append(addonInfo, addonActions);
      rows.push(addon, ...[viewLogPre('voice', 'qwen3-flash')].filter(Boolean));
    }
    if (engine.id === 'indextts2' && engine.deepspeed) {
      const ds = engine.deepspeed;
      const addon = el('div', 'lib-row core-build');
      const addonInfo = el('div', 'core-info');
      const dsDetail = ds.running ? `${ds.phase || 'Installing'}…` :
        ds.phase === 'failed' ? `Failed: ${ds.error || 'see log'}` :
        ds.installed ? `Installed · version ${ds.version}` : 'Optional acceleration · not installed';
      addonInfo.append(el('strong', '', `IndexTTS2 · DeepSpeed ${ds.required_version}`),
        el('p', `hint${ds.phase === 'failed' ? ' error' : ''}`, dsDetail));
      const addonActions = el('div', 'dl-actions');
      if (!ds.installed) {
        const install = el('button', 'secondary-button', 'Download & install DeepSpeed');
        install.type = 'button'; install.disabled = busy || !engine.installed;
        install.addEventListener('click', async () => {
          install.disabled = true;
          try { const reply = await post('core/voice/indextts2/deepspeed', {}); toast(reply.message || 'Started', 'ok'); }
          catch (error) { toast(error.message, 'error'); }
          loadCore(true);
        });
        addonActions.append(install);
      }
      const dsLog = el('button', 'link-button', viewLogState('voice')?.key === 'indextts2-deepspeed' ? 'Hide log' : 'Log');
      dsLog.type = 'button'; dsLog.addEventListener('click', () => toggleViewLog('voice', 'indextts2-deepspeed'));
      addonActions.append(dsLog);
      addon.append(addonInfo, addonActions);
      rows.push(addon, ...[viewLogPre('voice', 'indextts2-deepspeed')].filter(Boolean));
    }
  }
  container.replaceChildren(...rows);
}

// ---- Core Backend → ComfyUI models & nodes (comfysetup.py; node installs run on the host) ----
// Recommended defaults for empty model folders, files saved workflows load but don't have,
// and the custom node packs behind node types ComfyUI doesn't know. Nothing installs by itself.

let setupTimer;
state.setupPick = new Set();

async function loadComfySetup(fresh = false) {
  clearTimeout(setupTimer);
  const button = $('setup-scan');
  if (fresh) { button.disabled = true; button.textContent = 'Scanning…'; }
  try {
    const first = !state.setup;
    state.setup = await api('comfy-setup' + (fresh ? '?fresh=1' : ''));
    if (first || fresh) {
      // Preselect only the small recommended defaults; workflow files (often tens of GB) and node packs are opt-in.
      state.setupPick = new Set(state.setup.defaults.filter(item => !item.gated).map(item => item.key));
    }
    renderComfySetup();
  } catch (error) {
    $('comfy-setup').replaceChildren(el('p', 'empty error', error.message));
  } finally {
    button.disabled = false;
    button.textContent = 'Scan again';
  }
  // While a node install runs, poll; when it ends, rescan once so the lists reflect it.
  const running = Boolean(state.setup?.host?.job?.running);
  const finished = state.setupRunning && !running;
  state.setupRunning = running;
  if (state.view === 'manager' && state.sub === 'core')
    setupTimer = setTimeout(() => loadComfySetup(finished), finished ? 0 : running ? 3000 : 60000);
}
$('setup-scan').addEventListener('click', () => loadComfySetup(true));

function setupCheckbox(key, disabled = false) {
  const box = el('input');
  box.type = 'checkbox';
  box.checked = !disabled && state.setupPick.has(key);
  box.disabled = disabled;
  box.setAttribute('aria-label', 'Select');
  box.addEventListener('change', () => {
    if (box.checked) state.setupPick.add(key); else state.setupPick.delete(key);
    renderComfySetup();
  });
  return box;
}

function setupWorkflows(list) {
  const names = list.map(path => path.split('/').pop().replace(/\.json$/i, ''));
  const text = names.slice(0, 3).join(', ') + (names.length > 3 ? ` +${names.length - 3} more` : '');
  const line = el('p', 'hint', `Used by ${text}`);
  line.title = list.join('\n');
  return line;
}

function setupSearch(text) {
  setView('models');
  $('q').value = text;
  search(true);
}

function setupModelRow(item) {
  const row = el('div', 'lib-row setup-row');
  const queued = (state.downloads || []).some(job => job.group === `comfy-setup:${item.key}` &&
    !['failed', 'cancelled', 'complete'].includes(job.state));
  const pick = el('label', 'setup-pick');
  pick.append(setupCheckbox(item.key, !item.url || queued));
  const info = el('div', 'core-info');
  info.append(el('strong', '', item.title || item.name));
  if (queued) info.append(el('span', 'tag installed', 'In Downloads'));
  const meta = el('div', 'setup-meta');
  if (item.title) meta.append(el('span', 'hint', item.name));
  if (item.dest) meta.append(el('span', 'tag', `models/${item.dest}`));
  if (item.size) meta.append(el('span', 'hint', size(item.size)));
  if (item.gated) meta.append(el('span', 'tag warn', 'Gated: needs a Hugging Face token with access (Settings)'));
  if (item.dead) meta.append(el('span', 'tag warn', 'Listed link is gone'));
  if (!item.url && !item.dead) meta.append(el('span', 'tag', 'No download link known'));
  info.append(meta);
  if (item.workflows) info.append(setupWorkflows(item.workflows));
  row.append(pick, info);
  if (!item.url) {
    const find = el('button', 'secondary-button', 'Search in Models');
    find.type = 'button';
    find.addEventListener('click', () => setupSearch(item.search || item.name));
    row.append(find);
  }
  return row;
}

function setupNodeRow(item, kind) {
  const row = el('div', 'lib-row setup-row');
  const info = el('div', 'core-info');
  const title = el('a', 'setup-link', item.title || item.type);
  if (item.url) { title.href = item.url; title.target = '_blank'; title.rel = 'noopener noreferrer'; }
  info.append(el('strong', '')); info.firstChild.append(title);
  if (item.author) info.firstChild.append(el('span', 'hint', ` by ${item.author}`));
  if (item.description) info.append(el('p', 'hint', item.description));
  if (item.types) info.append(el('p', 'hint setup-types', `Nodes: ${item.types.join(', ')}`));
  if (kind === 'broken') info.append(el('p', 'hint', 'Installed, but ComfyUI doesn’t load these nodes: the pack may need an update, or it failed to import (see ComfyUI’s log).'));
  if (kind === 'unknown') info.append(el('p', 'hint', item.pack ? `From “${item.pack}”, which ComfyUI-Manager's list doesn't have.` : 'No known pack provides this node.'));
  info.append(setupWorkflows(item.workflows));
  if (kind === 'missing') {
    const pick = el('label', 'setup-pick');
    pick.append(setupCheckbox('node:' + item.key, Boolean(state.setup.host?.job?.running)));
    row.append(pick);
  }
  row.append(info);
  return row;
}

function setupGroup(rows, sideRows, sideLabel) {
  const out = [...rows];
  if (sideRows.length) {
    const more = el('details', 'setup-side');
    more.append(el('summary', '', `${sideLabel} (${sideRows.length})`), ...sideRows);
    out.push(more);
  }
  return out;
}

function renderComfySetup() {
  const data = state.setup;
  if (!data || state.view !== 'manager' || state.sub !== 'core') return;
  const host = data.host || {};
  const job = host.job || {};
  const parts = [];

  if (host.error) parts.push(el('p', 'task', `Host helper: ${host.error}. Node installs need it.`));
  if (job.kind === 'update') {
    // Updates are shown (with the restart button and log) in the Custom nodes card below.
    if (job.running || job.restart_needed) parts.push(el('p', 'task', job.running ? 'Updating node packs… see Custom nodes below.' : 'Restart ComfyUI from Custom nodes below to load the updated packs.'));
  } else if (job.running || job.phase === 'queued' || job.phase === 'installing') {
    const done = (job.repos || []).filter(repo => repo.state !== 'queued' && repo.state !== 'installing').length;
    parts.push(el('p', 'task', `Installing node packs… ${done}/${(job.repos || []).length} done (git clone, then their Python requirements with ComfyUI's ROCm torch pinned).`));
  } else if (job.phase && job.finished && Date.now() / 1000 - job.finished < 86400) {
    const failed = (job.repos || []).filter(repo => repo.state === 'failed');
    if (failed.length) parts.push(el('p', 'task error', `Failed: ${failed.map(repo => `${repo.dir} (${repo.error})`).join('; ')}`));
  }
  if (job.restart_needed && job.kind !== 'update') {
    const bar = el('div', 'task setup-restart');
    const restart = el('button', 'primary-button', 'Restart ComfyUI');
    restart.type = 'button';
    restart.addEventListener('click', async () => {
      if (!confirm('Restart ComfyUI now so it loads the new node packs?\n\nIt is refused while anything is rendering or queued.')) return;
      restart.disabled = true;
      try { toast((await post('comfy-setup/restart', {})).message || 'Restarting', 'ok'); setTimeout(() => loadComfySetup(true), 45000); }
      catch (error) { toast(error.message, 'error'); restart.disabled = false; }
    });
    bar.append(el('span', '', 'New node packs load after ComfyUI restarts.'), restart);
    parts.push(bar);
  }
  if (job.phase && job.kind !== 'update') {
    const logRow = el('div', 'setup-logrow');
    const toggle = el('button', 'link-button', viewLogState('nodes') ? 'Hide install log' : 'Node install log');
    toggle.type = 'button';
    toggle.addEventListener('click', () => toggleViewLog('nodes', 'nodes'));
    logRow.append(toggle);
    parts.push(logRow);
    const pre = viewLogPre('nodes', 'nodes');
    if (pre) parts.push(pre);
  }

  // Model files: recommended defaults + workflow files.
  const inQueue = new Set((state.downloads || []).filter(job => !['failed', 'cancelled', 'complete'].includes(job.state)).map(job => job.group));
  const fileKeys = [...data.defaults, ...data.models].filter(item => item.url && state.setupPick.has(item.key) && !inQueue.has(`comfy-setup:${item.key}`));
  const fileTotal = fileKeys.reduce((n, item) => n + (item.size || 0), 0);
  const modelHead = el('div', 'engine-heading');
  modelHead.append(el('div', ''));
  modelHead.firstChild.append(el('h3', '', 'Model files'),
    el('p', '', data.defaults.length || data.models.length ? 'Downloads go through the normal queue (Downloads tab): resumable and SHA-256 checked where the host publishes one.' :
      'Nothing missing: every model folder has files and every saved workflow finds its models.'));
  const download = el('button', 'primary-button', fileKeys.length ? `Download ${fileKeys.length}${fileTotal ? ` · ${size(fileTotal)}` : ''}` : 'Download');
  download.type = 'button';
  download.disabled = !fileKeys.length;
  download.addEventListener('click', async () => {
    if (fileTotal > 20 * GB && !confirm(`Download ${fileKeys.length} files (${size(fileTotal)})?`)) return;
    download.disabled = true;
    try {
      const reply = await post('comfy-setup/download', {keys: fileKeys.map(item => item.key)});
      toast(`${reply.jobs.length} download${reply.jobs.length === 1 ? '' : 's'} queued` + (reply.errors.length ? ` · ${reply.errors.join('; ')}` : ''), reply.errors.length ? 'error' : 'ok');
      fileKeys.forEach(item => state.setupPick.delete(item.key));
      renderComfySetup();
    } catch (error) { toast(error.message, 'error'); download.disabled = false; }
  });
  modelHead.append(download);
  const modelSection = el('section', 'engine-subsection');
  modelSection.append(modelHead);
  if (data.defaults.length) {
    modelSection.append(el('h4', '', 'Recommended defaults for empty folders'));
    modelSection.append(...data.defaults.map(setupModelRow));
  }
  if (data.models.length) {
    modelSection.append(el('h4', '', 'Used by your saved workflows'));
    modelSection.append(...setupGroup(data.models.filter(i => !i.side).map(setupModelRow),
      data.models.filter(i => i.side).map(setupModelRow), 'Only in imported / archived workflows'));
  }
  parts.push(modelSection);

  // Custom node packs.
  const nodeSection = el('section', 'engine-subsection');
  const picks = data.nodes.filter(item => state.setupPick.has('node:' + item.key));
  const nodeHead = el('div', 'engine-heading');
  nodeHead.append(el('div', ''));
  nodeHead.firstChild.append(el('h3', '', 'Custom nodes'),
    el('p', '', data.nodes_error || (data.nodes.length || data.broken_nodes.length || data.unknown_nodes.length
      ? 'Installed into ComfyUI/custom_nodes with their Python requirements. ComfyUI’s ROCm torch, triton and numpy are pinned, so a pack that wants different versions fails instead of replacing them.'
      : 'Nothing missing: ComfyUI has every node your saved workflows use.')));
  const install = el('button', 'primary-button', picks.length ? `Install ${picks.length} pack${picks.length === 1 ? '' : 's'}` : 'Install');
  install.type = 'button';
  install.disabled = !picks.length || Boolean(job.running) || Boolean(host.error);
  install.addEventListener('click', async () => {
    const unlisted = picks.filter(item => !item.listed);
    if (!confirm(`Install ${picks.map(item => item.title).join(', ')}?\n\nCustom nodes run their own Python code inside ComfyUI.` +
      (unlisted.length ? `\n\nNot in ComfyUI-Manager's reviewed list: ${unlisted.map(item => item.key).join(', ')}.` : '') +
      '\n\nComfyUI keeps running; restart it afterwards to load them.')) return;
    install.disabled = true;
    try {
      toast((await post('comfy-setup/nodes', {keys: picks.map(item => item.key)})).message || 'Installing', 'ok');
      picks.forEach(item => state.setupPick.delete('node:' + item.key));
    } catch (error) { toast(error.message, 'error'); }
    loadComfySetup();
  });
  nodeHead.append(install);
  nodeSection.append(nodeHead);
  if (data.nodes.length) {
    nodeSection.append(el('h4', '', 'Missing packs'));
    nodeSection.append(...setupGroup(data.nodes.filter(i => !i.side).map(i => setupNodeRow(i, 'missing')),
      data.nodes.filter(i => i.side).map(i => setupNodeRow(i, 'missing')), 'Only in imported / archived workflows'));
  }
  if (data.broken_nodes.length) {
    nodeSection.append(el('h4', '', 'Installed but not loading'));
    nodeSection.append(...setupGroup(data.broken_nodes.filter(i => !i.side).map(i => setupNodeRow(i, 'broken')),
      data.broken_nodes.filter(i => i.side).map(i => setupNodeRow(i, 'broken')), 'Only in imported / archived workflows'));
  }
  if (data.unknown_nodes.length) {
    const unknown = el('details', 'setup-side');
    unknown.append(el('summary', '', `Nodes no known pack provides (${data.unknown_nodes.length})`),
      ...data.unknown_nodes.map(i => setupNodeRow(i, 'unknown')));
    nodeSection.append(unknown);
  }
  parts.push(nodeSection);
  parts.push(el('p', 'hint', `Checked ${data.workflows} saved workflow${data.workflows === 1 ? '' : 's'} ${ago(data.scanned_at)}` +
    (data.manager_list ? '' : ' · ComfyUI-Manager’s lists couldn’t be fetched, so fewer files and packs can be matched')));
  $('comfy-setup').replaceChildren(...parts);
}

// ---- Core Backend → ComfyUI → Custom nodes (hostctl/comfynodes.py via /api/comfy-nodes) ----

let nodesTimer;
state.nodeSearch = {q: '', results: [], total: 0, busy: false};
const NODE_STATUS = {
  behind: ['Update available', 'installed'],
  current: ['Up to date', ''],
  ahead: ['Newer than the remote', ''],
  modified: ['Local edits: not updated', 'warn'],
  diverged: ['Own commits: not updated', 'warn'],
  detached: ['Pinned to a commit', ''],
  local: ['Local pack (no git remote)', ''],
  error: ['Check failed', 'warn'],
};

async function loadComfyNodes() {
  clearTimeout(nodesTimer);
  try {
    const wasRunning = Boolean(state.nodes?.job?.running);
    state.nodes = await api('comfy-nodes');
    renderComfyNodes();
    // An install/update just ended: the missing-nodes scan above may have changed too.
    if (wasRunning && !state.nodes.job.running && state.sub === 'core') loadComfySetup(true);
  } catch (error) {
    if ($('comfy-nodes')) $('comfy-nodes').replaceChildren(el('p', 'empty error', error.message));
  }
  if (state.view === 'manager' && ['core', 'updates'].includes(state.sub))
    nodesTimer = setTimeout(loadComfyNodes, state.nodes?.job?.running ? 3000 : 60000);
}

async function checkComfyNodes(quiet = false) {
  const button = $('nodes-check');
  button.disabled = true;
  button.textContent = 'Checking…';
  try {
    const reply = await post('comfy-nodes/check', {});
    if (!quiet) toast(reply.available ? `${reply.available} node pack update${reply.available === 1 ? '' : 's'} available` : 'All node packs are up to date', 'ok');
    await loadComfyNodes();
  } catch (error) { if (!quiet) toast(error.message, 'error'); }
  finally { button.disabled = false; button.textContent = 'Check for updates'; }
}
$('nodes-check').addEventListener('click', () => checkComfyNodes());

async function updateNodePacks(dirs) {
  if (!confirm(`Update ${dirs.length === 1 ? dirs[0] : `${dirs.length} node packs`}?\n\nEach pack moves to the newest commit of its branch and reinstalls its Python requirements with ComfyUI’s ROCm torch pinned. A pack whose requirements fail goes back to the commit it was on.\n\nComfyUI keeps running; restart it afterwards to load them.`)) return;
  try { toast((await post('comfy-nodes/update', {dirs})).message || 'Updating', 'ok'); }
  catch (error) { toast(error.message, 'error'); }
  loadComfyNodes();
}

async function installNodePacks(repos, label) {
  if (!confirm(`Install ${label}?\n\nCustom nodes run their own Python code inside ComfyUI. Its Python requirements are installed with ComfyUI’s ROCm torch pinned.\n\nComfyUI keeps running; restart it afterwards to load it.`)) return false;
  try { toast((await post('comfy-nodes/install', {repos})).message || 'Installing', 'ok'); }
  catch (error) { toast(error.message, 'error'); return false; }
  loadComfyNodes();
  return true;
}

async function searchNodeCatalog(q) {
  const search = state.nodeSearch;
  search.q = q;
  if (!q.trim()) { search.results = []; search.total = 0; renderComfyNodes(); return; }
  search.busy = true;
  renderNodeResults();
  try {
    const reply = await api('comfy-nodes/catalog?q=' + encodeURIComponent(q));
    if (search.q !== q) return;  // a newer search is on its way
    search.results = reply.results; search.total = reply.total;
  } catch (error) { toast(error.message, 'error'); }
  finally { search.busy = false; renderNodeResults(); }
}

function nodeJobBar(job) {
  const parts = [];
  const repos = job.repos || [];
  const verb = job.kind === 'update' ? 'Updating' : 'Installing';
  if (job.running || job.phase === 'queued' || job.phase === 'installing') {
    const done = repos.filter(repo => !['queued', 'installing', 'updating'].includes(repo.state)).length;
    parts.push(el('p', 'task', `${verb} node packs… ${done}/${repos.length} done.`));
  } else if (job.phase && job.finished && Date.now() / 1000 - job.finished < 86400) {
    const failed = repos.filter(repo => repo.state === 'failed');
    if (failed.length) parts.push(el('p', 'task error', `Failed: ${failed.map(repo => `${repo.dir} (${repo.error})`).join('; ')}`));
  }
  if (job.restart_needed) {
    const bar = el('div', 'task setup-restart');
    const restart = el('button', 'primary-button', 'Restart ComfyUI');
    restart.type = 'button';
    restart.addEventListener('click', async () => {
      if (!confirm('Restart ComfyUI now so it loads the new and updated node packs?\n\nIt is refused while anything is rendering or queued.')) return;
      restart.disabled = true;
      try { toast((await post('comfy-setup/restart', {})).message || 'Restarting', 'ok'); setTimeout(() => { loadComfyNodes(); loadComfySetup(true); }, 45000); }
      catch (error) { toast(error.message, 'error'); restart.disabled = false; }
    });
    bar.append(el('span', '', 'New and updated node packs load after ComfyUI restarts.'), restart);
    parts.push(bar);
  }
  if (job.phase) {
    const logRow = el('div', 'setup-logrow');
    const toggle = el('button', 'link-button', viewLogState('nodes') ? 'Hide log' : 'Node install / update log');
    toggle.type = 'button';
    toggle.addEventListener('click', () => toggleViewLog('nodes', 'nodes'));
    logRow.append(toggle);
    parts.push(logRow);
    const pre = viewLogPre('nodes', 'nodes');
    if (pre) parts.push(pre);
  }
  return parts;
}

function nodeResultRow(item, busy) {
  const row = el('div', 'lib-row setup-row');
  const info = el('div', 'core-info');
  const title = el('a', 'setup-link', item.title);
  title.href = item.url; title.target = '_blank'; title.rel = 'noopener noreferrer';
  info.append(el('strong', '')); info.firstChild.append(title, el('span', 'hint', ` by ${item.author}`));
  if (item.description) info.append(el('p', 'hint', item.description));
  row.append(info);
  if (item.installed) row.append(el('span', 'tag installed', 'Installed'));
  else {
    const install = el('button', 'secondary-button', 'Install');
    install.type = 'button';
    install.disabled = busy;
    install.addEventListener('click', async () => { install.disabled = true; if (!await installNodePacks([item.url], item.title)) install.disabled = false; });
    row.append(install);
  }
  return row;
}

function renderNodeResults() {
  const box = $('node-results');
  if (!box) return;
  const search = state.nodeSearch;
  const busy = Boolean(state.nodes?.job?.running) || Boolean(state.nodes?.error);
  if (search.busy) { box.replaceChildren(el('p', 'hint', 'Searching…')); return; }
  if (!search.q.trim()) { box.replaceChildren(); return; }
  if (!search.results.length) { box.replaceChildren(el('p', 'hint', 'No pack in ComfyUI-Manager’s list matches. Paste its GitHub link below instead.')); return; }
  box.replaceChildren(...search.results.map(item => nodeResultRow(item, busy)),
    ...(search.total > search.results.length ? [el('p', 'hint', `Showing ${search.results.length} of ${search.total}; type more to narrow it down.`)] : []));
}

function nodePackRow(pack, busy) {
  const row = el('div', 'lib-row setup-row');
  const info = el('div', 'core-info');
  const title = el('a', 'setup-link', pack.title || pack.dir);
  if (/^https?:\/\//.test(pack.remote || '')) { title.href = pack.remote.replace(/\.git$/, ''); title.target = '_blank'; title.rel = 'noopener noreferrer'; }
  info.append(el('strong', '')); info.firstChild.append(title);
  if (pack.title && pack.title !== pack.dir) info.firstChild.append(el('span', 'hint', ` · ${pack.dir}`));
  const meta = el('div', 'setup-meta');
  const [label, tone] = NODE_STATUS[pack.status] || ['Not checked yet', ''];
  meta.append(el('span', `tag ${tone}`.trim(), label));
  if (pack.disabled) meta.append(el('span', 'tag', 'Disabled'));
  if (pack.commit) meta.append(el('span', 'hint', pack.status === 'behind' && pack.latest ? `${pack.commit.slice(0, 7)} → ${pack.latest.slice(0, 7)}` : pack.commit.slice(0, 7)));
  if (pack.branch) meta.append(el('span', 'hint', pack.branch));
  if (pack.committed) meta.append(el('span', 'hint', `installed commit from ${ago(pack.committed)}`));
  info.append(meta);
  if (pack.status === 'modified') info.append(el('p', 'hint', `${pack.modified_files || 'Some'} tracked file${pack.modified_files === 1 ? ' is' : 's are'} changed in this checkout (a local patch?). Commit or undo the change to update it.`));
  if (pack.status === 'error' && pack.error) info.append(el('p', 'hint error', pack.error));
  row.append(info);
  if (pack.status === 'behind') {
    const update = el('button', 'secondary-button', 'Update');
    update.type = 'button';
    update.disabled = busy;
    update.addEventListener('click', () => updateNodePacks([pack.dir]));
    row.append(update);
  }
  return row;
}

function renderComfyNodes() {
  const data = state.nodes;
  if (!data || state.view !== 'manager') return;
  renderNodesUpdate(data);
  if (state.sub !== 'core') return;
  const job = data.job || {};
  const busy = Boolean(job.running) || Boolean(data.error);
  const parts = [];
  if (data.error) parts.push(el('p', 'task', `Host helper: ${data.error}. Installing and updating nodes needs it.`));
  parts.push(...nodeJobBar(job));

  // Install: ComfyUI-Manager's list, or any GitHub link.
  const add = el('section', 'engine-subsection');
  const addHead = el('div', 'engine-heading');
  addHead.append(el('div', ''));
  addHead.firstChild.append(el('h3', '', 'Install a node pack'),
    el('p', '', 'Search ComfyUI-Manager’s list of reviewed packs, or paste a github.com link to install one that isn’t listed.'));
  add.append(addHead);
  const searchRow = el('div', 'nodes-input-row');
  const query = el('input');
  query.type = 'search'; query.id = 'node-search'; query.placeholder = 'Search packs, e.g. video helper, upscale, gguf…';
  query.setAttribute('aria-label', 'Search node packs');
  query.value = state.nodeSearch.q;
  let typing;
  query.addEventListener('input', () => { clearTimeout(typing); typing = setTimeout(() => searchNodeCatalog(query.value), 300); });
  searchRow.append(query);
  add.append(searchRow);
  const results = el('div', 'node-results');
  results.id = 'node-results';
  add.append(results);
  const urlRow = el('div', 'nodes-input-row');
  const url = el('input');
  url.type = 'url'; url.placeholder = 'https://github.com/owner/ComfyUI-Pack';
  url.setAttribute('aria-label', 'GitHub link of a node pack');
  const installUrl = el('button', 'secondary-button', 'Install from GitHub');
  installUrl.type = 'button';
  installUrl.disabled = busy;
  installUrl.addEventListener('click', async () => {
    const link = url.value.trim();
    if (!/^https:\/\/github\.com\/[\w.-]+\/[\w.-]+?(\.git)?\/?$/.test(link)) { toast('Paste a github.com repository link, e.g. https://github.com/owner/ComfyUI-Pack', 'error'); return; }
    if (await installNodePacks([link], `${link.replace(/^https:\/\/github\.com\//, '').replace(/\.git$|\/$/g, '')}\n\nThis pack is not checked against ComfyUI-Manager’s reviewed list.`)) url.value = '';
  });
  urlRow.append(url, installUrl);
  add.append(urlRow);
  parts.push(add);

  // Installed packs and their update status.
  const packs = [...(data.packs || [])];
  const order = {behind: 0, modified: 1, diverged: 2, error: 3};
  packs.sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9) || (a.title || a.dir).localeCompare(b.title || b.dir));
  const behind = packs.filter(pack => pack.status === 'behind');
  const installed = el('section', 'engine-subsection');
  const head = el('div', 'engine-heading');
  head.append(el('div', ''));
  head.firstChild.append(el('h3', '', `Installed packs (${packs.length})`),
    el('p', '', data.checked_at ? `${behind.length ? `${behind.length} update${behind.length === 1 ? '' : 's'} available` : 'Everything that can be updated is up to date'} · checked ${ago(data.checked_at)}` :
      'Not checked for updates yet.'));
  const updateAll = el('button', 'primary-button', behind.length ? `Update all (${behind.length})` : 'Update all');
  updateAll.type = 'button';
  updateAll.disabled = !behind.length || busy;
  updateAll.addEventListener('click', () => updateNodePacks(behind.map(pack => pack.dir)));
  head.append(updateAll);
  installed.append(head);
  const main = packs.filter(pack => !['local', 'current', 'ahead', 'detached'].includes(pack.status) || !pack.status);
  const rest = packs.filter(pack => !main.includes(pack));
  installed.append(...setupGroup(main.map(pack => nodePackRow(pack, busy)), rest.map(pack => nodePackRow(pack, busy)), 'Up to date, pinned and local packs'));
  parts.push(installed);

  const focused = document.activeElement?.id === 'node-search';
  $('comfy-nodes').replaceChildren(...parts);
  renderNodeResults();
  if (focused) { const box = $('node-search'); box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
}

// The one-line row in Manager → Updates.
function renderNodesUpdate(data) {
  const target = $('nodes-updates');
  if (!target || state.sub !== 'updates') return;
  const behind = (data.packs || []).filter(pack => pack.status === 'behind');
  const row = el('div', 'lib-row core-build');
  const info = el('div', 'core-info');
  info.append(el('strong', '', `ComfyUI custom nodes · ${(data.packs || []).length} installed`),
    el('p', 'hint', data.job?.running ? `${data.job.kind === 'update' ? 'Updating' : 'Installing'}…` :
      !data.checked_at ? 'Not checked yet' : behind.length ? `${behind.length} update${behind.length === 1 ? '' : 's'} available: ${behind.slice(0, 4).map(pack => pack.title || pack.dir).join(', ')}${behind.length > 4 ? '…' : ''}` :
      `Up to date · checked ${ago(data.checked_at)}`));
  const actions = el('div', 'dl-actions');
  if (behind.length) {
    const update = el('button', 'secondary-button', `Update ${behind.length}`);
    update.type = 'button';
    update.disabled = Boolean(data.job?.running) || Boolean(data.error);
    update.addEventListener('click', () => updateNodePacks(behind.map(pack => pack.dir)));
    actions.append(update);
  }
  const open = el('button', 'link-button', 'Details');
  open.type = 'button';
  open.addEventListener('click', () => { setSub('core'); setCoreGroup('comfy'); setTimeout(() => $('comfy-nodes-card')?.scrollIntoView({block: 'start', behavior: 'smooth'}), 300); });
  actions.append(open);
  row.append(info, actions);
  target.replaceChildren(row);
}

async function swapAction(action, body = {}) {
  try {
    const reply = await post(`core/swap/${action}`, body);
    toast(reply.message || 'Started', 'ok');
  } catch (error) { toast(error.message, 'error'); }
  if (state.view === 'manager' && state.sub === 'core') loadCore(true);
  else if (state.view === 'manager' && state.sub === 'updates') loadComfyUpdate();
}

function renderSwapUpdate() {
  if (!state.swap) return;
  const row = el('div', 'lib-row core-build');
  const latest = state.swap.releases.find(item => item.compatible);
  const active = state.swap.builds.find(item => item.active);
  const info = el('div', 'core-info');
  info.append(el('strong', '', `llama-swap · ${active?.tag || 'unknown'} ${active?.builtin ? '(built-in)' : '+ Rocsy Addons'}`),
    el('p', 'hint', latest ? `Latest patch-compatible release: ${latest.tag}` : state.swap.fetch.running ? 'Checking releases…' : 'No compatible release checked yet'));
  const actions = el('div', 'dl-actions');
  if (latest && !state.swap.building && !state.swap.builds.some(build => build.tag === latest.tag && build.patch === state.swap.patch)) {
    const download = el('button', 'primary-button', `Download ${latest.tag} + Rocsy Addons`);
    download.type = 'button';
    download.addEventListener('click', () => swapAction('build', {tag: latest.tag}));
    actions.append(download);
  }
  const button = el('button', 'secondary-button', 'Browse versions');
  button.type = 'button';
  button.addEventListener('click', () => setSub('core'));
  actions.append(button);
  row.append(info, actions);
  $('swap-updates').replaceChildren(row);
}

let swapShowAll = false;
function renderSwap() {
  const data = state.swap;
  if (!data) return;
  $('swap-fetch').disabled = data.fetch.running;
  $('swap-task').hidden = !data.fetch.running && !data.fetch.error;
  $('swap-task').textContent = data.fetch.running ? 'Checking releases and Rocsy Addons patch compatibility…' : `Version check failed: ${data.fetch.error}`;
  const buildRows = data.builds.flatMap(build => {
    const row = el('div', `lib-row core-build${build.active ? ' is-active' : ''}`);
    const info = el('div', 'core-info');
    const head = el('div', 'dl-head');
    head.append(el('strong', '', `${build.tag} · ${build.builtin ? 'built-in upstream + gateway addons' : 'Rocsy Addons patched build'}`));
    if (build.active) head.append(el('span', 'pill pill-ok', 'In use'));
    info.append(head, el('p', `hint${build.phase === 'failed' ? ' error' : ''}`,
      build.phase === 'failed' ? build.error : build.builtin ? 'Built-in fallback' :
      [build.phase, build.size && size(build.size), build.finished && `built ${ago(build.finished)}`].filter(Boolean).join(' · ')));
    if (build.refs?.length) info.append(el('p', 'hint core-files', `Used by saved settings: ${build.refs.join(', ')}`));
    const actions = el('div', 'dl-actions');
    if (build.phase === 'ready' && !build.active && (build.exists ?? true)) {
      const button = el('button', 'secondary-button', 'Use this version');
      button.type = 'button';
      button.disabled = data.building;
      button.addEventListener('click', () => {
        if (confirm(`Switch llama-swap to ${build.tag}${build.builtin ? ' built-in' : ' + Rocsy Addons'}?\n\nThis restarts llama-swap and requires all models to be unloaded. Model files and Parameters settings stay in place.`)) swapAction('activate', {id: build.id});
      });
      actions.append(button);
    }
    if (!['ready', 'failed', 'cancelled'].includes(build.phase) && !build.builtin) {
      const button = el('button', 'link-button', 'Cancel');
      button.type = 'button';
      button.addEventListener('click', () => swapAction('cancel'));
      actions.append(button);
    }
    if (!build.builtin) {
      const button = el('button', 'link-button', viewLogState('swap')?.key === build.id ? 'Hide log' : 'Log');
      button.type = 'button';
      button.addEventListener('click', () => toggleViewLog('swap', build.id));
      actions.append(button);
    }
    if (!build.builtin && !build.active && ['ready', 'failed', 'cancelled'].includes(build.phase)) {
      const button = el('button', 'link-button danger', 'Delete');
      button.type = 'button';
      button.addEventListener('click', () => {
        if (confirm(`Delete ${build.tag} + Rocsy Addons?\n\nOnly this version's build folder is removed:\n${build.path?.replace(HOME_RE, '~/') || build.id}\n\nModel files and Parameters settings stay in place. Deletion is refused if the build contains a model, is running or mounted, or saved settings point to it.`)) swapAction('delete', {id: build.id});
      });
      actions.append(button);
    }
    row.append(info, actions);
    return [row, viewLogPre('swap', build.id)].filter(Boolean);
  });
  $('swap-builds').replaceChildren(...buildRows);
  const have = new Set(data.builds.filter(build => !build.builtin).map(build => `${build.tag}|${build.patch}`));
  const featured = new Set(data.releases.slice(0, 5).map(release => release.tag));
  const latestCompatible = data.releases.find(release => release.compatible);
  if (latestCompatible) featured.add(latestCompatible.tag);
  const visibleReleases = swapShowAll ? data.releases : data.releases.filter(release => featured.has(release.tag));
  const releaseRows = visibleReleases.map(release => {
    const row = el('div', 'lib-row core-release');
    const info = el('div', 'core-info');
    info.append(el('strong', '', `llama-swap ${release.tag}`),
      el('p', `hint${release.compatible ? '' : ' error'}`, release.compatible ? 'Rocsy Addons patch applies cleanly' : release.reason || 'Patch conflict'));
    const actions = el('div', 'dl-actions');
    if (release.compatible) {
      const built = have.has(`${release.tag}|${data.patch}`);
      const button = el('button', built ? 'link-button' : 'primary-button', built ? 'Already built' : 'Download & patch');
      button.type = 'button';
      button.disabled = built || data.building;
      button.addEventListener('click', () => swapAction('build', {tag: release.tag}));
      actions.append(button);
    }
    row.append(info, actions);
    return row;
  });
  const releaseList = releaseRows.length ? releaseRows : [el('p', 'empty', data.fetch.running ? 'Checking releases…' : 'No releases checked yet.')];
  if (data.releases.length > featured.size) {
    const more = el('button', 'secondary-button', swapShowAll ? 'Show fewer versions' : `Show all ${data.releases.length} versions`);
    more.type = 'button';
    more.addEventListener('click', () => { swapShowAll = !swapShowAll; renderSwap(); });
    releaseList.push(more);
  }
  $('swap-releases').replaceChildren(...releaseList);
}

$('swap-fetch').addEventListener('click', () => swapAction('fetch'));

async function engineAction(action, body = {}) {
  try {
    const reply = await post(`core/engines/${action}`, body);
    toast(reply.message || 'Started', 'ok');
  } catch (error) { toast(error.message, 'error'); }
  loadCore(true);
}
function renderEngines() {
  const data = state.engines;
  if (!data) return;
  const fpx = data.rocmfpx;
  const fpxRow = el('div', 'lib-row core-build');
  const fpxInfo = el('div', 'core-info');
  fpxInfo.append(el('strong', '', `ROCmFPX · ${fpx.arch || 'AMD GPU not detected'}`),
    el('p', 'hint', fpx.job?.phase === 'failed' ? fpx.job.error :
      fpx.job?.phase === 'ready' ? `Build ${fpx.job.id} is ready. Select it in llama.cpp above.` :
      fpx.building ? `Building · ${fpx.job?.phase || 'starting'}` : 'Download and build the ROCmFPX llama.cpp fork for this card.'));
  const fpxActions = el('div', 'dl-actions');
  const build = el('button', 'primary-button', 'Download & build ROCmFPX');
  build.type = 'button'; build.disabled = !fpx.arch || fpx.building;
  build.addEventListener('click', () => engineAction('rocmfpx-build'));
  fpxActions.append(build);
  if (fpx.building) {
    const cancel = el('button', 'link-button', 'Cancel'); cancel.type = 'button';
    cancel.addEventListener('click', () => engineAction('rocmfpx-cancel')); fpxActions.append(cancel);
  }
  fpxRow.append(fpxInfo, fpxActions);
  $('core-rocmfpx').replaceChildren(fpxRow);

  const strata = data.strata;
  const row = el('div', 'lib-row core-build');
  const info = el('div', 'core-info');
  info.append(el('strong', '', 'Strata · Qwen3.8 Flash Next'),
    el('p', 'hint', strata.job?.phase === 'failed' ? strata.job.error :
      strata.running ? 'Running · OpenAI API and Strata UI available' :
      strata.installing ? `Installing · ${strata.job?.phase || 'starting'}` :
      strata.installed ? `Installed: ${strata.models.join(', ')}` : 'Download source, engine and model'));
  const controls = el('div', 'dl-actions');
  const models = el('select');
  for (const choice of ['IQ2_XS', 'Q2_0', 'IQ3_XXS', 'IQ3_S']) {
    const option = el('option', '', choice); option.value = choice; models.append(option);
  }
  models.setAttribute('aria-label', 'Strata quantization');
  const family = el('select');
  for (const choice of ['qwen', 'swift', 'coder']) {
    const option = el('option', '', choice === 'qwen' ? 'Qwen3.8 Flash Next' : choice === 'swift' ? 'Swift 1.5' : 'Coder');
    option.value = choice; family.append(option);
  }
  family.setAttribute('aria-label', 'Strata model family');
  const install = el('button', 'primary-button', 'Download & install');
  install.type = 'button'; install.disabled = strata.installing || strata.running;
  install.addEventListener('click', () => engineAction('strata-install', {family: family.value, model: models.value, context: 32768, gguf_dir: custom.value.trim()}));
  const custom = el('input'); custom.type = 'text'; custom.placeholder = 'Optional custom GGUF directory';
  custom.setAttribute('aria-label', 'Custom GGUF directory inside managed LLM folders');
  controls.append(family, models, custom, install);
  if (strata.installed) {
    const toggle = el('button', 'secondary-button', strata.running ? 'Stop' : 'Start');
    toggle.type = 'button';
    toggle.addEventListener('click', () => engineAction(strata.running ? 'strata-stop' : 'strata-start'));
    controls.append(toggle);
  }
  if (strata.running) {
    const open = el('a', 'secondary-button', 'Open Strata UI');
    open.href = '/Strata/'; open.target = '_blank'; open.rel = 'noopener'; controls.append(open);
  }
  const logButton = el('button', 'link-button', 'Install log');
  logButton.type = 'button';
  logButton.addEventListener('click', async () => {
    let pre = info.querySelector('pre.core-log');
    if (pre) { pre.remove(); return; }
    pre = el('pre', 'core-log', 'Loading…'); info.append(pre);
    try { pre.textContent = (await api('core/engines/log')).log || '(empty)'; }
    catch (error) { pre.textContent = error.message; }
  });
  controls.append(logButton);
  row.append(info, controls);
  const ngram = strata.ngram;
  const ngramRow = el('div', 'lib-row core-build');
  if (ngram && strata.installed) {
    const job = ngram.job || {};
    const gb = (bytes) => (bytes / 1e9).toFixed(1);
    const ngramInfo = el('div', 'core-info');
    ngramInfo.append(el('strong', '', 'Full-precision n-gram table · BF16'),
      el('p', 'hint', ngram.present ? `Downloaded${job.row_error != null ? ` · checked against the model's table (median row difference ${(job.row_error * 100).toFixed(1)}%)` : ''}. Turn it on per model in llama-swap → Parameters → N-gram table.` :
        ngram.running ? `${job.phase === 'checking' ? 'Checking' : 'Downloading'} · ${gb(job.done || ngram.partial)} of ${gb(ngram.bytes)} GB` :
        job.phase === 'failed' ? `Download failed: ${job.error}` :
        `Qwen's own n-gram table instead of the models' 4-bit copy: better quality at the same speed, read from the SSD. ${gb(ngram.bytes)} GB of disk${ngram.partial ? `, ${gb(ngram.partial)} GB already here` : ''}.`));
    const ngramActions = el('div', 'dl-actions');
    if (!ngram.present && !ngram.running) {
      const get = el('button', 'secondary-button', ngram.partial ? 'Resume download' : 'Download (102 GB)');
      get.type = 'button';
      get.addEventListener('click', () => engineAction('strata-ngram'));
      ngramActions.append(get);
    }
    if (ngram.running) {
      const stop = el('button', 'link-button', 'Pause'); stop.type = 'button';
      stop.addEventListener('click', () => engineAction('strata-ngram-cancel')); ngramActions.append(stop);
    }
    ngramRow.append(ngramInfo, ngramActions);
  }
  $('core-strata').replaceChildren(...(ngramRow.childElementCount ? [row, ngramRow] : [row]));

  const tune = data.hipblaslt;
  const tuneRow = el('div', 'lib-row core-build');
  const tuneInfo = el('div', 'core-info');
  tuneInfo.append(el('strong', '', `${tune.arch || 'Unknown GPU'} · hipBLASLt ${tune.version || 'unknown'}`),
    el('p', 'hint', tune.running ? `Calibrating · ${tune.job?.phase || 'starting'}` :
      tune.job?.phase === 'failed' ? `Calibration failed: ${tune.job.error}` :
      tune.available ? `Matching table installed: ${tune.file}. Restart Strata to use it.` :
      'No exact matching table is installed. Strata falls back to hipBLAS for dense projections.'));
  const tuneActions = el('div', 'dl-actions');
  if (!tune.available && !strata.downloaded) {
    const get = el('button', 'secondary-button', 'Download matching table');
    get.type = 'button'; get.disabled = strata.installing;
    get.addEventListener('click', () => engineAction('strata-source'));
    tuneActions.append(get);
  }
  if (strata.downloaded) {
    const calibrate = el('button', 'primary-button', tune.available ? 'Retune for this card' : 'Calibrate for this card');
    calibrate.type = 'button'; calibrate.disabled = tune.running || strata.running || !strata.installed;
    calibrate.addEventListener('click', () => engineAction('hipblaslt-tune'));
    tuneActions.append(calibrate);
  }
  tuneRow.append(tuneInfo, tuneActions); $('core-hipblaslt').replaceChildren(tuneRow);
}


async function renderBenchInstall() {
  const box = $('core-llama-bench');
  if (!box) return;
  try {
    const data = await api('llama-benchmark/install/status');
    const row = el('div', 'lib-row core-build');
    const info = el('div', 'core-info');
    info.append(el('strong', '', data.installed ? 'Installed · connected to Agent' :
      data.state === 'installing' ? 'Installing matching llama-bench…' : 'Not installed'));
    info.append(el('p', 'hint', data.error || `Selected build: ${data.build || 'none'}`));
    const actions = el('div', 'dl-actions');
    const button = el('button', data.installed ? 'secondary-button' : 'primary-button', data.installed ? 'Uninstall' : 'Install llama-bench');
    button.type = 'button'; button.disabled = data.state === 'installing';
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const reply = await post(`llama-benchmark/${data.installed ? 'uninstall' : 'install'}`);
        toast(reply.message || 'Done', 'ok');
      } catch (error) { toast(error.message, 'error'); }
      renderBenchInstall();
    });
    actions.append(button); row.append(info, actions); box.replaceChildren(row);
    if (data.state === 'installing') setTimeout(renderBenchInstall, 3000);
  } catch (error) { box.replaceChildren(el('p', 'empty error', error.message)); }
}


async function coreAction(action, body, done) {
  try {
    const reply = await post(`core/llamacpp/${action}`, body);
    if (reply.confirm) {
      if (confirm(`${reply.message}\n\nDelete it anyway?`)) return coreAction(action, {...body, force: true}, done);
    } else {
      toast(reply.message || 'Done', 'ok');
      // Show the switch right away; the reload below confirms it from the host.
      if (action === 'activate' && state.core) {
        state.core.active = body.id;
        state.core.builds.forEach(build => { build.active = build.id === body.id; });
        renderCore();
      }
      if (done) done();
    }
  } catch (error) { toast(error.message, 'error'); }
  loadCore(true);
}

const isStock = build => build.kind === 'stock';
const isFpx = build => build.kind === 'rocmfpx';
function coreBuildName(build) {
  if (build.builtin) return `${build.label} · ${build.release} + RDNA Boost ${build.boost}`;
  if (build.legacy) return build.label;
  if (isFpx(build)) return `ROCmFPX ${build.release} · ${build.gpu_targets}`;
  const vk = (build.backends || []).includes('Vulkan') ? ' · ROCm + Vulkan' : '';
  return (isStock(build) ? `llama.cpp ${build.release} (stock)` : `${build.release} + RDNA Boost ${build.boost_short}`) + vk;
}

// "ROCm 10.1.0 (Rocsy)" / "ROCm 7.2.4 (system)"; a ? when the system ROCm changed since the build.
function rocmText(build) {
  if (!build.rocm_version) return '';
  return `ROCm ${build.rocm_version}${build.rocm_exact === false ? '?' : ''} (${build.rocm_id === 'system' ? 'system' : 'Rocsy'})`;
}

function buildRow(build, data) {
  const row = el('div', 'lib-row core-build' + (build.active ? ' is-active' : ''));
  const info = el('div', 'core-info');
  const head = el('div', 'dl-head');
  head.append(el('strong', '', coreBuildName(build)));
  const pills = el('span', 'core-pills');
  if (build.active) pills.append(el('span', 'pill pill-ok', 'In use'));
  if (rocmText(build)) {
    const pill = el('span', `pill core-rocm${build.rocm_id && build.rocm_id !== 'system' ? ' is-rocsy' : ''}`, rocmText(build).replace(/^ROCm /, 'ROCm '));
    pill.title = build.rocm_exact === false ? 'Built against /opt/rocm, which pacman has upgraded since; this is the current system version'
      : 'The ROCm this build was compiled against and runs with';
    pills.append(pill);
  }
  if (isStock(build) || isFpx(build)) pills.append(el('span', 'pill pill-retrying', isFpx(build) ? 'ROCmFPX' : 'No RDNA Boost'));
  if (build.legacy) pills.append(el('span', `pill ${build.backend === 'ROCm' ? 'pill-muted' : 'pill-failed'}`, build.backend === 'ROCm' ? 'ROCm GPU' : 'CPU only (no GPU)'));
  if (build.builtin && !build.exists) pills.append(el('span', 'pill pill-failed', 'Missing'));
  else if (!build.builtin && !build.legacy) pills.append(el('span', `pill pill-${build.phase === 'ready' ? 'muted' : build.phase === 'failed' ? 'failed' : 'downloading'}`,
    CORE_PHASE[build.phase] || build.phase));
  head.append(pills);
  info.append(head);
  if (!CORE_DONE.includes(build.phase)) {
    const bar = el('div', 'bar'), fill = el('span');
    fill.style.width = `${build.phase === 'compile' ? build.progress || 0 : build.phase === 'check' ? 100 : 2}%`;
    bar.append(fill);
    info.append(bar, el('p', 'hint', `${CORE_PHASE[build.phase]}${build.phase === 'compile' ? ` · ${build.progress || 0}%` : ''} · ROCm GPU build (${build.gpu_targets}) · compiling at low priority · started ${ago(build.started || build.created)}`));
  } else if (build.phase === 'failed') {
    info.append(el('p', 'hint error', build.error || 'Build failed'));
  } else if (build.builtin || build.legacy) {
    info.append(el('p', 'hint', [build.builtin ? `ROCm GPU build (${data.gpu_targets})` : build.backend === 'ROCm' ? 'ROCm GPU build' : 'CPU-only build, no GPU support',
      build.path.replace(HOME_RE, '~/')].join(' · ')));
  } else {
    info.append(el('p', 'hint', [isFpx(build) ? 'ROCmFPX quant support' : isStock(build) ? 'Stock, no RDNA Boost' : `${build.patches} RDNA Boost patches`, `${(build.backends || []).includes('Vulkan') ? 'ROCm + Vulkan' : 'ROCm'} GPU build (${build.gpu_targets})`,
      build.size && size(build.size), build.finished && `built ${ago(build.finished)}`].filter(Boolean).join(' · ')));
  }
  if (build.refs?.length) info.append(el('p', 'hint core-files', `Used by ${build.refs.join(', ')}`));
  if (build.pinned?.length) info.append(el('p', 'hint core-files', `Set for ${build.pinned.join(', ')} in llama-swap → Parameters`));
  const actions = el('div', 'dl-actions');
  const add = (label, handler, cls = 'link-button') => {
    const b = el('button', cls, label);
    b.type = 'button';
    b.addEventListener('click', handler);
    actions.append(b);
  };
  if (build.phase === 'ready' && !build.active && !build.legacy && (build.exists ?? true)) add('Use this build', () => {
    const lines = [`Switch llama-swap to ${coreBuildName(build)}?`];
    if (isStock(build) || isFpx(build)) {
      if (isFpx(build)) lines.push('', 'This build supports ROCmFPX quants and standard GGUF models; RDNA Boost-only flags are unavailable.');
      else lines.push('', 'THIS BUILD HAS NO RDNA BOOST. Expect slower prompt processing and decoding on RDNA3.');
      if (data.boost_only_models.length) lines.push(`These models use RDNA Boost-only options and will NOT start on it: ${data.boost_only_models.join(', ')}.`);
    }
    lines.push('', "A model that's already loaded keeps running; the next model load uses this build. You can switch back here any time.");
    if (confirm(lines.join('\n'))) coreAction('activate', {id: build.id});
  }, 'secondary-button');
  if (!CORE_DONE.includes(build.phase)) add('Cancel', () => isFpx(build) ? engineAction('rocmfpx-cancel') : coreAction('cancel', {}));
  if (!build.builtin && !build.legacy) add(state.coreLog?.id === build.id ? 'Hide log' : 'Log', () => openCoreLog(build));
  if (!build.active && CORE_DONE.includes(build.phase) && (build.exists ?? true)) add('Delete', () => {
    const where = build.path.replace(HOME_RE, '~/');
    if (confirm(`Delete ${coreBuildName(build)}?\n\nOnly this build folder is removed:\n${where}\n\nModels are never touched: the delete is refused if the folder holds any model file, is in use, or llama-swap points at it.`))
      coreAction('delete', {id: build.id});
  }, 'link-button danger');
  row.append(info, actions);
  return row;
}

function renderCore() {
  const data = state.core;
  const task = $('core-task');
  task.hidden = !data.fetch.running && !data.fetch.error;
  task.textContent = data.fetch.running ? 'Checking GitHub for new llama.cpp releases and RDNA Boost versions…' : `Last check failed: ${data.fetch.error}`;
  $('core-fetch').disabled = data.fetch.running;
  const running = data.builds.filter(b => !CORE_DONE.includes(b.phase));
  $('sub-core-badge').hidden = !running.length;
  $('sub-core-badge').textContent = running.length ? `${running[0].progress || 0}%` : '';

  const current = data.builds.filter(b => !b.legacy), older = data.builds.filter(b => b.legacy);
  const parts = current.map(b => buildRow(b, data));
  if (older.length) {
    parts.push(el('p', 'inst-sub core-sub', 'Older installs (made outside Core Backend; build folders only)'));
    parts.push(...older.map(b => buildRow(b, data)));
  }
  parts.push(el('p', 'hint core-foot', 'Switching takes effect on the next model load; a loaded model is never stopped. Deleting removes only a build folder, never models or source code.'));
  parts.push(el('p', 'hint core-foot', data.vulkan_buildable
    ? 'New builds include llama.cpp\'s Vulkan backend next to ROCm, so each model can pick ROCm or Vulkan in llama-swap → Parameters.'
    : `Vulkan backend: not included in builds yet - the host still needs ${(data.vulkan_missing || ['vulkan-headers']).join(', ')}. After that, new builds include Vulkan and llama-swap → Parameters offers ROCm or Vulkan per model.`));
  $('core-builds').replaceChildren(...parts);
  if (state.coreLog && !data.builds.some(b => b.id === state.coreLog.id)) closeCoreLog();

  const select = $('core-boost');
  select.hidden = data.boost.length < 2;
  select.replaceChildren(...data.boost.map(v => { const o = el('option', '', `RDNA Boost ${v.short} · ${date(new Date(v.date * 1000).toISOString())}`); o.value = v.sha; return o; }));
  const boost = data.boost.find(v => v.sha === data.boost_selected);
  if (boost) $('core-boost-note').textContent = `RDNA Boost ${boost.short} is upstream ${boost.base_tag} plus ${boost.patches} patches. A release is converted by replaying those patches onto it, then built for your GPUs (${data.gpu_targets}). Only releases every patch applies to cleanly can be converted; nothing is merged by hand.`;
  document.querySelectorAll('#core-list [data-list]').forEach(chip => chip.setAttribute('aria-checked', String(chip.dataset.list === state.coreList)));
  $('core-search').hidden = state.coreList !== 'all';
  if (state.coreList === 'all') renderCoreAll(data, boost);
  else renderCoreReleases(data, boost);
}

// When Vulkan can be built, only builds that include it count as "already built".
const vkKey = build => ((build.backends || []).includes('Vulkan') ? '|vk' : '');
function builtKeys(data) {
  return new Set(data.builds.filter(b => !b.legacy).map(b => (isStock(b) ? `${b.release}|stock` : `${b.release}|${b.boost_short || b.boost}`) + vkKey(b)));
}

function boostButton(release, boost, data, have) {
  const built = have.has(`${release}|${boost.short}${data.vulkan_buildable ? '|vk' : ''}`);
  const b = el('button', built ? 'link-button' : 'primary-button', built ? 'Already built' : 'Download & convert');
  b.type = 'button';
  b.disabled = built || data.building;
  if (data.building && !built) b.title = 'Another build is running';
  b.addEventListener('click', () => {
    if (confirm(`Download llama.cpp ${release}, apply the ${boost.patches} RDNA Boost patches and build it?\n\nIt's built as a ROCm GPU build for ${data.gpu_targets} against ${data.runtime || 'the system ROCm'} (about 5 min to compile, at low priority; renders aren't interrupted). Nothing changes until you choose "Use this build".`))
      coreAction('build', {release, boost: boost.sha});
  });
  return b;
}

function stockButton(release, data, have) {
  const built = have.has(`${release}|stock${data.vulkan_buildable ? '|vk' : ''}`);
  const b = el('button', built ? 'link-button' : 'secondary-button core-stock', built ? 'Stock build installed' : 'Download stock (no RDNA Boost)');
  b.type = 'button';
  b.disabled = built || data.building;
  if (data.building && !built) b.title = 'Another build is running';
  b.addEventListener('click', () => {
    if (confirm(`Download and build stock llama.cpp ${release}?\n\nThis build will NOT have RDNA Boost. ${NO_BOOST}\n\nIt's still a ROCm GPU build for ${data.gpu_targets} against ${data.runtime || 'the system ROCm'}, just without RDNA Boost (about 5 min to compile, at low priority). Very old releases may not build. Nothing changes until you choose "Use this build".`))
      coreAction('build', {release, stock: true});
  });
  return b;
}

function renderCoreReleases(data, boost) {
  const box = $('core-releases');
  if (!boost) { box.replaceChildren(el('p', 'empty', data.fetch.running ? 'Fetching versions…' : 'No versions yet. Use Check for new versions.')); return; }
  const have = builtKeys(data);
  const ok = data.releases.filter(r => r.compatible), bad = data.releases.filter(r => !r.compatible);
  const releaseRow = release => {
    const row = el('div', 'lib-row core-release' + (release.compatible ? '' : ' is-locked'));
    const info = el('div', 'core-info');
    const head = el('div', 'dl-head');
    head.append(el('strong', '', `llama.cpp ${release.tag}`),
      el('span', `pill ${release.compatible ? 'pill-ok' : 'pill-muted'}`, release.compatible ? 'RDNA Boost compatible' : 'Not RDNA Boost compatible'));
    info.append(head);
    const when = release.date ? date(new Date(release.date * 1000).toISOString()) : '';
    info.append(el('p', 'hint', release.compatible
      ? [when, release.tag === boost.base_tag ? 'the release RDNA Boost is based on' : 'all patches apply cleanly'].filter(Boolean).join(' · ')
      : [when, release.reason].filter(Boolean).join(' · ')));
    if (release.files?.length) info.append(el('p', 'hint core-files', release.files.join(', ')));
    const actions = el('div', 'dl-actions');
    actions.append(release.compatible ? boostButton(release.tag, boost, data, have) : stockButton(release.tag, data, have));
    row.append(info, actions);
    return row;
  };
  const parts = [ok.length ? el('div', 'core-list') : el('p', 'empty', 'No release is compatible with this RDNA Boost version.')];
  if (ok.length) parts[0].append(...ok.map(releaseRow));
  if (bad.length) {
    const more = el('details', 'core-incompatible');
    const total = Math.max(data.incompatible_total || 0, bad.length);
    more.append(el('summary', '', `${total} newer release${total === 1 ? '' : 's'} can't take RDNA Boost yet (newest ${bad[0].tag}); they can only be installed as stock llama.cpp.${total > bad.length ? ` Showing the newest ${bad.length}; see All versions for the rest.` : ''}`));
    const list = el('div', 'core-list');
    list.append(...bad.map(releaseRow));
    more.append(list);
    parts.push(more);
  }
  box.replaceChildren(...parts);
}

function renderCoreAll(data, boost) {
  const box = $('core-releases');
  const have = builtKeys(data);
  const query = $('core-search').value.trim().toLowerCase().replace(/^b/, '');
  const rows = data.all_versions.filter(([tag]) => !query || tag.slice(1).includes(query));
  const warn = el('p', 'core-warn', `RDNA Boost is applied only to releases marked compatible. Every other version installs as ${NO_BOOST}`);
  const list = el('div', 'core-list');
  list.append(...rows.slice(0, state.coreShown).map(([tag, compatible]) => {
    const row = el('div', 'lib-row core-release');
    const info = el('div', 'core-info');
    const head = el('div', 'dl-head');
    head.append(el('strong', '', `llama.cpp ${tag}`),
      el('span', `pill ${compatible ? 'pill-ok' : 'pill-retrying'}`, compatible ? 'RDNA Boost compatible' : 'No RDNA Boost'));
    info.append(head);
    const actions = el('div', 'dl-actions');
    if (compatible && boost) actions.append(boostButton(tag, boost, data, have));
    actions.append(stockButton(tag, data, have));
    row.append(info, actions);
    return row;
  }));
  const parts = [warn, rows.length ? list : el('p', 'empty', 'No release matches.')];
  if (rows.length > state.coreShown) {
    const more = el('button', 'secondary-button core-more', `Show more (${rows.length - state.coreShown} left)`);
    more.type = 'button';
    more.addEventListener('click', () => { state.coreShown += CORE_PAGE * 2; renderCore(); });
    parts.push(more);
  }
  box.replaceChildren(...parts);
}

$('core-list').addEventListener('click', event => {
  const chip = event.target.closest('[data-list]');
  if (!chip) return;
  state.coreList = chip.dataset.list;
  state.coreShown = CORE_PAGE;
  try { localStorage.setItem('models.coreList', state.coreList); } catch { /* private mode */ }
  if (state.core) renderCore();
});
$('core-search').addEventListener('input', () => { state.coreShown = CORE_PAGE; if (state.core) renderCore(); });

// Build log: its own panel outside the build list, so list refreshes never close it or move
// its scroll position. It sticks to the bottom only while you're at the bottom.
let coreLogTimer;
function openCoreLog(build) {
  if (state.coreLog?.id === build.id) { closeCoreLog(); return; }
  state.coreLog = {id: build.id, stick: true, text: ''};
  $('core-log-title').textContent = `Build log · ${coreBuildName(build)}`;
  $('core-log').textContent = 'Loading…';
  $('core-log-panel').hidden = false;
  pollCoreLog();
  if (state.core) renderCore();
  $('core-log-panel').scrollIntoView({block: 'nearest', behavior: 'smooth'});
}
function closeCoreLog() {
  clearTimeout(coreLogTimer);
  state.coreLog = null;
  $('core-log-panel').hidden = true;
  if (state.core) renderCore();
}
async function pollCoreLog() {
  clearTimeout(coreLogTimer);
  const log = state.coreLog;
  if (!log) return;
  const pre = $('core-log');
  try {
    const text = (await api(`core/llamacpp/log/${log.id}`)).log || '(empty)';
    if (state.coreLog !== log) return;
    if (text !== log.text) {
      log.text = text;
      pre.textContent = text;
      if (log.stick) pre.scrollTop = pre.scrollHeight;
    }
  } catch (error) { pre.textContent = error.message; }
  const build = state.core?.builds.find(b => b.id === log.id);
  if (state.coreLog === log) coreLogTimer = setTimeout(pollCoreLog, build && !CORE_DONE.includes(build.phase) ? 2000 : 15000);
}
$('core-log').addEventListener('scroll', () => {
  const pre = $('core-log');
  if (state.coreLog) state.coreLog.stick = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
});
$('core-log-close').addEventListener('click', closeCoreLog);
$('core-log-bottom').addEventListener('click', () => {
  const pre = $('core-log');
  pre.scrollTop = pre.scrollHeight;
  if (state.coreLog) state.coreLog.stick = true;
});

// ---- core backend: GPU runtimes (Rocsy's own ROCm versions + Vulkan tools) -----------------

const RT_DONE = ['done', 'failed', 'cancelled'];
const RT_PHASE = {queued: 'Queued', download: 'Downloading', verify: 'Checking the download', extract: 'Installing',
  build: 'Building the llama container image', waiting: 'Waiting for llama-swap to be idle', restart: 'Restarting llama-swap',
  done: 'Done', failed: 'Failed', cancelled: 'Cancelled'};
let rtTimer;

async function loadRuntime() {
  clearTimeout(rtTimer);
  try {
    state.runtime = await api('core/runtime');
    renderRuntime();
  } catch (error) { $('rt-rocm').replaceChildren(el('p', 'empty error', error.message)); }
  const job = state.runtime?.job;
  const busy = job && job.phase && !RT_DONE.includes(job.phase);
  if (state.view === 'manager' && state.sub === 'core') rtTimer = setTimeout(loadRuntime, busy ? 2000 : 30000);
  if (!$('rt-log-panel').hidden) loadRuntimeLog();
}

async function runtimeAction(action, body = {}, question = '') {
  if (question && !confirm(question)) return;
  try {
    const reply = await post(`core/runtime/${action}`, body);
    toast(reply.message || 'Done', 'ok');
  } catch (error) { toast(error.message, 'error'); }
  loadRuntime();
  if (action === 'default' || action === 'delete') loadCore(true);
}

function rtButton(label, handler, cls = 'link-button', disabled = false, title = '') {
  const button = el('button', cls, label);
  button.type = 'button';
  button.disabled = disabled;
  if (title) button.title = title;
  button.addEventListener('click', handler);
  return button;
}

function rtRow(title, pills, hint, actions) {
  const row = el('div', 'lib-row core-build');
  const info = el('div', 'core-info');
  const head = el('div', 'dl-head');
  const tags = el('span', 'core-pills');
  tags.append(...pills.filter(Boolean));
  head.append(el('strong', '', title), tags);
  info.append(head);
  if (hint) info.append(el('p', 'hint', hint));
  const box = el('div', 'dl-actions');
  box.append(...actions.filter(Boolean));
  row.append(info, box);
  return row;
}

function rtDefaultPicker(kind, options, current) {
  const wrap = el('label', 'rt-default');
  wrap.append(el('span', '', kind === 'rocm' ? 'New llama.cpp builds compile against' : 'New builds take Vulkan headers/compiler from'));
  const select = el('select');
  for (const [value, label] of options) { const o = el('option', '', label); o.value = value; select.append(o); }
  select.value = current;
  select.addEventListener('change', () => runtimeAction('default', {kind, version: select.value}));
  wrap.append(select);
  return wrap;
}

function renderRuntime() {
  const data = state.runtime;
  const job = data.job || {};
  const running = job.phase && !RT_DONE.includes(job.phase);
  const jobBox = $('rt-job');
  jobBox.replaceChildren();
  if (job.phase && (running || job.phase === 'failed')) {
    const name = job.kind === 'radv-update' ? 'Vulkan driver update' : `${job.kind === 'rocm-install' ? 'ROCm' : 'Vulkan SDK'} ${job.version}`;
    const pct = job.total ? Math.min(100, job.bytes / job.total * 100) : 0;
    const bar = el('div', 'bar'), fill = el('span');
    fill.style.width = `${job.phase === 'download' ? pct : running ? 100 : 0}%`;
    bar.append(fill);
    const detail = job.phase === 'failed' ? job.error : [RT_PHASE[job.phase] || job.phase,
      job.phase === 'download' && job.total ? `${size(job.bytes)} of ${size(job.total)} (${Math.round(pct)}%)` : ''].filter(Boolean).join(' · ');
    jobBox.append(rtRow(name, [el('span', `pill ${job.phase === 'failed' ? 'pill-failed' : 'pill-downloading'}`, RT_PHASE[job.phase] || job.phase)],
      '', [running && rtButton('Cancel', () => runtimeAction('cancel')), rtButton('Log', openRuntimeLog)]));
    jobBox.lastChild.querySelector('.core-info').append(...(running ? [bar] : []), el('p', `hint${job.phase === 'failed' ? ' error' : ''}`, detail));
  }

  // ROCm
  const rocm = data.rocm, defaults = data.defaults;
  const rocmBox = $('rt-rocm');
  const rocmParts = [el('h3', 'rt-title', 'ROCm')];
  rocmParts.push(rtDefaultPicker('rocm', [['system', `System ROCm ${rocm.system.version} (/opt/rocm)`],
    ...rocm.installed.map(i => [i.version, `Rocsy ROCm ${i.version}`])], defaults.build_rocm));
  rocmParts.push(rtRow(`System ROCm ${rocm.system.version}`, [
    defaults.build_rocm === 'system' && el('span', 'pill pill-ok', 'Default for builds'), el('span', 'pill pill-muted', 'pacman')],
    `${rocm.system.path} · installed and updated by your system (pacman), never changed by Rocsy`, []));
  for (const item of rocm.installed) {
    rocmParts.push(rtRow(`ROCm ${item.version}`, [defaults.build_rocm === item.version && el('span', 'pill pill-ok', 'Default for builds'),
      el('span', 'pill pill-muted', 'Rocsy only')],
      [item.path.replace(HOME_RE, '~/'), item.size && size(item.size), item.builds.length ? `used by ${item.builds.join(', ')}` : 'no builds use it yet'].filter(Boolean).join(' · '),
      [defaults.build_rocm !== item.version && rtButton('Make default', () => runtimeAction('default', {kind: 'rocm', version: item.version}), 'secondary-button'),
       rtButton('Delete', () => runtimeAction('delete', {kind: 'rocm', version: item.version},
         `Delete Rocsy's ROCm ${item.version}?\n\nOnly ${item.path.replace(HOME_RE, '~/')} is removed. Your system ROCm (/opt/rocm) isn't touched. It's refused while a llama.cpp build uses it.`), 'link-button danger',
         item.builds.length > 0 || defaults.build_rocm === item.version, item.builds.length ? 'llama.cpp builds use it' : '')]));
  }
  const more = el('details', 'core-incompatible');
  const notInstalled = rocm.available.filter(a => !a.installed);
  more.open = rocm.installed.length === 0;
  more.append(el('summary', '', `Download another ROCm version (${notInstalled.length} available for ${data.family})`));
  const list = el('div', 'core-list');
  list.append(...notInstalled.map(item => rtRow(`ROCm ${item.version}`, [el('span', 'pill pill-muted', item.channel)],
    `${size(item.size)} download · AMD TheRock build for ${data.family} · installs to ${data.paths.rocm.replace(HOME_RE, '~/')}/${item.version}`,
    [rtButton('Install', () => runtimeAction('install', {kind: 'rocm', version: item.version},
      `Download and install ROCm ${item.version} for Rocsy?\n\n${size(item.size)} download, roughly 4x that once installed, into ${data.paths.rocm.replace(HOME_RE, '~/')}/${item.version}. Your system ROCm and packages aren't changed.\n\nIt's used only by llama.cpp builds you compile against it. AMD only guarantees the ROCm + kernel driver pairs in its compatibility matrix, so the first model load on such a build is the real test.`),
      'secondary-button', running, running ? 'Another runtime job is running' : '')])));
  more.append(list);
  rocmParts.push(more);
  rocmParts.push(el('p', 'hint core-foot', 'Each llama.cpp build is compiled against one ROCm and always runs with it. To switch ROCm, build a version against it (pick the default above, then Download & convert), then choose that build per model in llama-swap → Parameters or for everything with Use this build. Nothing is reinstalled.'));
  rocmBox.replaceChildren(...rocmParts);

  // Vulkan
  const vk = data.vulkan;
  const vkParts = [el('h3', 'rt-title', 'Vulkan')];
  const sys = vk.system;
  vkParts.push(rtDefaultPicker('vulkan', [['system', `System packages (vulkan-headers ${sys.headers.split(':').pop() || 'missing'})`],
    ...vk.installed.map(i => [i.version, `Rocsy Vulkan SDK ${i.version}`])], defaults.build_vulkan));
  const radv = vk.radv;
  vkParts.push(rtRow(`Vulkan driver in llama-swap: RADV ${radv.installed || '?'}`, [
    radv.update ? el('span', 'pill pill-update', `${radv.latest} available`) : el('span', 'pill pill-ok', 'Up to date')],
    'Mesa RADV inside the llama container only. Updating rebuilds that container image; the driver your desktop and other programs use is untouched.',
    [radv.update && rtButton('Update driver', () => runtimeAction('radv-update', {},
      `Update the Vulkan driver inside the llama container to RADV ${radv.latest}?\n\nRocsy rebuilds only the llama container image, then restarts llama-swap once no model is loaded. Your system's drivers aren't changed.`),
      'secondary-button', running)]));
  vkParts.push(rtRow('System Vulkan build tools', [defaults.build_vulkan === 'system' && el('span', 'pill pill-ok', 'Default for builds'), el('span', 'pill pill-muted', 'pacman')],
    `vulkan-headers ${sys.headers || 'missing'} · spirv-headers ${sys.spirv || 'missing'} · shaderc ${sys.shaderc || 'missing'} · managed by your system, never changed by Rocsy`, []));
  for (const item of vk.installed) {
    vkParts.push(rtRow(`Vulkan SDK ${item.version}`, [defaults.build_vulkan === item.version && el('span', 'pill pill-ok', 'Default for builds'), el('span', 'pill pill-muted', 'Rocsy only')],
      [item.path.replace(HOME_RE, '~/'), item.size && size(item.size), item.builds.length ? `used by ${item.builds.join(', ')}` : ''].filter(Boolean).join(' · '),
      [defaults.build_vulkan !== item.version && rtButton('Make default', () => runtimeAction('default', {kind: 'vulkan', version: item.version}), 'secondary-button'),
       rtButton('Delete', () => runtimeAction('delete', {kind: 'vulkan', version: item.version}, `Delete Rocsy's Vulkan SDK ${item.version}? Your system Vulkan packages aren't touched.`),
         'link-button danger', item.builds.length > 0 || defaults.build_vulkan === item.version)]));
  }
  const vkMore = el('details', 'core-incompatible');
  const vkAvail = vk.available.filter(a => !a.installed);
  vkMore.append(el('summary', '', `Download a Vulkan SDK (LunarG, ${vkAvail.length} versions) - only needed to build with newer Vulkan headers than your system has`));
  const vkList = el('div', 'core-list');
  vkList.append(...vkAvail.map(item => rtRow(`Vulkan SDK ${item.version}`, [], `LunarG SDK · installs to ${data.paths.vulkan.replace(HOME_RE, '~/')}/${item.version}`,
    [rtButton('Install', () => runtimeAction('install', {kind: 'vulkan', version: item.version},
      `Download the LunarG Vulkan SDK ${item.version} for Rocsy's builds (about 370 MB)? Your system Vulkan packages aren't changed.`), 'secondary-button', running)])));
  vkMore.append(vkList);
  vkParts.push(vkMore);
  if (data.catalog_error) vkParts.push(el('p', 'hint error', `Some version lists couldn't be read: ${data.catalog_error}`));
  $('rt-vulkan').replaceChildren(...vkParts);
}

async function loadRuntimeLog() {
  try {
    const pre = $('rt-log');
    const stick = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
    const text = (await api('core/runtime/log')).log || '(empty)';
    if (pre.textContent !== text) { pre.textContent = text; if (stick) pre.scrollTop = pre.scrollHeight; }
  } catch (error) { $('rt-log').textContent = error.message; }
}
function openRuntimeLog() {
  $('rt-log-panel').hidden = false;
  $('rt-log').textContent = 'Loading…';
  loadRuntimeLog().then(() => { const pre = $('rt-log'); pre.scrollTop = pre.scrollHeight; });
}
$('rt-log-close').addEventListener('click', () => { $('rt-log-panel').hidden = true; });
$('rt-refresh').addEventListener('click', () => runtimeAction('refresh'));

$('core-fetch').addEventListener('click', () => coreAction('fetch', {}));

// ---- installed ------------------------------------------------------------------------------

async function loadInstalled() {
  if (!state.installed) { $('installed').replaceChildren(el('p', 'empty', 'Reading your models folder…')); watchMoves(); }
  try {
    state.installed = await api('installed');
    await loadBases();
    renderInstalled();
  } catch (error) { $('installed').replaceChildren(el('p', 'empty error', error.message)); }
}
$('inst-q').addEventListener('input', () => renderInstalled());
// Any sort other than "By kind" shows one flat list across all kinds (biggest files first, etc.).
try { state.instSort = localStorage.getItem('models.instSort') || 'kind'; } catch { state.instSort = 'kind'; }
$('inst-sort').value = state.instSort;
if ($('inst-sort').value !== state.instSort) state.instSort = $('inst-sort').value = 'kind';
$('inst-sort').addEventListener('change', () => {
  state.instSort = $('inst-sort').value;
  try { localStorage.setItem('models.instSort', state.instSort); } catch { /* private mode */ }
  renderInstalled();
});
const INST_SORTS = {
  'size-desc': (a, b) => b.size - a.size,
  'size-asc': (a, b) => a.size - b.size,
  name: (a, b) => a.name.localeCompare(b.name, undefined, {numeric: true, sensitivity: 'base'}),
  used: (a, b) => (a.last_used || 0) - (b.last_used || 0) || b.size - a.size,
};
$('unused-toggle').addEventListener('click', () => {
  state.showUnused = !state.showUnused;
  $('unused-toggle').setAttribute('aria-pressed', String(state.showUnused));
  if (!state.showUnused) $('unused-only').checked = false;
  renderInstalled();
});
$('unused-only').addEventListener('change', () => renderInstalled());

function lastUsed(item) {
  if (item.usage_note) return item.usage_note;
  if (item.last_used) return `Last used ${ago(item.last_used)}`;
  if (!item.tracked_since) return 'Usage not tracked yet';
  const days = Math.round((Date.now() / 1000 - item.tracked_since) / 86400);
  return days >= 1 ? `Not used in the last ${days} days` : 'Usage tracked from today';
}

// Each kind gets one colour, used in the storage bar, the kind chips, group heads and flat rows.
const KIND_COLORS = {
  'LLMs': 'var(--blue)', 'MTP prediction heads': '#d58cf0', 'Vision heads': '#6fd3e8', 'Video models': 'var(--violet)', 'Image models': 'var(--green)', 'LoRAs': 'var(--pink)',
  'Trainer models': '#ff8f6b', 'Trainer LoRAs & epochs': '#f5a3c7',
  'Text encoders & VAEs': 'var(--amber)', 'Upscalers & other': 'var(--teal)', 'Other models': 'var(--muted)',
};
// Groups whose sub-sections are Trainer runs: every run gets its own heading, epochs fold under it.
const TRAINER_GROUPS = ['Trainer models', 'Trainer LoRAs & epochs'];
const kindColor = name => KIND_COLORS[name] || 'var(--faint)';
try { state.instKind = localStorage.getItem('models.instKind') || ''; } catch { state.instKind = ''; }
function pickKind(name) {
  state.instKind = state.instKind === name ? '' : name;
  try { localStorage.setItem('models.instKind', state.instKind); } catch { /* private mode */ }
  renderInstalled();
}
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

function renderStorage(data, total) {
  const box = $('inst-storage');
  const whole = total + (data.free || 0);
  const bar = el('div', 'storage-bar');
  const legend = el('div', 'storage-legend');
  data.groups.filter(g => g.size).forEach(group => {
    const pct = group.size / whole * 100;
    const seg = el('button', 'storage-seg' + (state.instKind && state.instKind !== group.name ? ' is-dim' : ''));
    seg.type = 'button';
    seg.style.width = `${pct}%`;
    seg.style.background = kindColor(group.name);
    seg.title = `${group.name} · ${size(group.size)} (${Math.round(group.size / total * 100)}% of models)`;
    seg.addEventListener('click', () => pickKind(group.name));
    bar.append(seg);
  });
  const free = el('div', 'storage-seg is-free');
  free.style.width = `${(data.free || 0) / whole * 100}%`;
  free.title = `${size(data.free)} free on the drive`;
  bar.append(free);
  const used = el('span', 'storage-total');
  used.append(el('strong', '', size(total)), document.createTextNode(' models'));
  const left = el('span', 'storage-total');
  left.append(el('strong', '', size(data.free)), document.createTextNode(' free'));
  legend.append(used, left);
  box.replaceChildren(bar, legend);
}

function renderKinds(data, counts) {
  const all = el('button', 'kind-chip', '');
  all.type = 'button';
  all.setAttribute('role', 'radio');
  all.setAttribute('aria-checked', String(!state.instKind));
  all.append(el('span', '', 'All'), el('span', 'kind-count', String(counts.all)));
  all.addEventListener('click', () => pickKind(''));
  $('inst-kinds').replaceChildren(all, ...data.groups.map(group => {
    const chip = el('button', 'kind-chip');
    chip.type = 'button';
    chip.setAttribute('role', 'radio');
    chip.setAttribute('aria-checked', String(state.instKind === group.name));
    chip.style.setProperty('--kind', kindColor(group.name));
    chip.title = `${plural(group.count, 'file')} · ${size(group.size)}`;
    chip.append(el('span', 'kind-dot'), el('span', '', group.name), el('span', 'kind-count', size(group.size)));
    chip.addEventListener('click', () => pickKind(group.name));
    return chip;
  }));
}

// ---- Installed: multi-select + bulk uninstall ----
state.instSelected = new Set();
const selectable = item => item.removable !== false;
$('inst-select').addEventListener('click', () => {
  state.instSelecting = !state.instSelecting;
  $('inst-select').setAttribute('aria-pressed', String(state.instSelecting));
  $('inst-select').textContent = state.instSelecting ? 'Done' : 'Select';
  if (!state.instSelecting) state.instSelected.clear();
  renderInstalled();
});
function setSelected(paths, on) {
  paths.forEach(path => on ? state.instSelected.add(path) : state.instSelected.delete(path));
  syncSelection();
}
// Updates checkboxes, group "all" boxes and the bulk bar in place (no re-render, keeps focus + scroll).
function syncSelection() {
  const picked = state.instSelected;
  document.querySelectorAll('#installed .inst-row[data-path]').forEach(row => {
    const on = picked.has(row.dataset.path);
    row.classList.toggle('is-selected', on);
    const box = row.querySelector('.inst-check');
    if (box) box.checked = on;
  });
  document.querySelectorAll('#installed .group-check').forEach(box => {
    const paths = JSON.parse(box.dataset.paths);
    const n = paths.filter(p => picked.has(p)).length;
    box.checked = n > 0 && n === paths.length;
    box.indeterminate = n > 0 && n < paths.length;
  });
  const bar = $('inst-bulk');
  bar.hidden = !state.instSelecting;
  if (!state.instSelecting) return;
  const items = installedItems().filter(i => picked.has(i.path));
  $('bulk-count').textContent = items.length ? `${items.length} selected` : 'Nothing selected';
  $('bulk-size').textContent = items.length ? `${size(items.reduce((n, i) => n + i.size, 0))} would be freed` : 'Click rows to pick them · Shift-click picks a range';
  $('bulk-remove').disabled = !items.length;
  $('bulk-move').disabled = !items.length;
  $('bulk-move').textContent = items.length ? `Move ${items.length}…` : 'Move selected';
  $('bulk-remove').textContent = items.length ? `Uninstall ${items.length}` : 'Uninstall selected';
}
const installedItems = () => (state.installed?.groups || []).flatMap(g => g.items.map(item => ({...item, group: item.group || g.name})));
$('bulk-all').addEventListener('click', () => setSelected(state.instVisible || [], true));
$('bulk-clear').addEventListener('click', () => { state.instSelected.clear(); syncSelection(); });
$('bulk-remove').addEventListener('click', bulkUninstall);
$('bulk-move').addEventListener('click', () => openMove(installedItems().filter(i => state.instSelected.has(i.path))));

async function bulkUninstall() {
  const items = installedItems().filter(i => state.instSelected.has(i.path));
  if (!items.length) return;
  const total = items.reduce((n, i) => n + i.size, 0);
  const names = items.slice(0, 12).map(i => `• ${i.name} (${size(i.size)})`).join('\n') + (items.length > 12 ? `\n…and ${items.length - 12} more` : '');
  if (!confirm(`Uninstall ${items.length} file${items.length === 1 ? '' : 's'} and free ${size(total)}?\n\n${names}`)) return;
  const button = $('bulk-remove');
  button.disabled = true;
  let freed = 0, done = 0;
  const notes = [], failed = [], ask = [];
  const run = async (item, force) => {
    button.textContent = `Uninstalling ${++done}/${items.length}…`;
    try {
      const result = await post('uninstall', {path: item.path, force});
      freed += result.freed || 0;
      notes.push(...(result.notes || []));
      state.instSelected.delete(item.path);
    } catch (error) {
      if (!force && /anyway\?$/.test(error.message)) ask.push([item, error.message]);
      else failed.push([item, error.message]);
    }
  };
  for (const item of items) await run(item, false);
  // Files the studios or llama-swap still reference: ask once for all of them.
  if (ask.length && confirm(`${ask.length} file${ask.length === 1 ? ' needs' : 's need'} a second OK:\n\n`
      + ask.map(([item, msg]) => `• ${item.name}\n  ${msg.replace(/ Uninstall anyway\?$/, '')}`).join('\n\n') + '\n\nUninstall these anyway?')) {
    done = items.length - ask.length;
    for (const [item] of ask) await run(item, true);
  } else ask.forEach(([item]) => failed.push([item, 'kept (not confirmed)']));
  const removed = items.length - failed.length;
  toast(`Uninstalled ${removed} · freed ${size(freed) || '0 MB'}${notes.length ? ' · ' + [...new Set(notes)].slice(0, 3).join(', ') : ''}`, removed ? 'ok' : 'error');
  if (failed.length) toast(`Not uninstalled: ${failed.map(([item, msg]) => `${item.name} (${msg})`).join('; ')}`, 'error');
  await loadInstalled();
  refreshState();
}

function renderInstalled() {
  const data = state.installed;
  if (!data) return;
  const known = new Set(installedItems().map(i => i.path));
  [...state.instSelected].forEach(path => { if (!known.has(path)) state.instSelected.delete(path); });
  state.instVisible = [];
  if (state.instKind && !data.groups.some(g => g.name === state.instKind)) state.instKind = '';
  const query = $('inst-q').value.trim().toLowerCase();
  const total = data.groups.reduce((n, g) => n + g.size, 0);
  const stale = data.groups.flatMap(g => g.items).filter(i => i.unused);
  $('unused-bar').hidden = !state.showUnused;
  if (state.showUnused) {
    $('unused-text').textContent = stale.length
      ? `${plural(stale.length, 'file')} · ${size(stale.reduce((n, i) => n + i.size, 0))} not used in ${data.unused_days}+ days`
      : `Everything was used in the last ${data.unused_days} days.`;
  }
  const onlyStale = state.showUnused && $('unused-only').checked;
  const fileCount = data.groups.reduce((n, g) => n + g.count, 0);
  $('inst-summary').textContent = `${plural(fileCount, 'file')} in ${plural(data.groups.length, 'kind')} · click a kind or a bar segment to show only that kind`;
  renderStorage(data, total);
  renderKinds(data, {all: fileCount});
  const keep = item => (!query || `${item.name} ${item.sub} ${item.path} ${item.uses.map(u => u.label).join(' ')}`.toLowerCase().includes(query))
    && (!onlyStale || item.unused);
  const groups = data.groups.filter(g => !state.instKind || g.name === state.instKind);
  // Size bars are relative to the biggest file in view, so the heavy ones stand out at a glance.
  const biggest = Math.max(1, ...groups.flatMap(g => g.items.filter(keep).map(i => i.size)));
  const sorter = INST_SORTS[state.instSort];
  if (sorter) {
    const items = groups.flatMap(g => g.items.map(item => ({...item, group: item.group || g.name}))).filter(keep).sort(sorter);
    const list = el('div', 'inst-list');
    list.append(...items.map(item => installedRow(item, biggest, true)));
    $('installed').replaceChildren(items.length ? list : el('p', 'empty', 'Nothing matches.'));
    syncSelection();
    return;
  }
  const open = state.openGroups || (state.openGroups = new Set());
  const sections = groups.map(group => {
    const items = group.items.filter(keep);
    if (!items.length) return null;
    const section = el('details', 'inst-group');
    section.style.setProperty('--kind', kindColor(group.name));
    const staleHere = items.filter(i => i.unused);
    const sizeHere = items.reduce((n, i) => n + i.size, 0);
    section.open = open.has(group.name) || !!query || !!state.instKind || groups.length === 1 || (state.showUnused && staleHere.length > 0);
    section.addEventListener('toggle', () => section.open ? open.add(group.name) : open.delete(group.name));
    const head = el('summary');
    const title = el('div', 'inst-group-title');
    title.append(el('span', 'kind-dot'), el('strong', '', group.name), el('span', 'hint', plural(items.length, 'file')));
    if (state.showUnused && staleHere.length) title.append(el('span', 'tag stale-tag', `${staleHere.length} unused · ${size(staleHere.reduce((n, i) => n + i.size, 0))}`));
    const share = el('div', 'inst-group-size');
    const meter = el('div', 'share-meter');
    const fill = el('span');
    fill.style.width = `${Math.max(2, sizeHere / total * 100)}%`;
    meter.append(fill);
    share.append(meter, el('strong', '', size(sizeHere)));
    if (state.instSelecting) {
      const paths = items.filter(selectable).map(i => i.path);
      const all = el('input', 'group-check');
      all.type = 'checkbox';
      all.dataset.paths = JSON.stringify(paths);
      all.title = `Select every ${group.name.toLowerCase()} file shown`;
      all.disabled = !paths.length;
      all.addEventListener('click', event => {
        event.stopPropagation();  // don't fold the group
        setSelected(paths, !paths.every(p => state.instSelected.has(p)));
      });
      title.insertBefore(all, title.children[1]);
    }
    head.append(title, share);
    section.append(head);
    // Folders/sub-kinds with one file don't get their own heading: the row shows it instead.
    const bySub = new Map();
    items.forEach(item => { if (!bySub.has(item.sub)) bySub.set(item.sub, []); bySub.get(item.sub).push(item); });
    const body = el('div', 'inst-group-body');
    const singles = [];
    const runs = TRAINER_GROUPS.includes(group.name);
    bySub.forEach((members, sub) => {
      if (!runs && (members.length < 2 || !sub)) { singles.push(...members); return; }
      const subHead = el('div', 'inst-sub');
      subHead.append(el('span', '', sub), el('span', 'inst-sub-meta', `${members.length} · ${size(members.reduce((n, i) => n + i.size, 0))}`));
      body.append(subHead);
      if (members[0].sub_note) body.append(el('div', 'inst-sub-note', members[0].sub_note));
      const epochs = members.filter(i => i.epoch != null);
      body.append(...members.filter(i => i.epoch == null).map(item => installedRow(item, biggest, false)));
      if (epochs.length) body.append(epochFold(epochs, `${group.name}/${sub}`, biggest, !!query));
    });
    if (singles.length) {
      if (body.childElementCount) body.append(el('div', 'inst-sub', 'Other'));
      body.append(...singles.map(item => installedRow(item, biggest, false, true)));
    }
    section.append(body);
    return section;
  }).filter(Boolean);
  $('installed').replaceChildren(...(sections.length ? sections : [el('p', 'empty', 'Nothing matches.')]));
  syncSelection();
}

// A run's per-epoch checkpoints (and resume states), folded under its final file.
function epochFold(items, key, biggest, forceOpen) {
  const open = state.openEpochs || (state.openEpochs = new Set());
  const fold = el('details', 'inst-epochs');
  fold.open = forceOpen || open.has(key);
  fold.addEventListener('toggle', () => fold.open ? open.add(key) : open.delete(key));
  const head = el('summary');
  const numbers = items.filter(i => !i.resume_state).map(i => i.epoch);
  const span = numbers.length ? ` ${Math.min(...numbers)}–${Math.max(...numbers)}` : '';
  head.append(el('span', '', `Epochs${span}`), el('span', 'inst-sub-meta', `${plural(items.length, 'file')} · ${size(items.reduce((n, i) => n + i.size, 0))}`));
  const list = el('div', 'inst-epochs-body');
  // Rows render lazily on first open: a long run can have dozens of checkpoints.
  const fill = () => { if (!list.childElementCount) { list.append(...items.map(item => installedRow(item, biggest, false))); syncSelection(); } };
  if (fold.open || state.instSelecting) list.append(...items.map(item => installedRow(item, biggest, false)));
  fold.addEventListener('toggle', () => fold.open && fill());
  fold.append(head, list);
  return fold;
}

function trainerTag(item) {
  if (item.resume_state) return `Resume state · epoch ${item.epoch}`;
  if (item.epoch != null) return `Epoch ${item.epoch}`;
  return item.final ? 'Final' : '';
}

function installedRow(item, biggest, flat = false, showSub = false) {
  const row = el('div', 'inst-row' + (state.showUnused && item.unused ? ' is-stale' : ''));
  if (flat) row.style.setProperty('--kind', kindColor(item.group));
  if (state.instSelecting) {
    row.classList.add('is-picking');
    if (selectable(item)) {
      row.dataset.path = item.path;
      state.instVisible.push(item.path);
      const box = el('input', 'inst-check');
      box.type = 'checkbox';
      box.setAttribute('aria-label', `Select ${item.name}`);
      row.append(box);
      row.addEventListener('click', event => {
        if (event.target.closest('button, select, a')) return;
        const on = !state.instSelected.has(item.path);
        const order = state.instVisible, last = order.indexOf(state.instLastPick), here = order.indexOf(item.path);
        if (event.shiftKey && last >= 0 && here >= 0) {
          setSelected(order.slice(Math.min(last, here), Math.max(last, here) + 1), on);
          document.getSelection()?.removeAllRanges();
        } else setSelected([item.path], on);
        state.instLastPick = item.path;
      });
    }
  }
  const main = el('div', 'lib-main');
  const name = el('strong', 'inst-name', item.name);
  name.title = item.location || `models/${item.path}`;
  main.append(name);
  const where = el('div', 'inst-where');
  if (flat) where.append(el('span', 'kind-dot'), el('span', 'inst-kind', item.sub ? `${item.group} · ${item.sub}` : item.group));
  else if (showSub && item.sub) where.append(el('span', 'inst-kind', item.sub));
  if (trainerTag(item)) where.append(el('span', 'tag ' + (item.final ? 'epoch-final' : 'epoch'), trainerTag(item)));
  if (item.copy) {
    const tag = el('span', 'tag stored', item.copy_note || 'Copy');
    tag.title = 'A copy of this checkpoint; the original stays in the Trainer output folder';
    where.append(tag);
  }
  const parts = item.path.split('/');
  if (item.stored) {
    const tag = el('span', 'tag ' + (item.offline ? 'warn' : 'stored'), item.offline ? `${item.stored.label} · offline` : item.stored.managed ? `On ${item.stored.label}` : 'Linked');
    tag.title = item.offline ? `Its file is on ${item.stored.target}, which isn't reachable (drive unplugged?)` : `Stored at ${item.stored.target.replace(HOME_RE, '~/')}`;
    where.append(tag);
  }
  // Trainer runs: the folder is enough, the file name is the row title.
  if (item.location) where.append(el('span', 'hint', item.job ? item.location.replace(/\/[^/]+$/, '') : item.location));
  else if (parts.length > 2 && item.group !== 'LLMs') where.append(el('span', 'hint', `models/${parts.slice(0, -1).join('/')}`));
  if (where.childElementCount) main.append(where);
  const tags = el('div', 'inst-meta');
  item.uses.forEach(use => {
    const tag = el('span', `tag ${use.kind === 'llama' ? 'use-llama' : 'use-gallery'}`, use.label);
    tag.title = use.label;
    tags.append(tag);
  });
  if (item.status === 'update') tags.append(el('span', 'tag warn', 'Update available'));
  if (tags.childElementCount) main.append(tags);

  const sizeCell = el('div', 'inst-size');
  const bar = el('div', 'size-bar');
  const fill = el('span');
  fill.style.width = `${Math.max(1.5, item.size / biggest * 100)}%`;
  bar.append(fill);
  sizeCell.append(el('strong', '', size(item.size)), bar);
  const used = el('div', 'inst-used' + (item.unused ? ' is-old' : ''), lastUsed(item));

  const remove = el('button', 'secondary-button small danger', 'Uninstall');
  remove.type = 'button';
  remove.addEventListener('click', async () => {
    if (!confirm(`Uninstall ${item.name}?\n\nDeletes ${item.location || `models/${item.path}`} (${size(item.size)}).`)) return;
    remove.disabled = true;
    const run = force => post('uninstall', {path: item.path, force});
    try {
      let result;
      try { result = await run(false); }
      catch (error) {
        if (!/anyway\?$/.test(error.message) || !confirm(error.message)) throw error;
        result = await run(true);
      }
      toast(`Freed ${size(result.freed) || '0 MB'}${result.notes.length ? ' · ' + result.notes.join(', ') : ''}`, 'ok');
      loadInstalled();
      refreshState();
    } catch (error) {
      remove.disabled = false;
      toast(error.message, 'error');
    }
  });
  const actions = el('div', 'lib-actions');
  if (BASE_GROUPS.includes(item.group)) actions.append(basePicker(item));
  if (item.removable !== false && item.movable === false) actions.append(remove);
  else if (item.removable !== false) {
    const move = el('button', 'secondary-button small', 'Move');
    move.type = 'button';
    move.title = 'Store this file on another drive or folder (Settings → Storage)';
    move.disabled = Boolean(state.moving?.has(item.path));
    move.addEventListener('click', () => openMove([item]));
    actions.append(move, remove);
  }
  row.append(main, sizeCell, used, actions);
  return row;
}

// ---- Installed: move files between storage locations (Settings -> Storage; the host does the copying) ----
state.moving = new Set();
const MOVE_LIVE = ['queued', 'copying', 'verifying'];
let movePoll = null;

async function openMove(items) {
  items = items.filter(i => i.removable !== false);
  if (!items.length) return;
  const dialog = $('move-dialog');
  const total = items.reduce((n, i) => n + i.size, 0);
  $('move-title').textContent = items.length === 1 ? `Move ${items[0].name}` : `Move ${items.length} files`;
  $('move-lead').textContent = `${size(total)} · pick where ${items.length === 1 ? 'it' : 'they'} should live.`;
  $('move-targets').replaceChildren(el('p', 'hint', 'Reading storage locations…'));
  $('move-go').disabled = true;
  dialog.showModal();
  let data;
  try { data = await api('storage'); } catch (error) { $('move-targets').replaceChildren(el('p', 'hint error', error.message)); return; }
  const here = new Set(items.map(i => i.stored?.id || 'comfy'));
  const targets = [data.builtin, ...data.locations];
  if (!data.locations.length) {
    const empty = el('p', 'hint', 'No other storage locations yet. ');
    const link = el('a', '', 'Add one in Settings → Storage');
    link.href = '/Settings#storage-locations';
    empty.append(link);
    $('move-targets').replaceChildren(empty);
    dialog.querySelector('.move-note').hidden = true;
    return;
  }
  dialog.querySelector('.move-note').hidden = false;
  {
  }
  let picked = '';
  $('move-targets').replaceChildren(...targets.map(target => {
    const card = el('label', 'move-target');
    const radio = el('input');
    radio.type = 'radio';
    radio.name = 'move-to';
    radio.value = target.id;
    const free = target.disk?.free || 0;
    const reason = !target.online ? 'Not reachable (drive unplugged?)'
      : !target.writable ? "Can't write there"
      : !target.ready ? 'Still being connected to the apps (they refresh once idle)'
      : here.size === 1 && here.has(target.id) ? (items.length === 1 ? 'It is already here' : 'They are all here already')
      : free && free < total + 2 * GB && target.disk?.mount !== data.builtin.disk?.mount ? `Only ${size(free)} free` : '';
    radio.disabled = Boolean(reason);
    radio.addEventListener('change', () => { picked = target.id; $('move-go').disabled = false; });
    const text = el('span', 'move-target-text');
    text.append(el('strong', '', target.label), el('span', 'hint', target.path.replace(HOME_RE, '~/')));
    const meta = el('span', 'move-target-meta', reason || [free ? `${size(free)} free` : '', target.disk?.model, target.disk?.fstype].filter(Boolean).join(' · '));
    if (reason) card.classList.add('is-off');
    card.append(radio, text, meta);
    return card;
  }));
  dialog.onclose = async () => {
    if (dialog.returnValue !== 'go' || !picked) return;
    const failed = [];
    for (const item of items) {
      if ((item.stored?.id || 'comfy') === picked) continue;
      try { await post('storage/move', {path: item.path, to: picked}); state.moving.add(item.path); }
      catch (error) { failed.push(`${item.name}: ${error.message}`); }
    }
    if (failed.length) toast(failed.join('; '), 'error');
    else toast(items.length === 1 ? 'Moving… the file stays usable until the switch' : `Moving ${items.length} files, one at a time…`, 'ok');
    if (state.instSelecting) { state.instSelected.clear(); syncSelection(); }
    watchMoves();
  };
}

async function watchMoves() {
  clearTimeout(movePoll);
  let data;
  try { data = await api('storage'); } catch { movePoll = setTimeout(watchMoves, 5000); return; }
  const box = $('inst-moves');
  const live = (data.jobs || []).filter(j => MOVE_LIVE.includes(j.state));
  const recent = (data.jobs || []).filter(j => !MOVE_LIVE.includes(j.state) && j.finished > Date.now() / 1000 - 600 && state.moving.has(j.path));
  const names = Object.fromEntries([data.builtin, ...data.locations].map(l => [l.id, l.label]));
  box.hidden = !live.length && !recent.length;
  box.replaceChildren(...[...live, ...recent].map(job => {
    const row = el('div', 'move-row is-' + job.state);
    const pct = job.total ? Math.min(100, job.bytes / job.total * 100) : 0;
    const label = {queued: 'Waiting', copying: 'Copying', verifying: 'Checking the copy', done: 'Moved', failed: 'Failed', cancelled: 'Cancelled'}[job.state] || job.state;
    const text = el('div', 'move-text');
    text.append(el('strong', '', job.path.split('/').pop()),
      el('span', 'hint', `${label} → ${names[job.to] || job.to}${job.state === 'copying' ? ` · ${size(job.bytes) || '0 MB'} of ${size(job.total)}` : ''}${job.error ? ` · ${job.error}` : job.note ? ` · ${job.note}` : ''}`));
    const bar = el('div', 'size-bar');
    const fill = el('span');
    fill.style.width = `${job.state === 'done' ? 100 : job.state === 'verifying' ? 100 : pct}%`;
    bar.append(fill);
    row.append(text, bar);
    if (MOVE_LIVE.includes(job.state)) {
      const cancel = el('button', 'link-button danger', 'Cancel');
      cancel.type = 'button';
      cancel.addEventListener('click', () => post(`storage/move/${job.id}/cancel`).then(watchMoves).catch(e => toast(e.message, 'error')));
      row.append(cancel);
    }
    return row;
  }));
  const finished = [...state.moving].filter(path => !live.some(j => j.path === path));
  if (finished.length) {
    finished.forEach(path => state.moving.delete(path));
    const done = recent.filter(j => finished.includes(j.path));
    if (done.some(j => j.state === 'failed')) toast(`Move failed: ${done.filter(j => j.state === 'failed').map(j => j.error).join('; ')}`, 'error');
    loadInstalled();
  }
  live.forEach(j => state.moving.add(j.path));
  if (live.length) movePoll = setTimeout(watchMoves, 1500);
}

// Manual base association ("Other models" file that is really MiniMax H3): the
// picker reassigns it, which also moves the file into that base's folder.
const BASE_GROUPS = ['Video models', 'Image models', 'Audio models', 'Other models'];
function baseOptions(current) {
  const seen = new Set(), out = [];
  const add = b => { if (b && !seen.has(b)) { seen.add(b); out.push(b); } };
  add(current);
  (bases?.installed || []).forEach(b => add(b.base));
  (bases?.all || []).forEach(add);
  (bases?.hf_mapped || []).forEach(add);
  return out;
}

function basePicker(item) {
  const pick = el('select', 'base-override');
  pick.title = 'Associate with a base model by hand. Model files move to the folder that base belongs in, so the studios recognize them.';
  const auto = el('option', '', `Automatic${item.base && !item.base_manual ? ` (${item.base})` : ''}`);
  auto.value = '';
  pick.append(auto);
  baseOptions(item.base_manual || item.base).forEach(b => {
    const option = el('option', '', b);
    option.value = b;
    pick.append(option);
  });
  pick.value = item.base_manual || '';
  pick.addEventListener('change', async () => {
    pick.disabled = true;
    try {
      const result = await post('installed/base', {path: item.path, base: pick.value});
      toast((result.moved ? `Moved to models/${result.path.split('/').slice(0, -1).join('/')}` : result.base ? `Associated with ${result.base}` : 'Back to automatic')
        + (result.notes.length ? ` · ${result.notes.join(', ')}` : ''), 'ok');
      loadInstalled();
      refreshState();
    } catch (error) {
      pick.value = item.base_manual || '';
      toast(error.message, 'error');
    }
    pick.disabled = false;
  });
  return pick;
}

// ---- downloads + state -------------------------------------------------------------------

// Downloads: overall progress, filter chips, then sections (in progress → waiting → failed → finished).
const DL_SECTIONS = [
  {key: 'active', label: 'In progress', states: ['downloading', 'verifying', 'queued']},
  {key: 'waiting', label: 'Paused & scheduled', states: ['paused', 'scheduled']},
  {key: 'failed', label: 'Failed & cancelled', states: ['failed', 'cancelled']},
  {key: 'done', label: 'Finished', states: ['complete']},
];
const DL_SOURCES = {civitai: 'Civitai', civitai_red: 'Civitai Red', hf: 'Hugging Face', 'comfy-setup': 'ComfyUI setup'};
const DL_ORDER = {downloading: 0, verifying: 1, queued: 2};
state.dlFilter = 'all';
const shortTime = secs => secs > 5400 ? `${Math.round(secs / 3600)} h` : secs > 90 ? `${Math.round(secs / 60)} min` : `${secs} s`;

function renderDownloads(jobs) {
  const box = $('downloads');
  const bySection = Object.fromEntries(DL_SECTIONS.map(sec => [sec.key, jobs.filter(job => sec.states.includes(job.state))]));
  bySection.active.sort((a, b) => (DL_ORDER[a.state] ?? 9) - (DL_ORDER[b.state] ?? 9) || a.created - b.created);
  bySection.done.sort((a, b) => (b.finished || 0) - (a.finished || 0));
  bySection.failed.sort((a, b) => (b.finished || b.created) - (a.finished || a.created));
  $('retry-failed').hidden = !jobs.some(job => job.state === 'failed');
  $('clear-finished').hidden = !jobs.some(job => ['complete', 'cancelled'].includes(job.state));
  $('dl-overview').hidden = !jobs.length;

  // Overall progress of what is downloading right now.
  const running = jobs.filter(job => job.state === 'downloading' && job.size);
  $('dl-total').hidden = !running.length;
  if (running.length) {
    const got = running.reduce((n, j) => n + j.bytes, 0), want = running.reduce((n, j) => n + j.size, 0);
    const speed = running.reduce((n, j) => n + (j.speed || 0), 0);
    $('dl-total-pct').textContent = `${Math.floor(got / want * 100)}%`;
    $('dl-total-text').textContent = [`${size(got)} of ${size(want)}`, speed && `${size(speed)}/s`,
      speed && `${shortTime(Math.round((want - got) / speed))} left`].filter(Boolean).join(' · ');
    $('dl-total-fill').style.width = `${got / want * 100}%`;
  }
  const counts = Object.fromEntries(DL_SECTIONS.map(sec => [sec.key, bySection[sec.key].length]));
  $('dl-summary').textContent = jobs.length
    ? [counts.active && `${counts.active} in progress`, counts.waiting && `${counts.waiting} waiting`,
       counts.failed && `${counts.failed} failed or cancelled`, counts.done && `${counts.done} finished`].filter(Boolean).join(' · ')
    : 'Resumable and SHA-256 checked. Files land in ComfyUI\'s models folder.';
  if (state.dlFilter !== 'all' && !counts[state.dlFilter]) state.dlFilter = 'all';
  const chip = (key, label, n) => {
    const b = el('button', 'kind-chip');
    b.type = 'button';
    b.setAttribute('role', 'radio');
    b.setAttribute('aria-checked', String(state.dlFilter === key));
    b.append(el('span', '', label), el('span', 'kind-count', String(n)));
    b.addEventListener('click', () => { state.dlFilter = key; renderDownloads(state.downloads || []); });
    return b;
  };
  $('dl-filter').replaceChildren(chip('all', 'All', jobs.length),
    ...DL_SECTIONS.filter(sec => counts[sec.key]).map(sec => chip(sec.key, sec.label, counts[sec.key])));

  if (!jobs.length) {
    const empty = el('div', 'dl-empty');
    const go = (label, view) => { const b = el('button', 'secondary-button small', label); b.type = 'button'; b.addEventListener('click', () => setView(view)); return b; };
    empty.append(el('strong', '', 'No downloads yet'), el('p', 'hint', 'Pick a model or LoRA and press Download: it shows up here with live progress.'));
    const actions = el('div', 'head-actions');
    actions.append(go('Browse models', 'models'), go('Browse LoRAs', 'loras'));
    empty.append(actions);
    box.replaceChildren(empty);
    return;
  }
  box.replaceChildren(...DL_SECTIONS.filter(sec => bySection[sec.key].length && (state.dlFilter === 'all' || state.dlFilter === sec.key)).map(sec => {
    const section = el('section', `dl-section dl-sec-${sec.key}`);
    const head = el('div', 'dl-section-head');
    head.append(el('span', '', sec.label), el('span', 'inst-sub-meta', String(bySection[sec.key].length)));
    section.append(head, ...bySection[sec.key].map(downloadRow));
    return section;
  }));
}

function downloadRow(job) {
  const waiting = job.state === 'queued' && job.retry?.at;  // automatic retry pending
  const shown = waiting ? 'retrying' : job.state;
  const row = el('div', `dl-item dl-${shown}`);
  const top = el('div', 'dl-top');
  const title = el('div', 'dl-title');
  const name = el('strong', '', job.name);
  name.title = `models/${job.dest}/${job.name}`;
  const pill = el('span', `pill pill-${shown}`);
  pill.append(el('span', 'pill-dot'), document.createTextNode(shown));
  title.append(name, pill);
  const actions = el('div', 'dl-buttons');
  const add = (label, action, kind = '') => {
    const b = el('button', `secondary-button small ${kind}`, label);
    b.type = 'button';
    b.addEventListener('click', async () => { b.disabled = true; await post(`downloads/${job.id}/${action}`); refreshState(); });
    actions.append(b);
  };
  if (['queued', 'downloading'].includes(job.state)) { add('Pause', 'pause'); add('Cancel', 'cancel', 'danger'); }
  if (job.state === 'paused') { add('Resume', 'resume'); add('Cancel', 'cancel', 'danger'); }
  if (job.state === 'scheduled') { add('Try now', 'start'); add('Cancel', 'cancel', 'danger'); }
  if (['failed', 'cancelled'].includes(job.state)) { add('Retry', 'retry'); add('Remove', 'clear'); }
  if (job.state === 'complete') add('Remove from list', 'clear');
  top.append(title, actions);
  row.append(top);

  const where = el('div', 'dl-where');
  const source = DL_SOURCES[job.source] || job.source;
  if (source) where.append(el('span', `tag dl-src src-${job.source}`, source));
  where.append(el('span', 'hint', `models/${job.dest}`));
  if (job.previous) where.append(el('span', 'hint', `previous kept as ${job.previous.split('/').pop()}`));
  row.append(where);

  if (job.state !== 'complete' && (job.bytes || ['downloading', 'verifying', 'queued'].includes(job.state))) {
    const pct = job.size ? Math.min(100, job.bytes / job.size * 100) : 0;
    const progress = el('div', 'dl-progress-row');
    const bar = el('div', 'dl-progress');
    const fill = el('span');
    fill.style.width = `${pct}%`;
    bar.append(fill);
    progress.append(bar, el('span', 'dl-pct', job.size ? `${Math.floor(pct)}%` : ''));
    row.append(progress);
  }
  const meta = el('p', 'dl-meta');
  const eta = job.speed && job.size ? Math.round((job.size - job.bytes) / job.speed) : 0;
  if (waiting) {
    meta.dataset.retryAt = job.retry.at;
    meta.dataset.retryText = `attempt ${job.retry.attempt} of ${job.retry.of} · ${job.retry.error}`;
  }
  meta.textContent = job.state === 'scheduled'
    ? `Early access on Civitai · starts by itself ${untilText(new Date(job.not_before * 1000).toISOString())} · Try now after unlocking it with Buzz`
    : waiting ? retryText(meta) : job.error || job.state_detail
    || (job.state === 'complete' ? [job.size && size(job.size), job.finished && `finished ${ago(job.finished)}`, job.note].filter(Boolean).join(' · ') : '')
    || [job.size ? (job.bytes ? `${size(job.bytes)} of ${size(job.size)}` : size(job.size)) : job.bytes && size(job.bytes),
        job.state === 'downloading' && job.speed && `${size(job.speed)}/s`, job.state === 'downloading' && eta && `${shortTime(eta)} left`,
        job.state === 'queued' && 'waiting for a free slot'].filter(Boolean).join(' · ');
  if (job.error) meta.classList.add('is-error');
  row.append(meta);
  return row;
}
// "Retrying in 40 s · attempt 2 of 5 · Connection dropped": counts down between state polls.
function retryText(meta) {
  const left = Math.max(0, Math.round(Number(meta.dataset.retryAt) - Date.now() / 1000));
  return `${left ? `Retrying in ${left > 90 ? Math.round(left / 60) + ' min' : left + ' s'}` : 'Retrying now'} · ${meta.dataset.retryText}`;
}
setInterval(() => document.querySelectorAll('#downloads [data-retry-at]').forEach(meta => { meta.textContent = retryText(meta); }), 1000);
$('retry-failed').addEventListener('click', async () => {
  for (const job of (state.downloads || []).filter(job => job.state === 'failed')) await post(`downloads/${job.id}/retry`);
  refreshState();
});
$('clear-finished').addEventListener('click', async () => {
  const done = (state.downloads || []).filter(job => ['complete', 'cancelled'].includes(job.state));
  for (const job of done) await post(`downloads/${job.id}/clear`);
  refreshState();
});

const finishedUpdateJobs = new Set();
let updateLibraryRefreshTimer;
async function refreshState() {
  try {
    const data = await api('state');
    state.hw = data.hardware;
    state.settings = data.settings;
    state.downloads = data.downloads;
    let updateFinished = false;
    for (const job of data.downloads) {
      if (!job.update_batch) continue;
      const finished = (job.state === 'complete' && job.postprocessed) || ['failed', 'cancelled'].includes(job.state);
      if (!finished) finishedUpdateJobs.delete(job.id);
      else if (!finishedUpdateJobs.has(job.id)) { finishedUpdateJobs.add(job.id); updateFinished = true; }
    }
    if (updateFinished && state.view === 'manager') {
      clearTimeout(updateLibraryRefreshTimer);
      updateLibraryRefreshTimer = setTimeout(() => {
        if (state.sub === 'updates') loadLibrary();
        if (state.sub === 'installed') loadInstalled();
      }, 400);
    }
    const gpu = (data.hardware.gpus || [])[0];
    $('hw-chip-text').textContent = gpu ? `${gpu.name.replace('AMD Radeon ', '').split(' / ')[0]} · ${Math.round(gpu.vram / GB)} GB`
      + ((data.hardware.gpus || []).length > 1 ? ` +${data.hardware.gpus.length - 1}` : '') : 'No GPU found';
    $('hw-chip').className = 'service-state ' + (gpu ? 'is-online' : 'is-error');
    const active = data.downloads.filter(job => job.state === 'downloading');
    $('dl-chip').hidden = !data.active;
    if (data.active) {
      const done = active.reduce((n, j) => n + j.bytes, 0), total = active.reduce((n, j) => n + (j.size || 0), 0);
      $('dl-chip-text').textContent = total ? `${Math.round(done / total * 100)}%` : `${data.active}`;
    }
    const updateCount = (data.updates || 0) + (data.node_updates || 0);
    $('updates-badge').hidden = $('sub-updates-badge').hidden = !updateCount;
    $('updates-badge').textContent = $('sub-updates-badge').textContent = updateCount;
    $('sub-dl-badge').hidden = !data.active;
    $('sub-dl-badge').textContent = data.active;
    $('llama-pending').hidden = !data.llama_restart?.pending;
    if (state.view === 'manager' && state.sub === 'downloads') renderDownloads(data.downloads);
    if (!settingsFilled) fillSettings(data.settings);
    return data;
  } catch { return null; }
}
$('hw-chip').addEventListener('click', () => setView('hardware'));
$('dl-chip').addEventListener('click', () => setView('manager'));

// ---- manager / library -------------------------------------------------------------------

radio('lib-filter', 'filter', value => { state.libFilter = value; renderLibrary(); });
$('lib-q').addEventListener('input', () => renderLibrary());

async function loadLibrary() {
  try {
    const data = await api('library');
    state.library = data.items;
    state.lastCheck = data.last_check;
    showTask(data.task);
    renderLibrary();
    loadOlds();
  } catch (error) { $('library').replaceChildren(el('p', 'empty error', error.message)); }
}

function showTask(task) {
  const node = $('lib-task');
  node.hidden = !task.running && !task.message;
  node.textContent = task.running
    ? `${task.kind === 'updates' ? 'Checking for updates' : 'Identifying'} ${task.done}/${task.total}${task.current ? ' · ' + task.current : ''}`
    : task.message;
  $('check').disabled = $('identify').disabled = task.running;
  if (task.running) setTimeout(pollTask, 1500);
}
async function pollTask() {
  const data = await api('library');
  state.library = data.items;
  state.lastCheck = data.last_check;
  showTask(data.task);
  if (!data.task.running) { renderLibrary(); refreshState(); }
}

const STATUS = {update: ['Update available', 'update'], current: ['Up to date', 'ok'], unchecked: ['Not checked yet', ''],
  unknown: ['Unknown source', 'muted'], not_found: ['Source not found', 'muted'], error: ['Check failed', 'error'],
  local: ['Local', 'ok']};

function renderLibrary() {
  const items = state.library;
  const updates = items.filter(i => i.status === 'update').length;
  const unknown = items.filter(i => ['unknown', 'not_found'].includes(i.status)).length;
  const local = items.filter(i => i.status === 'local').length;
  $('lib-summary').textContent = `${items.length} files · ${updates} update${updates === 1 ? '' : 's'} · ${local ? local + ' made here · ' : ''}${unknown} of unknown source · last checked ${ago(state.lastCheck)}`;
  const query = $('lib-q').value.trim().toLowerCase();
  const shown = items.filter(item => {
    if (state.libFilter === 'updates' && item.status !== 'update') return false;
    if (state.libFilter === 'lora' && item.kind !== 'lora') return false;
    if (state.libFilter === 'model' && item.kind !== 'model') return false;
    if (state.libFilter === 'unknown' && !['unknown', 'not_found'].includes(item.status)) return false;
    return !query || `${item.path} ${item.link?.name || ''}`.toLowerCase().includes(query);
  }).sort((a, b) => (b.status === 'update') - (a.status === 'update') || a.path.localeCompare(b.path));
  if (!shown.length) {
    $('library').replaceChildren(el('p', 'empty', state.libFilter === 'updates'
      ? (state.lastCheck ? 'Everything linked is up to date.' : 'Press "Check for updates" to look for newer versions.')
      : 'Nothing here.'));
    return;
  }
  $('library').replaceChildren(...shown.slice(0, 400).map(libraryRow));
}

function libraryRow(item) {
  const row = el('div', 'lib-row' + (item.status === 'update' ? ' has-update' : ''));
  const main = el('div', 'lib-main');
  const name = el('strong', '', item.link?.name || item.name);
  name.title = item.path;
  main.append(name);
  const meta = el('p', 'hint');
  const source = item.link?.source ? SOURCE[item.link.source] || item.link.source : '';
  meta.textContent = [item.link?.name ? item.name : '', `models/${item.path.split('/').slice(0, -1).join('/')}`, size(item.size),
    source && `${source}${item.link.version_name ? ' · ' + item.link.version_name : ''}${item.link.linked_by ? ' (' + item.link.linked_by + ')' : ''}`]
    .filter(Boolean).join(' · ');
  main.append(meta);
  const update = item.link?.update;
  if (update?.available) {
    main.append(el('p', 'update-line', `New: ${update.version_name}${update.base ? ' · ' + update.base : ''}${update.published ? ' · ' + date(update.published) : ''}${update.count > 1 ? ` (${update.count} newer versions)` : ''}${update.early_access ? ' · early access' : ''}`));
  }
  if (update?.installed_as) main.append(el('p', 'hint', `Newer version already installed as ${update.installed_as}`));
  if (update?.other_versions?.length) main.append(el("p", "hint", `Other newer versions on the page: ${update.other_versions.slice(0, 3).join(", ")}${update.other_versions.length > 3 ? "…" : ""}`));
  if (item.link?.error) main.append(el('p', 'hint error', item.link.error));
  if (item.link?.note) main.append(el('p', 'hint', item.link.note));
  if (item.link?.local_changes) main.append(el('p', 'hint', 'Changed locally (e.g. a rewritten header), so updates are not checked automatically'));

  const [label, tone] = STATUS[item.status] || ['', ''];
  const side = el('div', 'lib-side');
  side.append(el('span', `pill ${tone ? 'pill-' + tone : ''}`, label));
  const actions = el('div', 'lib-actions');
  const button = (text, cls, onClick) => {
    const b = el('button', cls, text);
    b.type = 'button';
    b.addEventListener('click', () => onClick(b));
    actions.append(b);
  };
  if (item.status === 'update') button('Update…', 'primary-button small', () => confirmUpdate(item, row));
  if (['unknown', 'not_found', 'error'].includes(item.status)) {
    if (!item.sha256 && item.status === 'unknown') button('Identify', 'secondary-button small', async b => {
      b.disabled = true;
      await post('library/identify', {paths: [item.path], compute: true});
      toast(item.size > 5 * GB ? 'Hashing a big file: this takes a minute' : 'Identifying…');
      setTimeout(pollTask, 800);
    });
    button('Link…', 'secondary-button small', () => linkPrompt(item, row));
  }
  const origin = item.link?.source === 'local' ? item.link.derived_from : null;   // a local conversion: link its original
  if (origin?.repo || origin?.model_id) {
    const open = el('a', 'link-button', 'Original ↗');
    open.href = origin.repo ? `https://huggingface.co/${origin.repo}` : `https://civitai.com/models/${origin.model_id}`;
    open.target = '_blank';
    open.rel = 'noopener noreferrer';
    actions.append(open);
  }
  if (item.link?.source && item.link.source !== 'local') {
    const open = el('a', 'link-button', 'Source ↗');
    open.href = item.link.source === 'hf' ? `https://huggingface.co/${item.link.repo}`
      : `https://${item.link.source === 'civitai_red' || item.link.nsfw ? 'civitai.red' : 'civitai.com'}/models/${item.link.model_id}?modelVersionId=${item.link.version_id}`;
    open.target = '_blank';
    open.rel = 'noopener noreferrer';
    actions.append(open);
  }
  side.append(actions);
  row.append(main, side);
  return row;
}

function confirmUpdate(item, row) {
  row.querySelector('.inline-form')?.remove();
  const form = el('div', 'inline-form');
  const keep = el('label', 'check');
  const box = el('input');
  box.type = 'checkbox';
  box.checked = true;
  keep.append(box, ` Keep the filename ${item.name} (studio LoRA/model pickers keep working)`);
  const go = el('button', 'primary-button small', 'Download update');
  go.type = 'button';
  const cancel = el('button', 'link-button', 'Cancel');
  cancel.type = 'button';
  cancel.addEventListener('click', () => form.remove());
  go.addEventListener('click', async () => {
    go.disabled = true;
    try {
      await post('library/update', {path: item.path, keep_name: box.checked});
      toast('Update downloading: see Downloads above', 'ok');
      form.remove();
      refreshState().then(() => renderDownloads(state.downloads || []));
    } catch (error) { go.disabled = false; toast(error.message, 'error'); }
  });
  form.append(keep, el('p', 'hint', 'The previous file is removed only after every new file passes source SHA-256 verification and registration. Otherwise it is retained.'), el('div', 'action-row'));
  form.lastChild.append(go, cancel);
  row.append(form);
}

function linkPrompt(item, row) {
  row.querySelector('.inline-form')?.remove();
  const form = el('form', 'inline-form');
  const input = el('input');
  input.type = 'url';
  input.required = true;
  input.placeholder = 'Paste the civitai.com/models/… or huggingface.co/… link';
  const go = el('button', 'secondary-button small', 'Link');
  form.append(input, go);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    go.disabled = true;
    try {
      await post('library/link', {path: item.path, url: input.value});
      toast('Linked. Checking for updates…', 'ok');
      setTimeout(loadLibrary, 1500);
    } catch (error) { go.disabled = false; toast(error.message, 'error'); }
  });
  row.append(form);
  input.focus();
}

$('check').addEventListener('click', async () => {
  await post('library/check', {});
  setTimeout(pollTask, 600);
});
$('identify').addEventListener('click', async () => {
  const data = await post('library/identify', {compute: false});
  toast(data.started ? `Looking up ${data.started} files…` : 'Nothing to identify without hashing; use Identify on a file');
  setTimeout(pollTask, 600);
});

async function loadOlds() {
  const data = await api('library/olds');
  state.hasOlds = data.olds.length > 0;
  $('olds-card').hidden = !state.hasOlds || state.sub !== 'updates';
  $('olds').replaceChildren(...data.olds.map(old => {
    const row = el('div', 'lib-row');
    row.append(el('span', '', `models/${old.path} · ${size(old.size)}`));
    const remove = el('button', 'link-button danger', 'Delete');
    remove.type = 'button';
    remove.addEventListener('click', async () => {
      if (!confirm(`Delete ${old.path}?`)) return;
      await api('library/old?' + new URLSearchParams({path: old.path}), {method: 'DELETE'});
      loadOlds();
    });
    row.append(remove);
    return row;
  }));
}

// ---- settings ------------------------------------------------------------------------------

let settingsFilled = false;
function fillSettings(values) {
  settingsFilled = true;
  $('auto-check').checked = values.auto_check;
  $('blur-nsfw').checked = values.blur_nsfw;
  $('concurrency').value = values.concurrency;
  $('civitai-saved').textContent = values.civitai_key ? '· saved' : '· not set';
  $('hf-saved').textContent = values.hf_token ? '· saved' : '· not set';
}
$('save-settings').addEventListener('click', async () => {
  const body = {auto_check: $('auto-check').checked, blur_nsfw: $('blur-nsfw').checked, concurrency: Number($('concurrency').value)};
  if ($('civitai-key').value.trim()) body.civitai_key = $('civitai-key').value.trim();
  if ($('hf-token').value.trim()) body.hf_token = $('hf-token').value.trim();
  try {
    const data = await api('settings', {method: 'PUT', headers: {'content-type': 'application/json'}, body: JSON.stringify(body)});
    $('civitai-key').value = $('hf-token').value = '';
    fillSettings(data.settings);
    state.settings = data.settings;
    $('settings-state').textContent = 'Saved.';
  } catch (error) { $('settings-state').textContent = error.message; }
});

// ---- hardware -------------------------------------------------------------------------------

const BITS = [['F16 / BF16', 16], ['Q8_0 / FP8', 8.5], ['Q6_K', 6.6], ['Q5_K_M', 5.7], ['Q4_K_M', 4.8], ['IQ3_M', 3.7]];
function renderHardware() {
  const hw = state.hw;
  if (!hw) return;
  $('hw-when').textContent = `Scanned ${ago(hw.scanned_at)}`;
  const cards = (hw.gpus || []).map(gpu => {
    const box = el('div', 'hw-card');
    box.append(el('span', 'hint', gpu.display ? 'GPU · drives the display' : 'GPU'), el('strong', '', gpu.name));
    const bar = el('div', 'bar');
    const fill = el('span');
    fill.style.width = `${Math.round(gpu.vram_used / gpu.vram * 100)}%`;
    bar.append(fill);
    box.append(bar, el('p', 'hint', `${(gpu.vram_used / GB).toFixed(1)} of ${Math.round(gpu.vram / GB)} GB in use · ${gpu.arch || 'unknown arch'}${gpu.display ? ' · 1.5 GB kept for the desktop' : ''}`));
    return box;
  });
  const ram = el('div', 'hw-card');
  ram.append(el('span', 'hint', 'System memory'), el('strong', '', `${Math.round(hw.ram / GB)} GB RAM`),
    el('p', 'hint', `${Math.round(hw.ram_available / GB)} GB free · ${hw.cpu} (${hw.cores} threads)`));
  const disk = Object.entries(hw.disks || {}).map(([label, d]) => {
    const box = el('div', 'hw-card');
    box.append(el('span', 'hint', label), el('strong', '', `${Math.round(d.free / GB)} GB free`), el('p', 'hint', `of ${Math.round(d.total / GB)} GB${d.nvme ? ' · NVMe' : ''}`));
    return box;
  });
  $('hw').replaceChildren(...cards, ram, ...disk);
  if (!cards.length) $('hw').prepend(el('p', 'empty', 'No GPU visible to this service.'));

  const usable = Math.max(0, ...(hw.gpus || []).map(g => g.usable));
  const total = (hw.gpus || []).reduce((n, g) => n + g.usable, 0);
  const table = el('table', 'fits');
  const head = el('tr');
  ['Format', 'LLM (fully on GPU)', (hw.gpus || []).length > 1 ? 'LLM (all GPUs)' : null, 'Image model', 'Video model']
    .filter(Boolean).forEach(text => head.append(el('th', '', text)));
  table.append(head);
  const params = (budget, bits, factor, extra) => Math.max(0, (budget / GB - extra) / factor / (bits / 8));
  BITS.forEach(([label, bits]) => {
    const tr = el('tr');
    tr.append(el('td', '', label));
    const cells = [params(usable, bits, 1.1, 1.5), (hw.gpus || []).length > 1 ? params(total, bits, 1.1, 1.5) : null,
      params(usable, bits, 1, 3), params(usable, bits, 1, 7)];
    cells.filter(v => v !== null).forEach(v => tr.append(el('td', '', v >= 1 ? `≈ ${v >= 10 ? Math.round(v) : v.toFixed(1)}B params` : '—')));
    table.append(tr);
  });
  $('fit-table').replaceChildren(table, el('p', 'hint', 'LLM numbers assume an 8k context; long contexts need more. Diffusion text encoders and VAEs load separately (ComfyUI swaps them in and out).'));
}
$('rescan').addEventListener('click', async () => {
  $('rescan').disabled = true;
  state.hw = await post('hardware/scan');
  $('rescan').disabled = false;
  renderHardware();
  refreshState();
});

// ---- drafter / vision head search (Llama → Parameters → Find drafter models / Find vision heads) -----------
// The Parameters tab opens /Models/?drafter={id, name, family, stem, arch, mtp_head} or ?vision={…, has_vision}:
// a bar with one chip per kind, best kind first, each a Hugging Face GGUF search for files made for that model.
const DRAFTER_KINDS = [
  ['mtp', 'MTP drafter', 'MTP', 'A multi-token-prediction drafter made for this exact model: the highest acceptance rate. Draft type: MTP.'],
  ['dflash', 'DFlash', 'DFlash', 'A DFlash block drafter trained for this model. Draft type: DFlash.'],
  ['eagle3', 'EAGLE-3', 'eagle3', 'An EAGLE-3 speculator trained for this model. Draft type: EAGLE-3.'],
  ['small', 'Small same-family model', '', 'A 0.5–2 B model of the same family (same tokenizer), about a tenth of its size or less. Draft type: Draft model.'],
];
const VISION_KINDS = [
  ['mmproj', 'Vision head (mmproj)', 'mmproj', 'Repos with an mmproj made for this model: open one and get a file from its "Vision heads (mmproj)" section.'],
  ['gguf', 'GGUF repos of this model', 'GGUF', 'Most GGUF repos ship their mmproj files next to the quants: look in "Vision heads (mmproj)" on the model page.'],
  ['family', 'Same family', 'mmproj', 'Heads from other finetunes of the same family: one with the same width as this model loads (Llama ★-marks those).'],
];
let drafterContext = null;
const searchKinds = () => drafterContext.mode === 'vision' ? VISION_KINDS : DRAFTER_KINDS;

function drafterQuery(kind) {
  const c = drafterContext;
  const stem = c.stem || c.family || c.name || '';
  const word = searchKinds().find(k => k[0] === kind)[2];
  if (kind === 'family') return `${c.family || stem} ${word}`.trim();
  return kind === 'small' ? (c.family || stem) : `${stem} ${word}`.trim();
}

function pickDrafterKind(kind, run = true) {
  state.source = 'hf';
  $('source').querySelectorAll('[data-source]').forEach(b => b.setAttribute('aria-checked', String(b.dataset.source === 'hf')));
  state.category = 'llm';
  $('category').querySelectorAll('[data-category]').forEach(b => b.setAttribute('aria-checked', String(b.dataset.category === 'llm')));
  $('gguf').checked = true;
  $('sort').value = 'downloads';
  $('q').value = drafterQuery(kind);
  $('drafter-bar').querySelectorAll('[data-drafter]').forEach(b => b.setAttribute('aria-checked', String(b.dataset.drafter === kind)));
  const where = drafterContext.mode === 'vision' ? 'Vision head' : 'Draft model';
  $('drafter-hint').textContent = searchKinds().find(k => k[0] === kind)[3]
    + ` Download it, then pick it in Llama → ${drafterContext.name || drafterContext.id} → Parameters → ${where}.`;
  if (run) search(true);
}

function startDrafterSearch() {
  const params = new URLSearchParams(location.search);
  const mode = params.get('vision') ? 'vision' : 'drafter';
  const raw = params.get(mode);
  if (!raw) return false;
  try { drafterContext = JSON.parse(raw); } catch (_) { return false; }
  if (!drafterContext || typeof drafterContext !== 'object') return false;
  drafterContext.mode = mode;
  history.replaceState(history.state, '', location.pathname + '#models');
  // With its own MTP head the model needs no MTP download; the next best drafter is DFlash.
  const best = mode === 'vision' ? 'mmproj' : drafterContext.mtp_head > 0 ? 'dflash' : 'mtp';
  const bar = $('drafter-bar');
  const title = document.createElement('strong');
  title.textContent = `${mode === 'vision' ? 'Vision heads' : 'Draft models'} for ${drafterContext.name || drafterContext.id}`;
  const chips = document.createElement('div');
  chips.className = 'chips';
  chips.setAttribute('role', 'radiogroup');
  chips.setAttribute('aria-label', mode === 'vision' ? 'Where to look' : 'Kind of drafter');
  for (const [kind, label] of searchKinds()) {
    const chip = document.createElement('button');
    chip.type = 'button'; chip.className = 'chip'; chip.setAttribute('role', 'radio'); chip.dataset.drafter = kind;
    chip.textContent = (kind === best ? '★ ' : '') + label;
    if (kind === best) chip.title = 'Recommended for this model';
    chip.addEventListener('click', () => pickDrafterKind(kind));
    chips.append(chip);
  }
  const close = document.createElement('button');
  close.type = 'button'; close.className = 'drafter-close'; close.textContent = '✕';
  close.title = mode === 'vision' ? 'Close the vision head search' : 'Close the drafter search';
  close.addEventListener('click', () => { bar.hidden = true; drafterContext = null; });
  const hint = document.createElement('p');
  hint.className = 'hint'; hint.id = 'drafter-hint';
  const note = document.createElement('p');
  note.className = 'hint';
  note.textContent = mode === 'vision'
    ? (drafterContext.has_vision ? 'This model already has a vision head; another one only helps if it is a better precision or adds audio.'
      : 'This model has no vision head yet: it reads text only.')
    : drafterContext.mtp_head > 0 ? '★ This model already has its own MTP head: Draft type MTP needs no download. A DFlash drafter may draft further.' : '';
  bar.replaceChildren(title, chips, close, hint, ...(note.textContent ? [note] : []));
  bar.hidden = false;
  pickDrafterKind(best, false);
  return true;
}

// ---- start --------------------------------------------------------------------------------------

(async () => {
  // Bases (LoRA filter) and the Core Backend scan (~1.3 s at the host) load alongside the first
  // view instead of before it: the first page of results only waits for the download state.
  detectHermesTab();
  loadBases();
  await refreshState();
  let start = startDrafterSearch() ? 'models' : location.hash.slice(1);
  // Links straight to a Manager sub-tab (the home page's Updates chip uses #updates).
  if (['updates', 'installed', 'downloads', 'settings', 'core'].includes(start)) { state.sub = start; start = 'manager'; }
  setView(['models', 'loras', 'hermes', 'manager', 'hardware'].includes(start) ? start : 'models', {push: false});
  scanCoreOnRefresh();
  setInterval(() => { if (!document.hidden) refreshState(); }, 3000);
})();
