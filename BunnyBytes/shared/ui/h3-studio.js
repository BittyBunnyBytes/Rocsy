/* Shared accessible dialog helper for studio generation details. */
(() => {
  function close(dialog) {
    if (!dialog) return;
    dialog.hidden = true;
  }
  function open(dialog) {
    if (typeof dialog === 'string') dialog = document.querySelector(dialog);
    if (!dialog) return;
    dialog.hidden = false;
    dialog.querySelector('[data-studio-dialog-close]')?.focus();
  }
  document.addEventListener('click', (event) => {
    const closeButton = event.target.closest('[data-studio-dialog-close]');
    if (closeButton) return close(closeButton.closest('[data-studio-dialog]'));
    if (event.target.matches('[data-studio-dialog]')) close(event.target);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') close(document.querySelector('[data-studio-dialog]:not([hidden])'));
  });
  const paths = {
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/>',
    download: '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
    retry: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
    cancel: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
    copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
  };
  const icon = (name) => paths[name]
    ? `<svg class="studio-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name]}</svg>`
    : '';
  window.BunnyStudioDialog = {open, close};
  window.BunnyStudioIcons = {icon};
})();

/* Shared H3-style mobile Create/Generations navigation for Music and Image. */
(() => {
  const body = document.body;
  const tabs = [...document.querySelectorAll('.mobile-tab[data-mobile-tab]')];
  const list = document.querySelector('#jobs-list');
  if (!tabs.length || !list) return;
  const key = `${body.dataset.appPrefix || location.pathname}:mobile-tab`;
  const scroll = {create: 0, jobs: 0};
  function setTab(next) {
    if (next !== 'create' && next !== 'jobs') return;
    const previous = body.dataset.mobileTab || 'create';
    scroll[previous] = window.scrollY;
    body.dataset.mobileTab = next;
    for (const tab of tabs) {
      const active = tab.dataset.mobileTab === next;
      tab.classList.toggle('is-active', active);
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
    }
    try { sessionStorage.setItem(key, next); } catch { /* storage may be disabled */ }
    window.scrollTo(0, scroll[next] || 0);
  }
  for (const tab of tabs) {
    tab.addEventListener('click', () => setTab(tab.dataset.mobileTab));
    tab.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      const next = tab.dataset.mobileTab === 'create' ? 'jobs' : 'create';
      setTab(next);
      tabs.find((item) => item.dataset.mobileTab === next)?.focus();
    });
  }
  try { if (sessionStorage.getItem(key) === 'jobs') setTab('jobs'); } catch { /* storage may be disabled */ }
  const badge = document.querySelector('#mobile-jobs-badge');
  if (badge) {
    const updateBadge = () => {
      const active = list.querySelectorAll('.job-state.state-waiting_for_gpu,.job-state.state-preparing,.job-state.state-queued,.job-state.state-enhancing,.job-state.state-rendering,.job-state.state-cancelling').length;
      badge.textContent = active ? String(active) : '';
    };
    new MutationObserver(updateBadge).observe(list, {childList: true, subtree: true});
    updateBadge();
  }

  // H3-style infinite history: the studios own paging and their Show more
  // buttons; this shared layer activates the next page near the scroll end.
  const pager = document.querySelector('#jobs-pager');
  if (pager) {
    let queued = false;
    const maybeLoadMoreJobs = () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        const button = pager.querySelector('#jobs-more');
        if (pager.hidden || !button || !pager.getClientRects().length) return;
        const ownScroll = getComputedStyle(list).overflowY === 'auto';
        const height = ownScroll ? list.clientHeight : window.innerHeight;
        const remaining = ownScroll
          ? list.scrollHeight - list.scrollTop - list.clientHeight
          : pager.getBoundingClientRect().top - window.innerHeight;
        if (remaining < height * 1.5) button.click();
      });
    };
    new MutationObserver(maybeLoadMoreJobs).observe(pager, {childList: true, attributes: true, attributeFilter: ['hidden']});
    list.addEventListener('scroll', maybeLoadMoreJobs, {passive: true});
    window.addEventListener('scroll', maybeLoadMoreJobs, {passive: true});
    window.addEventListener('resize', maybeLoadMoreJobs, {passive: true});
    tabs.forEach((tab) => tab.addEventListener('click', maybeLoadMoreJobs));
    maybeLoadMoreJobs();
  }
})();

