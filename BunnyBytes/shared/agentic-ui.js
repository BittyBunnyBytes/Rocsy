/* Shared Image, Music and Voice Agentic mode UI (Video parity). The studio proxies
   every request to the internal framework (BunnyBytes/shared/agentic.py):
   - Studio card: Agentic queue, Hermes instructions, approval, active sessions.
   - Prompt syntax helper under the prompt that follows the loaded model.
   - Hermes settings tab (gear): model, thinking, sampling, run switches, the per-model
     guides Hermes is given, prompts, message preview, and the Hermes terminal (live log,
     steering, / commands). */
(() => {
  const root = document.body;
  const mount = root.dataset.appPrefix || location.pathname;
  const studio = root.dataset.studio || (mount.startsWith('/Image') ? 'image' : mount.startsWith('/Music') ? 'music' : 'voice');
  const prefix = root.dataset.appPrefix ?? (studio === 'image' ? '/Image' : studio === 'music' ? '/Music' : '/Voice');
  const API = `${prefix}/api/agentic`;
  const LABEL = { image: 'Image', music: 'Music', voice: 'Voice' }[studio];
  const ACTIVE = new Set(['planning', 'reviewing_plan', 'queued', 'rendering', 'reviewing_output', 'retrying']);
  const FINISHED = new Set(['complete', 'failed', 'cancelled', 'interrupted']);
  const el = id => document.getElementById(id);
  const value = id => el(id)?.value || '';
  const drafts = new Map();
  let settings = null;
  let guides = null;
  let loadedModel = '';
  let listTimer = null;
  let refreshing = false;
  let sessions = [];

  function h(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, val] of Object.entries(props)) {
      if (val === undefined || val === null || val === false) continue;
      if (key === 'class') node.className = val;
      else if (key === 'text') node.textContent = val;
      else if (key === 'value') node.value = val;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), val);
      else if (key in node && typeof val !== 'string') node[key] = val;
      else node.setAttribute(key, val === true ? '' : val);
    }
    node.append(...children.flat().filter(child => child !== null && child !== undefined && child !== false));
    return node;
  }
  async function call(path = '', init = {}) {
    const response = await fetch(API + path, { credentials: 'same-origin', cache: 'no-store', ...init });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${response.status}`);
    return data;
  }
  const send = (method, path, body) => call(path, { method, headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body) });
  const setStatus = text => { const node = el('agentic-status'); if (node) node.textContent = text; };
  const stateText = state => state === 'awaiting_feedback' ? 'your call' : String(state || '').replaceAll('_', ' ');

  // ── which generation model the studio has loaded ──────────────────────────
  function currentModel() {
    if (studio === 'image') return document.querySelector('input[name="model"]:checked')?.value || 'krea2';
    if (studio === 'music') return value('model') || 'yue2';
    return value('engine') || 'qwen3';
  }
  const modelLabel = key => guides?.models?.[key]?.label || key;

  // ── request payload from the Studio form ─────────────────────────────────
  function payload() {
    const common = { guide: value('agentic-guide').trim(), approve_plan: Boolean(el('agentic-approve-plan')?.checked) };
    if (studio === 'image') {
      const model = currentModel();
      const form = { model, checkpoint: model === 'anima' ? undefined : value('checkpoint') || undefined, width: Number(value('width')) || 1024, height: Number(value('height')) || 1024,
        count: Number(value('batch-count')) || 1, speed: value('speed'),
        enhance: Boolean(el('enhance')?.checked), use_references: Boolean(el('use-references')?.checked) };
      if (model === 'anima') {
        form.anima_version = value('anima-version');
        form.anima_cfg = Number(value('anima-cfg'));
        form.anima_steps = Number(value('anima-steps')) || undefined;
        if (value('anima-negative-prompt').trim()) form.negative_prompt = value('anima-negative-prompt').trim();
      } else if (model === 'qwen_image_21') {
        form.steps = Number(value('steps'));
        form.cfg = Number(value('cfg'));
        form.negative_prompt = value('negative-prompt');
        form.transparent = Boolean(el('transparent')?.checked);
      } else {
        form.turbo_strength = Number(value('turbo-strength')) || 1;
        form.krea_steps = Number(value('krea-steps')) || undefined;
      }
      if (model !== 'anima') {
        form.loras = Array.from(document.querySelectorAll('#lora-list .lora-row'))
          .sort((a, b) => Number(a.dataset.loraIndex) - Number(b.dataset.loraIndex))
          .map(row => ({ enabled: Boolean(row.querySelector('.lora-enabled')?.checked),
            strength: Number(row.querySelector('.lora-strength')?.value || 1) }));
        try { form.references = JSON.parse(localStorage.getItem('krea.references') || '[]')
          .slice(0, model === 'qwen_image_21' ? 10 : 4)
          .map(ref => ({ enabled: Boolean(ref.enabled && ref.image), strength: Number(ref.strength || 1),
            image: ref.image || null, width: ref.width || undefined, height: ref.height || undefined })); }
        catch { form.references = []; }
      }
      return { ...common, request: value('prompt').trim() || common.guide, settings: form };
    }
    if (studio === 'music') {
      const form = {};
      document.querySelectorAll('#generate-form [data-request]').forEach(field => {
        if (field.type === 'checkbox') form[field.name] = field.checked;
        else if (field.type === 'number' || field.type === 'range') {
          if (field.value.trim()) form[field.name] = field.name.includes('seed') ? field.value.trim() : Number(field.value);
        } else form[field.name] = field.value;
      });
      delete form.style; delete form.lyrics;
      const direction = value('style').trim() || common.guide;
      return { ...common, request: direction + (value('lyrics').trim() ? '\n\nExact lyrics:\n' + value('lyrics').trim() : ''),
        settings: form, locked_lyrics: value('lyrics').trim() };
    }
    return { ...common, request: value('text').trim() || common.guide,
      settings: { engine: value('engine'), voice_id: value('voice'), language: value('language') || 'English' } };
  }

  // ── prompt syntax helper (follows the loaded model) ──────────────────────
  function syntaxAnchor() {
    const field = studio === 'image' ? el('prompt') : studio === 'music' ? el('lyrics') : el('text');
    return field?.closest('.field-row, .field') || field;
  }
  function renderSyntax() {
    const anchor = syntaxAnchor();
    if (!anchor || !guides) return;
    let box = el('agentic-syntax');
    if (!box) {
      box = h('details', { id: 'agentic-syntax', class: 'agentic-syntax' });
      try { box.open = localStorage.getItem(`agentic.syntax.${studio}`) === '1'; } catch { /* storage blocked */ }
      box.addEventListener('toggle', () => { try { localStorage.setItem(`agentic.syntax.${studio}`, box.open ? '1' : '0'); } catch { /* ignore */ } });
      anchor.after(box);
    }
    const guide = guides.models[loadedModel];
    box.replaceChildren(
      h('summary', {}, h('span', { text: 'Prompt syntax' }), h('b', { text: guide?.label || loadedModel })),
      guide?.tips?.length ? h('ul', {}, guide.tips.map(tip => h('li', { text: tip })))
        : h('p', { class: 'agentic-muted', text: 'No guide for this model yet.' }),
      h('button', { type: 'button', class: 'agentic-link', text: 'Full guide Hermes uses →',
        onclick: () => openView('guide') }));
  }
  function watchModel() {
    const next = currentModel();
    if (next === loadedModel) return;
    loadedModel = next;
    renderSyntax();
    if (el('agentic-view') && !el('agentic-view').hidden) selectGuide(loadedModel);
    const badge = el('agentic-loaded'); if (badge) badge.textContent = modelLabel(loadedModel);
  }

  // ── studio panel ─────────────────────────────────────────────────────────
  function planText(row) {
    const plan = row.plan || {};
    if (studio === 'image') return [plan.prompt, plan.negative_prompt ? `Negative: ${plan.negative_prompt}` : '',
      plan.chosen_loras?.length ? `LoRAs: ${plan.chosen_loras.map(lora => `${lora.label} @ ${lora.strength}`).join(', ')}` : ''].filter(Boolean).join('\n\n');
    if (studio === 'music') return [plan.style, row.settings?.locked_lyrics || plan.lyrics].filter(Boolean).join('\n\n');
    return plan.text || '';
  }
  function outputsOf(row) {
    return (row.outputs || []).map(url => studio === 'image'
      ? h('a', { href: url, target: '_blank', rel: 'noopener', class: 'agentic-thumb' }, h('img', { src: url, alt: 'Agentic result', loading: 'lazy' }))
      : h('audio', { controls: true, preload: 'none', src: url }));
  }
  // A rendered image waits for the user's verdict: approve / disapprove render again from the notes, keep ends the session.
  function feedbackControls(row, after) {
    const notes = h('textarea', { rows: 2, maxLength: 4000, placeholder: 'What should change? (needed for Disapprove, optional for Approve)',
      'aria-label': 'What should change in the image', 'data-agentic-draft': row.id, value: drafts.get(row.id) || '',
      oninput: event => drafts.set(row.id, event.target.value) });
    const status = h('small', { class: 'agentic-error' });
    const decide = async action => {
      if (action === 'disapprove' && !notes.value.trim()) { status.textContent = 'Say what should change first.'; notes.focus(); return; }
      buttons.forEach(button => { button.disabled = true; });
      try { await send('POST', `/sessions/${row.id}/feedback`, { action, notes: notes.value.trim() }); drafts.delete(row.id); await after(); }
      catch (error) { status.textContent = error.message; buttons.forEach(button => { button.disabled = false; }); }
    };
    const buttons = [
      h('button', { type: 'button', class: 'primary-button agentic-approve', text: '👍 Approve', title: 'Keep this image and render the next version with your changes', onclick: () => decide('approve') }),
      h('button', { type: 'button', class: 'secondary-button agentic-disapprove', text: '👎 Disapprove', title: 'Reject this image; Hermes re-plans from your notes', onclick: () => decide('disapprove') }),
      h('button', { type: 'button', class: 'secondary-button agentic-keep', text: 'Keep & end', title: 'Keep this image and end the session', onclick: () => decide('keep') }),
    ];
    return h('div', { class: 'agentic-feedback' }, notes, h('div', { class: 'agentic-actions' }, ...buttons), status);
  }
  function renderList() {
    const list = el('agentic-list');
    if (!list) return;
    // Only live work (planning/queued/rendering/awaiting you); finished, failed and cancelled sessions stay in the Hermes terminal picker.
    const live = sessions.filter(row => !FINISHED.has(row.state)).slice(0, 6);
    const runs = el('agentic-runs');
    const badge = el('agentic-queue-badge');
    if (runs) runs.hidden = !live.length;
    if (badge) { badge.hidden = !live.length; badge.textContent = `${live.length} active`; }
    const count = el('agentic-queue-count');
    if (count) count.textContent = `${live.length} run${live.length === 1 ? '' : 's'}`;
    keepPlace(list, () => {
      // Keyed update: an item is rebuilt only when what it shows changes (not on every 3 s poll), so
      // images don't reload, typed notes and open details stay, and nothing blinks.
      const existing = new Map([...list.querySelectorAll(':scope > .agentic-item')].map(node => [node.dataset.id, node]));
      let previous = null;
      for (const row of live) {
        const key = itemKey(row);
        let node = existing.get(row.id);
        existing.delete(row.id);
        if (!node || node.dataset.key !== key) {
          const fresh = sessionItem(row);
          fresh.dataset.key = key;
          if (node) {
            // Keep already-loaded thumbnails instead of fetching them again.
            for (const thumb of fresh.querySelectorAll('.agentic-thumb')) {
              const old = [...node.querySelectorAll('.agentic-thumb')].find(link => link.getAttribute('href') === thumb.getAttribute('href'));
              if (old) thumb.replaceWith(old);
            }
            const focused = node.contains(document.activeElement) ? document.activeElement : null;
            const field = fresh.querySelector('textarea[data-agentic-draft]');
            node.replaceWith(fresh);
            if (focused?.dataset?.agenticDraft && field) { field.focus(); field.setSelectionRange(focused.selectionStart, focused.selectionEnd); }
          }
          node = fresh;
        }
        const stage = node.querySelector('.agentic-stage');
        if (stage.textContent !== (row.stage || '')) stage.textContent = row.stage || '';
        const slot = previous ? previous.nextElementSibling : list.firstElementChild;
        if (slot !== node) list.insertBefore(node, slot);
        previous = node;
      }
      existing.forEach(node => node.remove());
      const empty = list.querySelector(':scope > .agentic-empty');
      if (live.length) empty?.remove();
      else if (!empty) list.append(h('p', { class: 'agentic-muted agentic-empty', text: 'No active or queued Agentic sessions.' }));
    });
  }
  // Everything an item shows except the stage line, which is patched in place.
  const itemKey = row => JSON.stringify([row.state, row.loaded_model, row.request, row.error, row.outputs,
    row.plan && (row.state === 'awaiting_approval' || row.state === 'complete') ? planText(row) : '']);
  // When the sessions box changes height while its top is scrolled above the screen, scroll by the
  // same amount so what you're looking at (prompt, LoRAs…) doesn't jump.
  function keepPlace(node, change) {
    const before = node.getBoundingClientRect();
    change();
    if (before.top >= 0) return;
    const delta = node.getBoundingClientRect().height - before.height;
    if (delta) window.scrollBy(0, delta);
  }
  function sessionItem(row) {
    const item = h('div', { class: 'agentic-item', 'data-state': row.state, 'data-id': row.id },
      h('div', { class: 'agentic-item-head' },
        h('span', { class: 'agentic-badge', 'data-state': row.state, text: stateText(row.state) }),
        h('span', { class: 'agentic-item-model', text: modelLabel(row.loaded_model || '') }),
        h('button', { type: 'button', class: 'agentic-link', text: 'Terminal', title: 'Open this session in the Hermes terminal',
          onclick: () => openView('terminal', row.id) })),
      h('small', { class: 'agentic-stage', text: row.stage || '' }),
      h('small', { class: 'agentic-muted', text: (row.request || '').slice(0, 140) }),
      row.error ? h('small', { class: 'agentic-error', text: row.error }) : null);
    if (row.plan && (row.state === 'awaiting_approval' || row.state === 'complete')) {
      item.append(h('details', { class: 'agentic-plan', open: row.state === 'awaiting_approval' },
        h('summary', { text: 'Hermes plan' }), h('pre', { text: planText(row) })));
    }
    if (row.state === 'awaiting_approval') {
      const notes = h('textarea', { rows: 2, maxLength: 4000, placeholder: 'Changes for Hermes before rendering…',
        'aria-label': 'Plan revision notes', 'data-agentic-draft': row.id, value: drafts.get(row.id) || '',
        oninput: event => drafts.set(row.id, event.target.value) });
      const decide = async action => {
        try { await send('POST', `/sessions/${row.id}/plan`, { action, notes: notes.value.trim() }); drafts.delete(row.id); await refresh(); }
        catch (error) { setStatus(error.message); }
      };
      item.append(notes, h('div', { class: 'agentic-actions' },
        h('button', { type: 'button', class: 'primary-button', text: 'Approve and render', onclick: () => decide('approve') }),
        h('button', { type: 'button', class: 'secondary-button', text: 'Revise plan', onclick: () => decide('revise') })));
    }
    item.append(...outputsOf(row));
    if (row.state === 'awaiting_feedback') item.append(feedbackControls(row, refresh));
    if (!FINISHED.has(row.state) && row.state !== 'awaiting_feedback') {
      item.append(h('button', { type: 'button', class: 'secondary-button agentic-cancel', text: 'Cancel', onclick: async () => {
        try { await send('POST', `/sessions/${row.id}/cancel`, {}); await refresh(); } catch (error) { setStatus(error.message); }
      } }));
    }
    return item;
  }
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      sessions = await call('/sessions');
      renderList();
      if (sessions.some(row => !FINISHED.has(row.state) )) {
        if (!listTimer) listTimer = setInterval(refresh, 3000);
      } else if (listTimer) { clearInterval(listTimer); listTimer = null; }
      refreshPicker();
    } catch (error) { setStatus(error.message); }
    finally { refreshing = false; }
  }
  async function start() {
    const body = payload();
    if (!body.request) { setStatus(studio === 'music' ? 'Enter a music direction or Hermes guide first.' : 'Enter a request or Hermes guide first.'); return; }
    setStatus('Starting Agentic session…');
    try {
      const row = await send('POST', '/sessions', body);
      terminal.session = row.id;
      el('agentic-controls').open = true;
      setStatus(`Hermes is planning with the ${modelLabel(row.loaded_model)} guide`);
      await refresh();
    } catch (error) { setStatus(error.message); }
  }
  function setupPanel() {
    const target = studio === 'voice' ? el('generate')?.parentElement : el('generate-button')?.parentElement;
    if (!target) return;
    const panel = h('details', { id: 'agentic-controls', class: 'card agentic-studio-card agentic-controls is-active' },
      h('summary', { class: 'card-head' },
        h('span', { class: 'card-title', text: 'Agentic' }),
        h('span', { class: 'card-sub agentic-card-sub', text: {
          image: 'Hermes plans, renders and checks images for you',
          music: 'Hermes plans, renders and checks tracks for you',
          voice: 'Hermes plans, renders and checks speech for you' }[studio] }),
        h('span', { id: 'agentic-queue-badge', class: 'agentic-queue-badge', hidden: true }),
        h('span', { class: 'card-chevron', 'aria-hidden': 'true' })),
      h('div', { class: 'card-body' },
        h('div', { id: 'agentic-runs', class: 'agentic-runs', hidden: true },
          h('div', { class: 'agentic-heading' }, h('strong', { text: 'Agentic runs' }),
            h('span', { id: 'agentic-queue-count', class: 'agentic-muted' })),
          h('div', { id: 'agentic-list' })),
        h('label', { for: 'agentic-guide', class: 'agentic-field-label', text: 'Agentic guide / instructions' }),
        h('p', { class: 'agentic-description', text: {
          image: 'Tell Hermes what to make or preserve. If the Prompt is empty, these instructions are the whole request.',
          music: 'Describe the track, structure and lyrics to preserve. If Music direction is empty, these instructions are the whole request.',
          voice: 'Describe the script, tone and pacing. If the Script is empty, these instructions are the whole request.' }[studio] }),
        h('textarea', { id: 'agentic-guide', rows: 4, maxLength: 4000, placeholder: {
          image: 'A warm editorial portrait; keep the same character and lighting.',
          music: 'Keep the chorus uplifting and preserve my lyrics exactly.',
          voice: 'Warm, calm delivery with natural pauses.' }[studio] }),
        h('span', { class: 'agentic-loaded-wrap' }, 'Model guide: ', h('b', { id: 'agentic-loaded', text: modelLabel(loadedModel) })),
        h('label', { class: 'agentic-extra' }, h('input', { type: 'checkbox', id: 'agentic-approve-plan' }),
          h('span', { text: 'Wait for my approval before rendering' })),
        h('div', { class: 'agentic-actions' },
          h('button', { type: 'button', class: 'agentic-queue-button', text: '✦ Queue Agentic run', onclick: start }),
          h('button', { type: 'button', class: 'secondary-button agentic-settings-button', text: '⚙ Agentic settings',
            onclick: () => openView() })),
        h('div', { id: 'agentic-status', role: 'status', 'aria-live': 'polite', text: 'Ready' })));
    target.insertAdjacentElement('afterend', panel);
    try { panel.open = localStorage.getItem(`agentic.card.open.${studio}`) !== '0'; }
    catch { panel.open = true; }
    panel.addEventListener('toggle', () => {
      try { localStorage.setItem(`agentic.card.open.${studio}`, panel.open ? '1' : '0'); }
      catch { /* storage blocked */ }
    });
  }

  // ── Hermes settings tab ──────────────────────────────────────────────────
  const terminal = { session: '', renderedId: '', renderedRevision: -1, follow: true, timer: null, busy: false };
  let saveTimer = null;
  let guideKey = '';

  const field = (label, control, hint) => h('div', { class: 'agentic-field' },
    h('label', { for: control.id, text: label }), control, hint ? h('span', { class: 'agentic-hint', text: hint }) : null);
  const toggleRow = (id, label, hint) => h('label', { class: 'agentic-switch-row', for: id },
    h('span', {}, label, hint ? h('span', { class: 'agentic-hint', text: hint }) : null), h('input', { type: 'checkbox', id }));
  const num = (id, min, max, step) => h('input', { type: 'number', id, min, max, step });

  function buildView() {
    const reviewLabel = { image: ['Hermes reviews the image', 'Sends the finished image to Hermes (needs a vision model); a clear failure re-plans and re-renders'],
      music: ['Check the rendered track', 'Verifies the file has audio, a plausible length and is not silent; a failure stops the run'],
      voice: ['Check the rendered speech', 'Confirms the studio returned an audio file'] }[studio];
    const view = h('section', { id: 'agentic-view', class: 'agentic-view', hidden: true, 'aria-label': 'Hermes settings' },
      h('div', { class: 'agentic-view-top' },
        h('button', { type: 'button', class: 'secondary-button', text: `← Back to ${LABEL}`, onclick: closeView }),
        h('h1', { text: `Hermes · ${LABEL} agent` }),
        h('span', { id: 'agentic-save-state', class: 'agentic-hint', 'aria-live': 'polite' })),
      h('p', { class: 'agentic-hint', text: `Agentic sends your ${LABEL} request to Hermes on the local Llama service with the guide for the model you have loaded, renders through the shared GPU queue, and reviews the result. Same controls as Video's Hermes settings.` }),
      h('div', { class: 'agentic-layout' },
        h('form', { id: 'agentic-settings-form', class: 'agentic-main', autocomplete: 'off', onsubmit: event => event.preventDefault() },
          h('div', { class: 'agentic-card' }, h('h2', { text: 'Hermes model' }),
            h('p', { class: 'agentic-hint', text: 'The local Llama model that plans, reviews and rewrites prompts for this studio.' }),
            field('Llama model', h('select', { id: 'ag-model' }, h('option', { value: '', text: 'Loading Llama models…' }))),
            h('div', { class: 'agentic-actions' }, h('button', { type: 'button', class: 'secondary-button', text: 'Refresh models', onclick: loadModels }),
              h('span', { class: 'agentic-hint', id: 'ag-model-result' })),
            field('Thinking level', h('select', { id: 'ag-thinking_level' }, ...[['none', 'Off'], ['default', 'Model default'], ['minimal', 'Minimal'], ['low', 'Low'],
              ['medium', 'Medium'], ['high', 'High'], ['xhigh', 'Extra high'], ['max', 'Maximum']].map(([v, t]) => h('option', { value: v, text: t }))),
              'Available levels depend on the model.'),
            field('Agent context (tokens)', h('input', { id: 'ag-context', type: 'text', readonly: true }),
              'Shared with Video: change it in H3 → Hermes settings or Llama Swap → Parameters.'),
            field('Llama service', h('input', { id: 'ag-base-url', type: 'text', readonly: true })),
            h('details', { class: 'agentic-advanced' }, h('summary', { text: 'Advanced sampling' }),
              h('div', { class: 'agentic-grid' },
                field('Temperature', num('ag-temperature', 0, 2, 0.05)), field('Top P', num('ag-top_p', 0, 1, 0.01)),
                field('Top K', num('ag-top_k', 0, 1000, 1)), field('Min P', num('ag-min_p', 0, 1, 0.01)),
                field('Repeat penalty', num('ag-repeat_penalty', 0, 4, 0.01)), field('Seed (-1 = random)', num('ag-seed', -1, 2147483647, 1))),
              field('Other llama settings (JSON object)', h('textarea', { id: 'ag-extra_body', rows: 3, spellcheck: 'false' })))),
          h('div', { class: 'agentic-card' }, h('h2', { text: 'Agentic run' }),
            toggleRow('ag-plan_review', 'Hermes reviews its plan', 'A second Hermes pass checks the plan against your request and the model guide before anything renders'),
            toggleRow('ag-approve_plan', 'Wait for my approval', 'Default for the Studio checkbox: Approve renders the plan, Revise sends notes back to Hermes'),
            toggleRow('ag-output_review', reviewLabel[0], reviewLabel[1]),
            toggleRow('ag-use_model_guide', 'Use the model guide', 'Give Hermes the syntax guide for the model loaded in the Studio (below)'),
            studio === 'image' ? toggleRow('ag-suggest_loras', 'Look for relevant LoRAs',
              'Like H3: Hermes gets the installed LoRAs for the loaded model (name, category, triggers, short description) and may turn on ONE that fits, at its gallery strength, using one or two of its trigger words. LoRAs you enabled stay on. Krea 2 and Qwen Image only') : null,
            field('Attempts', h('select', { id: 'ag-max_attempts' }, h('option', { value: '1', text: '1 — no retries' }),
              h('option', { value: '2', text: '2 — one retry with feedback' }), h('option', { value: '3', text: '3 — two retries' })))),
          h('div', { class: 'agentic-card', id: 'agentic-guide-card' }, h('h2', { text: 'Model guides' }),
            h('p', { class: 'agentic-hint', text: 'What Hermes is told about each model\'s prompt syntax. The tab for the model loaded in the Studio is marked and opens automatically; its "Quick syntax" bullets also show under the Studio prompt.' }),
            h('div', { class: 'agentic-tabs', id: 'ag-guide-tabs', role: 'tablist' }),
            h('textarea', { id: 'ag-guide-text', rows: 16, spellcheck: 'false', 'aria-label': 'Model guide' }),
            h('div', { class: 'agentic-actions' }, h('button', { type: 'button', class: 'secondary-button', text: 'Reset to default', onclick: resetGuide }),
              h('span', { class: 'agentic-hint', id: 'ag-guide-state' }))),
          h('details', { class: 'agentic-card agentic-prompts' }, h('summary', { text: 'Prompts · what Hermes is told for plans and reviews' }),
            field('Hermes task instructions', h('textarea', { id: 'ag-instructions', rows: 7, spellcheck: 'false' })),
            field('Plan review prompt', h('textarea', { id: 'ag-plan_review_prompt', rows: 4, spellcheck: 'false' })),
            studio === 'image' ? field('Image review prompt', h('textarea', { id: 'ag-output_review_prompt', rows: 4, spellcheck: 'false' })) : null,
            h('div', { class: 'agentic-actions' }, h('button', { type: 'button', class: 'secondary-button', text: 'Reset prompts to default', onclick: resetPrompts }))),
          h('details', { class: 'agentic-card', id: 'agentic-preview-card' }, h('summary', { text: 'Preview · exact messages sent with the current Studio form' }),
            h('div', { class: 'agentic-actions' }, h('button', { type: 'button', class: 'secondary-button', text: 'Refresh preview', onclick: loadPreview }),
              h('span', { class: 'agentic-hint', id: 'ag-preview-size' })),
            h('pre', { class: 'agentic-preview', id: 'ag-preview' }))),
        h('aside', { class: 'agentic-term', 'aria-label': 'Hermes terminal' },
          h('div', { class: 'agentic-term-head' }, h('strong', { text: '☤ Hermes Agent' }), h('span', { id: 'ag-term-state', text: 'Idle' }),
            h('button', { type: 'button', id: 'ag-term-cancel', hidden: true, text: 'Cancel', onclick: () => steerSend('/stop') })),
          h('div', { class: 'agentic-term-picker' }, h('label', { for: 'ag-term-session', text: 'Session' }),
            h('select', { id: 'ag-term-session', onchange: event => { terminal.session = event.target.value; terminal.renderedRevision = -1; refreshTerminal(); } }),
            h('button', { type: 'button', id: 'ag-term-delete', title: 'Delete this finished session', 'aria-label': 'Delete session', text: '🗑', onclick: deleteSession })),
          h('div', { class: 'agentic-term-body', id: 'ag-term-body', role: 'log', 'aria-live': 'polite' }),
          h('form', { class: 'agentic-steer', id: 'ag-steer', autocomplete: 'off', onsubmit: event => { event.preventDefault(); steerSend(); } },
            h('div', { class: 'agentic-steer-menu', id: 'ag-steer-menu', role: 'listbox', hidden: true }),
            h('span', { class: 'agentic-steer-prompt', 'aria-hidden': 'true', text: '❯' }),
            h('input', { id: 'ag-steer-input', type: 'text', enterkeyhint: 'send', placeholder: 'Steer Hermes… (/ for commands)', 'aria-label': 'Steer the Agentic session' }),
            h('button', { type: 'submit', id: 'ag-steer-send', text: 'Send' })),
          h('div', { class: 'agentic-term-foot', id: 'ag-term-foot', text: 'Waiting for a session' }))));
    document.body.append(view);
    const form = el('agentic-settings-form');
    form.addEventListener('change', event => { if (event.target.id !== 'ag-guide-text') scheduleSave(); });
    form.addEventListener('input', event => { if (event.target.tagName === 'TEXTAREA' && event.target.id !== 'ag-guide-text') scheduleSave(); });
    el('ag-guide-text').addEventListener('input', () => scheduleSave({ guide: true }));
    const body = el('ag-term-body');
    body.addEventListener('scroll', () => { terminal.follow = body.scrollTop + body.clientHeight >= body.scrollHeight - 8; }, { passive: true });
    bindSteerMenu();
    document.addEventListener('keydown', event => { if (event.key === 'Escape' && !view.hidden && !el('ag-steer-menu').childElementCount) closeView(); });
  }
  function fillSettings() {
    if (!settings) return;
    for (const key of ['thinking_level', 'temperature', 'top_p', 'top_k', 'min_p', 'repeat_penalty', 'seed', 'instructions',
      'plan_review_prompt', 'output_review_prompt']) { const node = el(`ag-${key}`); if (node) node.value = settings[key]; }
    for (const key of ['plan_review', 'approve_plan', 'output_review', 'use_model_guide', 'suggest_loras']) if (el(`ag-${key}`)) el(`ag-${key}`).checked = Boolean(settings[key]);
    el('ag-max_attempts').value = String(settings.max_attempts);
    el('ag-extra_body').value = JSON.stringify(settings.extra_body || {}, null, 2);
    el('ag-base-url').value = settings.base_url || '';
    el('ag-context').value = settings.context_size ? `${settings.context_size.toLocaleString()} tokens` : 'Llama default';
    const select = el('ag-model');
    if (select.options.length <= 1 || ![...select.options].some(option => option.value === settings.model)) {
      select.replaceChildren(h('option', { value: '', text: `Framework default · ${settings.default_model}` }),
        ...(settings.model ? [h('option', { value: settings.model, text: settings.model })] : []));
    }
    select.value = settings.model || '';
  }
  async function loadModels() {
    const result = el('ag-model-result');
    try {
      const data = await call('/models');
      const select = el('ag-model');
      select.replaceChildren(h('option', { value: '', text: `Framework default · ${data.default || 'Hermes'}` }),
        ...data.models.map(id => h('option', { value: id, text: id })));
      select.value = settings?.model || '';
      result.textContent = `${data.models.length} models`;
    } catch (error) { result.textContent = error.message; }
  }
  function collect(includeGuide) {
    const changes = { model: value('ag-model'), thinking_level: value('ag-thinking_level'), max_attempts: Number(value('ag-max_attempts')),
      instructions: value('ag-instructions'), plan_review_prompt: value('ag-plan_review_prompt') };
    if (el('ag-output_review_prompt')) changes.output_review_prompt = value('ag-output_review_prompt');
    for (const key of ['temperature', 'top_p', 'top_k', 'min_p', 'repeat_penalty', 'seed']) changes[key] = Number(value(`ag-${key}`));
    for (const key of ['plan_review', 'approve_plan', 'output_review', 'use_model_guide', 'suggest_loras']) if (el(`ag-${key}`)) changes[key] = el(`ag-${key}`).checked;
    try { changes.extra_body = JSON.parse(value('ag-extra_body') || '{}'); }
    catch { throw new Error('Other llama settings must be valid JSON'); }
    if (includeGuide && guideKey) changes.guide_overrides = { [guideKey]: value('ag-guide-text') };
    return changes;
  }
  function scheduleSave(options = {}) {
    clearTimeout(saveTimer);
    el('agentic-save-state').textContent = 'Saving…';
    saveTimer = setTimeout(async () => {
      try {
        settings = await send('PUT', '/settings', collect(options.guide));
        el('agentic-save-state').textContent = 'Saved';
        if (el('agentic-approve-plan')) el('agentic-approve-plan').checked = settings.approve_plan;
        if (options.guide) { guides = await call('/guides'); renderGuideTabs(); renderSyntax(); }
      } catch (error) { el('agentic-save-state').textContent = error.message; }
    }, 600);
  }
  function renderGuideTabs() {
    const tabs = el('ag-guide-tabs');
    if (!tabs || !guides) return;
    tabs.replaceChildren(...Object.entries(guides.models).map(([key, guide]) => h('button', {
      type: 'button', role: 'tab', class: 'agentic-tab' + (key === guideKey ? ' is-active' : ''), 'aria-selected': String(key === guideKey),
      onclick: () => selectGuide(key) }, guide.label, key === loadedModel ? h('span', { class: 'agentic-tab-loaded', text: 'loaded' }) : null,
      guide.overridden ? h('span', { class: 'agentic-tab-edited', title: 'Edited', text: '•' }) : null)));
    const guide = guides.models[guideKey];
    el('ag-guide-state').textContent = guide?.overridden ? 'Edited — Hermes uses your version' : 'Shipped guide';
  }
  function selectGuide(key) {
    if (!guides?.models?.[key]) return;
    const editor = el('ag-guide-text');
    if (guideKey === key && document.activeElement === editor) return;
    guideKey = key;
    editor.value = guides.models[key].text;
    renderGuideTabs();
  }
  async function resetGuide() {
    if (!guideKey) return;
    el('ag-guide-text').value = guides.models[guideKey].default;
    scheduleSave({ guide: true });
  }
  function resetPrompts() {
    el('ag-instructions').value = settings.default_instructions;
    el('ag-plan_review_prompt').value = settings.default_plan_review_prompt;
    if (el('ag-output_review_prompt')) el('ag-output_review_prompt').value = settings.default_output_review_prompt;
    scheduleSave();
  }
  async function loadPreview() {
    try {
      const body = payload();
        const preview = await send('POST', '/preview', { request: body.request, guide: body.guide, settings: body.settings });
      el('ag-preview').textContent = preview.messages.map(message => `── ${message.role.toUpperCase()} ──\n${message.content}`).join('\n\n');
      el('ag-preview-size').textContent = `${preview.characters.toLocaleString()} characters · about ${Math.round(preview.characters / 3.5).toLocaleString()} input tokens · ${modelLabel(preview.model)} guide`;
    } catch (error) { el('ag-preview').textContent = error.message; }
  }
  async function openView(focus = '', sessionId = '') {
    if (!el('agentic-view')) buildView();
    const view = el('agentic-view');
    view.hidden = false;
    document.body.classList.add('agentic-view-open');
    if (sessionId) { terminal.session = sessionId; terminal.renderedRevision = -1; }
    try {
      [settings, guides] = await Promise.all([call('/settings'), call('/guides')]);
      fillSettings();
      selectGuide(loadedModel);
      loadModels();
    } catch (error) { el('agentic-save-state').textContent = error.message; }
    await refresh();
    refreshTerminal();
    clearInterval(terminal.timer);
    terminal.timer = setInterval(refreshTerminal, 1500);
    if (focus === 'guide') el('agentic-guide-card').scrollIntoView({ block: 'start', behavior: 'smooth' });
    if (focus === 'terminal' && matchMedia('(max-width: 1000px)').matches) document.querySelector('.agentic-term').scrollIntoView({ block: 'start' });
    try { history.replaceState(null, '', '#hermes'); } catch { /* ignore */ }
  }
  function closeView() {
    el('agentic-view').hidden = true;
    document.body.classList.remove('agentic-view-open');
    clearInterval(terminal.timer); terminal.timer = null;
    try { if (location.hash === '#hermes') history.replaceState(null, '', location.pathname + location.search); } catch { /* ignore */ }
    refresh();
  }

  // ── Hermes terminal ──────────────────────────────────────────────────────
  function refreshPicker() {
    const picker = el('ag-term-session');
    if (!picker) return;
    if (!sessions.some(row => row.id === terminal.session)) terminal.session = sessions[0]?.id || '';
    const options = sessions.length ? sessions.map(row => h('option', { value: row.id,
      text: `${new Date(row.created * 1000).toLocaleTimeString()} · ${modelLabel(row.loaded_model || '')} · ${stateText(row.state)} · ${(row.request || '').slice(0, 60)}` }))
      : [h('option', { value: '', text: 'No recent sessions' })];
    const signature = options.map(option => option.value + option.textContent).join('|');
    if (picker.dataset.signature !== signature) { picker.replaceChildren(...options); picker.dataset.signature = signature; }
    picker.value = terminal.session;
  }
  const PREFIX = { user: '❯ ', tool: '┊ ⚙ ', assistant: '☤ ', stream: '☤ ', thinking: '◇ ', ok: '✓ ', warn: '⚠ ', error: '✗ ' };
  async function refreshTerminal() {
    if (terminal.busy || el('agentic-view')?.hidden !== false) return;
    terminal.busy = true;
    const body = el('ag-term-body');
    try {
      refreshPicker();
      const disabled = !terminal.session;
      el('ag-steer-input').disabled = el('ag-steer-send').disabled = disabled;
      if (disabled) {
        el('ag-term-state').textContent = 'Idle'; el('ag-term-cancel').hidden = true; el('ag-term-delete').hidden = true;
        el('ag-term-foot').textContent = 'Waiting for a session';
        body.replaceChildren(h('p', { class: 'agentic-term-empty', text: `Turn on Agentic mode in the ${LABEL} studio and press Generate to see Hermes work here.` }));
        terminal.renderedId = ''; return;
      }
      const row = await call(`/sessions/${terminal.session}`);
      const active = ACTIVE.has(row.state);
      el('ag-term-cancel').hidden = !active && row.state !== 'awaiting_approval';
      el('ag-term-delete').hidden = active || row.state === 'awaiting_approval' || row.state === 'awaiting_feedback';
      const state = el('ag-term-state'); state.textContent = stateText(row.state); state.dataset.state = row.state;
      const elapsed = Math.max(0, Math.floor((FINISHED.has(row.state) ? row.updated : Date.now() / 1000) - row.created));
      el('ag-term-foot').textContent = `${row.planner_model || 'Hermes'}  │  ${modelLabel(row.loaded_model || '')} guide  │  ${Math.floor(elapsed / 60)}:${String(elapsed % 60).padStart(2, '0')}  │  ${row.stage || stateText(row.state)}`;
      if (terminal.renderedId === row.id && terminal.renderedRevision === row.log_revision && terminal.renderedState === row.state) return;
      if (terminal.renderedId !== row.id) { body.replaceChildren(); terminal.follow = true; }
      const rows = [...body.querySelectorAll('.agentic-term-row:not(.is-live)')];
      rows.slice((row.log || []).length).forEach(node => node.remove());
      let live = body.querySelector('.agentic-term-row.is-live');
      body.querySelector('.agentic-term-empty')?.remove();
      (row.log || []).forEach((entry, index) => {
        let node = rows[index];
        if (!node) {
          node = h('div', { class: 'agentic-term-row' }, h('span', { class: 'agentic-term-time' }), h('span', { class: 'agentic-term-text' }));
          body.insertBefore(node, live);
        }
        node.dataset.kind = entry.kind;
        const time = new Date(entry.t * 1000).toLocaleTimeString();
        if (node.firstChild.textContent !== time) node.firstChild.textContent = time;
        const text = (PREFIX[entry.kind] || '· ') + entry.text;
        if (node.lastChild.textContent !== text) node.lastChild.textContent = text;
      });
      if (active && !live) {
        live = h('div', { class: 'agentic-term-row is-live', 'data-kind': 'system' }, h('span', { class: 'agentic-term-time' }), h('span', { class: 'agentic-term-text' }));
        body.append(live);
      } else if (!active && live) { live.remove(); live = null; }
      if (live) live.lastChild.textContent = `… ${row.stage || stateText(row.state)}`;
      // Outputs (and the feedback box) are rebuilt only when they change; otherwise just kept last.
      const outKey = JSON.stringify([row.id, row.state, row.outputs || []]);
      let outs = body.querySelector('.agentic-term-outputs');
      if (outs && outs.dataset.key !== outKey) { outs.remove(); outs = null; }
      if (!outs && row.outputs?.length) {
        outs = h('div', { class: 'agentic-term-outputs', 'data-key': outKey }, outputsOf(row),
          row.state === 'awaiting_feedback' ? feedbackControls(row, async () => { await refresh(); refreshTerminal(); }) : null);
      }
      if (outs && body.lastElementChild !== outs) body.append(outs);
      if (terminal.follow) body.scrollTop = body.scrollHeight;
      terminal.renderedId = row.id; terminal.renderedRevision = row.log_revision; terminal.renderedState = row.state;
    } catch (error) {
      el('ag-term-foot').textContent = `Log unavailable: ${error.message}`;
    } finally { terminal.busy = false; }
  }
  async function deleteSession() {
    if (!terminal.session || !confirm('Delete this Agentic session from the list? Rendered files stay in the studio.')) return;
    try { await send('DELETE', `/sessions/${terminal.session}`); terminal.session = ''; await refresh(); refreshTerminal(); }
    catch (error) { el('ag-term-foot').textContent = error.message; }
  }
  const COMMANDS = [
    { token: '/status', label: 'what Hermes is doing and which renders are done' },
    { token: '/plan', label: 'show the current plan' },
    { token: '/approve ', label: 'render the waiting plan, or approve the image and refine it, with optional notes' },
    { token: '/revise ', label: 'send the waiting plan back to Hermes with notes' },
    { token: '/disapprove ', label: 'reject the rendered image; Hermes re-plans from your notes' },
    { token: '/keep', label: 'keep the rendered image and end the session' },
    { token: '/retry ', label: 'start again from this session, with optional notes' },
    { token: '/stop', label: 'end this run' },
    { token: '/help', label: 'list the commands' },
  ];
  const menu = { matches: [], index: 0 };
  function renderMenu() {
    const node = el('ag-steer-menu');
    node.hidden = !menu.matches.length;
    node.replaceChildren(...menu.matches.map((entry, index) => h('button', { type: 'button', role: 'option',
      class: 'agentic-steer-option' + (index === menu.index ? ' is-active' : ''), 'data-index': String(index) },
      h('b', { text: entry.token.trim() }), h('span', { text: entry.label }))));
  }
  function pick(entry) {
    const input = el('ag-steer-input'); input.value = entry.token; input.focus();
    menu.matches = []; renderMenu();
  }
  function bindSteerMenu() {
    const input = el('ag-steer-input');
    input.addEventListener('input', () => {
      menu.matches = /^\/\S*$/.test(input.value) ? COMMANDS.filter(entry => entry.token.startsWith(input.value.toLowerCase()) && entry.token.trim() !== input.value) : [];
      menu.index = Math.min(menu.index, Math.max(0, menu.matches.length - 1));
      renderMenu();
    });
    input.addEventListener('blur', () => setTimeout(() => { menu.matches = []; renderMenu(); }, 120));
    input.addEventListener('keydown', event => {
      if (!menu.matches.length) return;
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        menu.index = (menu.index + (event.key === 'ArrowDown' ? 1 : -1) + menu.matches.length) % menu.matches.length;
        renderMenu();
      } else if (event.key === 'Tab' || (event.key === 'Enter' && menu.matches[menu.index].token.endsWith(' '))) {
        event.preventDefault(); pick(menu.matches[menu.index]);
      } else if (event.key === 'Escape') { event.stopPropagation(); menu.matches = []; renderMenu(); }
    });
    el('ag-steer-menu').addEventListener('mousedown', event => {
      const option = event.target.closest('[data-index]');
      if (!option) return;
      event.preventDefault(); pick(menu.matches[Number(option.dataset.index)]);
    });
  }
  async function steerSend(forced) {
    const input = el('ag-steer-input'), button = el('ag-steer-send');
    const text = (forced || input.value).trim();
    if (!text || !terminal.session) return;
    button.disabled = true;
    try {
      const result = await send('POST', `/sessions/${terminal.session}/steer`, { text });
      if (!forced) input.value = '';
      menu.matches = []; renderMenu();
      terminal.follow = true; terminal.renderedRevision = -1;
      if (result.session_id && result.session_id !== terminal.session) terminal.session = result.session_id;
      await refresh();
      await refreshTerminal();
    } catch (error) { el('ag-term-foot').textContent = error.message; }
    finally { button.disabled = false; if (!forced) input.focus(); }
  }

  // ── start ────────────────────────────────────────────────────────────────
  async function setup() {
    loadedModel = currentModel();
    try { [settings, guides] = await Promise.all([call('/settings'), call('/guides')]); }
    catch (error) { guides = null; }
    setupPanel();
    if (el('agentic-approve-plan') && settings) el('agentic-approve-plan').checked = Boolean(settings.approve_plan);
    renderSyntax();
    document.addEventListener('change', event => { if (event.target.matches?.('input[name="model"], #model, #engine')) watchModel(); });
    setInterval(watchModel, 1000);  // some studios switch the model from tabs without a change event
    await refresh();
    if (sessions.some(row => !FINISHED.has(row.state))) el('agentic-controls').open = true;
    if (location.hash === '#hermes') openView();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', setup); else setup();
})();