/* Gallery image loading and tiny reusable icons shared by H3 and Image. */
(() => {
  const supportsObserver = 'IntersectionObserver' in window;
  const pending = supportsObserver ? new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const image = entry.target;
      pending.unobserve(image);
      const source = image.dataset.gallerySrc;
      if (!source) continue;
      image.src = source;
      delete image.dataset.gallerySrc;
    }
  }, {rootMargin: '320px 0px'}) : null;

  function imageAttrs(source, eager = false) {
    if (eager || !supportsObserver) return `src="${source}" loading="${eager ? 'eager' : 'lazy'}" decoding="async"${eager ? ' fetchpriority="high"' : ''}`;
    return `data-gallery-src="${source}" loading="lazy" decoding="async" fetchpriority="low"`;
  }

  function observe(root) {
    for (const image of root.querySelectorAll('img[data-gallery-src]')) {
      if (pending) pending.observe(image);
      else {
        image.src = image.dataset.gallerySrc;
        delete image.dataset.gallerySrc;
      }
    }
  }

  const sprite = '<svg xmlns="http://www.w3.org/2000/svg" aria-hidden="true" style="position:absolute;width:0;height:0;overflow:hidden"><symbol id="bunny-gallery-play" viewBox="0 0 24 24"><path d="m8 5 11 7-11 7z"/></symbol></svg>';
  if (document.body) document.body.insertAdjacentHTML('afterbegin', sprite);
  const icon = (name) => name === 'play'
    ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" aria-hidden="true"><use href="#bunny-gallery-play"/></svg>'
    : '';
  window.BunnyGalleryMedia = {imageAttrs, observe, icon};
})();

/* Compact phone controls for both LoRA galleries. */
(() => {
  const gallery = document.querySelector('#lora-gallery');
  const tools = gallery?.querySelector('.lora-gallery-tools');
  if (!tools || tools.querySelector('.gallery-mobile-options-toggle')) return;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'secondary-button gallery-mobile-options-toggle';
  button.textContent = 'Options';
  button.setAttribute('aria-expanded', 'false');
  button.setAttribute('aria-label', 'Show gallery filters and upload controls');
  tools.insertBefore(button, tools.children[1] || null);
  const setOpen = (open) => {
    gallery.dataset.galleryMobileOptions = open ? 'open' : 'closed';
    button.setAttribute('aria-expanded', String(open));
    button.setAttribute('aria-label', `${open ? 'Hide' : 'Show'} gallery filters and upload controls`);
  };
  button.addEventListener('click', () => setOpen(gallery.dataset.galleryMobileOptions !== 'open'));
  new MutationObserver(() => {
    if (!gallery.hidden) setOpen(false);
  }).observe(gallery, {attributes: true, attributeFilter: ['hidden']});
  setOpen(false);
})();

/* Video's References card as a shared component (used by Image).
   The studio owns its reference state and uploads; this renders the H3 row
   markup (thumbnail, token chip, meta line, extras, remove) and the totals,
   wires the drop zone, and inserts tokens at the prompt cursor. */
(() => {
  const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

  // item: {key, thumb, token, insertToken, meta, metaTitle, extras (HTML), category, busy, removeLabel}
  function rowHtml(item) {
    const key = escape(item.key);
    const thumb = item.thumb
      ? `<img class="reference-thumb" data-view-reference="${key}" src="${escape(item.thumb)}" alt="" loading="lazy">`
      : `<span class="reference-thumb reference-thumb-icon" aria-hidden="true"></span>`;
    const token = item.insertToken
      ? `<button type="button" class="reference-token" data-category="${escape(item.category || 'image')}" data-insert-token="${escape(item.insertToken)}" title="Insert ${escape(item.insertToken)} into the prompt">${escape(item.token)}</button>`
      : `<span class="reference-token is-label" data-category="${escape(item.category || 'image')}">${escape(item.token)}</span>`;
    return `
    <div class="reference-row${item.busy ? ' is-busy' : ''}${item.off ? ' is-off' : ''}" data-reference-id="${key}">
      ${thumb}
      <div class="reference-info">
        ${token}
        ${item.meta ? `<span class="reference-file-name" title="${escape(item.metaTitle || '')}">${escape(item.meta)}</span>` : ''}
        ${item.extras || ''}
      </div>
      ${item.after || ''}
      <button type="button" class="icon-button reference-remove" data-remove-reference="${key}" aria-label="${escape(item.removeLabel || `Remove ${item.token}`)}">&#10005;</button>
    </div>`;
  }

  function render(list, items) { list.innerHTML = items.map(rowHtml).join(''); }

  function renderTotals(node, lines) {
    node.innerHTML = lines.map((line) => `<span class="reference-total">${escape(line)}</span>`).join('');
  }

  function insertToken(textarea, token) {
    const start = textarea.selectionStart ?? textarea.value.length;
    const end = textarea.selectionEnd ?? textarea.value.length;
    const needsSpace = start > 0 && !/\s$/.test(textarea.value.slice(0, start));
    textarea.setRangeText(`${needsSpace ? ' ' : ''}${token}`, start, end, 'end');
    textarea.focus();
    textarea.dispatchEvent(new Event('input', {bubbles: true}));
  }

  // Clicks on the list: token -> onInsert, thumbnail -> onView, x -> onRemove.
  function bindList(list, {onInsert, onView, onRemove}) {
    list.addEventListener('click', (event) => {
      const insert = event.target.closest('[data-insert-token]');
      if (insert) return onInsert?.(insert.dataset.insertToken);
      const view = event.target.closest('[data-view-reference]');
      if (view) return onView?.(view.dataset.viewReference);
      const remove = event.target.closest('[data-remove-reference]');
      if (remove) return onRemove?.(remove.dataset.removeReference);
    });
  }

  // H3's drop zone; `area` (default: the zone) also accepts drops.
  // onFiles(files, rowKey) - rowKey is set when dropped onto a reference row.
  function bindDropZone(zone, {area = zone, accept = (file) => file.type.startsWith('image/'), onFiles}) {
    let depth = 0;
    const hasFiles = (event) => [...(event.dataTransfer?.types || [])].includes('Files');
    const clear = () => { depth = 0; zone.classList.remove('is-dragging'); area.querySelectorAll('.reference-row.is-dragging').forEach((row) => row.classList.remove('is-dragging')); };
    area.addEventListener('dragenter', (event) => { if (hasFiles(event)) { depth += 1; zone.classList.add('is-dragging'); } });
    area.addEventListener('dragover', (event) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      const row = event.target.closest('.reference-row');
      area.querySelectorAll('.reference-row').forEach((item) => item.classList.toggle('is-dragging', item === row));
    });
    area.addEventListener('dragleave', (event) => { if (hasFiles(event) && (depth = Math.max(0, depth - 1)) === 0) clear(); });
    area.addEventListener('drop', (event) => {
      const files = [...(event.dataTransfer?.files || [])].filter(accept);
      const row = event.target.closest('.reference-row');
      clear();
      if (!files.length) return;
      event.preventDefault();
      onFiles(files, row?.dataset.referenceId ?? null);
    });
    zone.addEventListener('click', () => zone.dispatchEvent(new CustomEvent('reference-pick', {bubbles: true})));
  }

  window.BunnyReferences = {rowHtml, render, renderTotals, insertToken, bindList, bindDropZone};
})();
