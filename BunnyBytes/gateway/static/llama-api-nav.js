(() => {
  // Same ?v= as this script's own URL (set by the gateway), so the editor files are never stale.
  const VERSION = new URL(document.currentScript.src).searchParams.get('v') || '';
  // BunnyBytes additions to llama-swap's UI (gateway-injected):
  //  - an "API" link in the sidebar, styled by cloning its Settings link
  //  - a "Rocsy home" link at the top of the sidebar (same as every service's top bar)
  //  - a "Parameters" tab next to Details on each model page (#/models/<id>),
  //    rendered by llama-params.js inside llama-swap's own tab strip
  function addApiTab() {
    if (document.querySelector('a[data-bunny-tab]')) return;
    const settings = [...document.querySelectorAll('a')].find(a =>
      [...a.querySelectorAll('span')].some(s => s.textContent.trim() === 'Settings')
    );
    if (!settings) return;
    const link = settings.cloneNode(true);
    link.dataset.bunnyTab = 'api';
    link.href = '/llama-api';
    link.removeAttribute('aria-current');
    const label = [...link.querySelectorAll('span')].find(s => s.textContent.trim() === 'Settings');
    if (label) label.textContent = 'API';
    link.title = 'API setup';
    settings.parentNode.insertBefore(link, settings);
  }

  const ROCSY_MARK = '<svg viewBox="40 140 432 260" width="18" height="12" aria-hidden="true" style="flex:none">'
    + '<circle cx="256" cy="268" r="124" fill="var(--bb-amber, #e1a744)"/><path fill="#f2f7fb" d="M256 186 C269 186 276 197 274 210 '
    + 'C320 182 380 162 464 156 C418 200 358 234 292 254 L308 314 L256 294 L204 314 L220 254 C154 234 94 200 48 156 C132 162 192 182 '
    + '238 210 C236 197 243 186 256 186 Z"/></svg>';
  function addHomeLink() {
    if (document.querySelector('a[data-bunny-home]')) return;
    const first = [...document.querySelectorAll('a')].find(a =>
      [...a.querySelectorAll('span')].some(s => s.textContent.trim() === 'Activity')
    );
    if (!first) return;
    const link = first.cloneNode(true);
    link.dataset.bunnyHome = '1';
    link.href = '/';
    link.removeAttribute('aria-current');
    link.title = 'Back to the Rocsy home page';
    const label = [...link.querySelectorAll('span')].find(s => s.textContent.trim() === 'Activity');
    if (label) label.textContent = 'Rocsy home';
    const icon = link.querySelector('svg');
    if (icon) icon.outerHTML = ROCSY_MARK;
    first.parentNode.insertBefore(link, first);
  }

  let assets;
  function loadParamsAssets() {
    if (!assets) {
      const css = document.createElement('link');
      css.rel = 'stylesheet';
      css.href = `/static/llama-params.css?v=${VERSION}`;
      document.head.append(css);
      assets = new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = `/static/llama-params.js?v=${VERSION}`;
        script.onload = resolve;
        script.onerror = () => { assets = null; reject(new Error('Could not load the parameters editor.')); };
        document.head.append(script);
      });
    }
    return assets;
  }

  const SAVED = ['data-state', 'aria-selected', 'tabindex', 'data-active', 'hidden'];

  function remember(element) {
    if (element.dataset.bpSaved) return;
    const saved = {};
    for (const name of SAVED) saved[name] = element.getAttribute(name);
    element.dataset.bpSaved = JSON.stringify(saved);
  }

  function restore(element) {
    if (!element.dataset.bpSaved) return;
    const saved = JSON.parse(element.dataset.bpSaved);
    for (const [name, value] of Object.entries(saved)) {
      if (value === null) element.removeAttribute(name); else element.setAttribute(name, value);
    }
    delete element.dataset.bpSaved;
  }

  function setState(element, active) {
    element.setAttribute('data-state', active ? 'active' : 'inactive');
    element.setAttribute('aria-selected', String(active));
    element.setAttribute('tabindex', active ? '0' : '-1');
    if (active) element.setAttribute('data-active', ''); else element.removeAttribute('data-active');
  }

  function currentModel() {
    const match = location.hash.match(/^#\/models\/([^/?#]+)/);
    return match ? decodeURIComponent(match[1]) : null;
  }

  const CUSTOM_TAB = '[data-bunny-custom-tab]';
  const CUSTOM_PANEL = '[data-bunny-custom-panel]';

  function closeCustom(root) {
    root.querySelectorAll(CUSTOM_TAB).forEach(tab => setState(tab, false));
    root.querySelectorAll(CUSTOM_PANEL).forEach(panel => { panel.hidden = true; });
    root.querySelectorAll('[data-bp-saved]').forEach(restore);
  }

  function activateCustom(root, tab, panel) {
    closeCustom(root);
    root.querySelectorAll(`[role=tab]:not(${CUSTOM_TAB})`).forEach(native => { remember(native); setState(native, false); });
    [...root.children].filter(node => node.getAttribute('role') === 'tabpanel' && !node.matches(CUSTOM_PANEL))
      .forEach(native => { remember(native); native.hidden = true; });
    setState(tab, true);
    panel.hidden = false;
  }

  const TUNE_STEPS = [['auto_first', 'rocBLAS auto'], ['1', 'hipBLASLt'], ['0', 'Tensile'], ['auto_last', 'rocBLAS auto (repeat)']];
  const TUNE_LABELS = Object.fromEntries(TUNE_STEPS);
  const TIPS = {
    pp: 'Prompt processing (prefill): how fast the model reads everything you send before it starts answering — the system prompt, chat history, pasted text and files. Higher means a shorter wait before the first word appears, which matters most for long prompts and agent runs.',
    tg: 'Generation (decode): how fast the model writes its reply, one token at a time. This is the streaming speed you watch in chat; roughly 1 token ≈ ¾ of a word.',
  };
  const TUNE_WINNERS = {1: 'hipBLASLt', 0: 'Tensile', auto: 'rocBLAS automatic'};
  const ICONS = {
    prompt: '<path d="M4 6h16M4 12h10M4 18h7"/><path d="M17 15l3 3-3 3"/>',
    gen: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 17l.8 2.2L22 20l-2.2.8L19 23l-.8-2.2L16 20l2.2-.8z"/>',
    chip: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
    play: '<path d="M7 4v16l13-8z"/>',
    tune: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
    check: '<path d="M5 12l5 5 9-10"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  };
  const icon = (name, size = 18) => {
    const span = document.createElement('span');
    span.className = 'bp-icon';
    span.innerHTML = `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
    return span;
  };

  async function mountBenchmark(panel, modelId) {
    await loadParamsAssets();
    const {node, card, skin} = window.BunnyLlamaParams;
    panel.classList.add('bp', 'bp-bench');
    const post = async (url, fallback) => {
      const response = await fetch(url, {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({model: modelId})});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || fallback);
      return data;
    };
    const getJson = async (url, fallback) => {
      const response = await fetch(url, {credentials: 'same-origin'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || fallback);
      return data;
    };
    const button = (text, primary, iconName) => {
      const element = node('button', `bp-button${primary ? ' bp-primary' : ''}`);
      element.type = 'button';
      if (iconName) element.append(icon(iconName, 15));
      element.append(node('span', '', text));
      element.setText = value => { element.lastChild.textContent = value; };
      return element;
    };
    const badge = (element, text, tone) => {
      element.className = `bp-badge${tone ? ` bp-${tone}` : ''}`;
      element.replaceChildren(node('i', 'bp-dot'), node('span', '', text));
    };
    const when = seconds => seconds ? new Date(seconds * 1000).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'}) : '';
    const buildName = path => (path || '').replace(/\/bin\/llama-bench$/, '').split('/').filter(Boolean).pop() || '';
    const setError = (element, message) => { element.textContent = message; element.classList.toggle('bp-error', Boolean(message)); };

    // Hero: what is being measured, the build, and llama-bench readiness.
    const hero = node('div', 'bp-hero');
    const heroText = node('div', 'bp-hero-text');
    const buildChip = node('span', 'bp-chip');
    const installBadge = node('span', 'bp-badge', '');
    badge(installBadge, 'Checking llama-bench…');
    const heroMeta = node('div', 'bp-hero-meta');
    heroMeta.append(installBadge, buildChip);
    heroText.append(node('h2', 'bp-hero-title', 'Benchmarking'), heroMeta);
    const install = button('Install llama-bench', true, 'chip');
    const uninstall = button('Uninstall llama-bench', false);
    const heroActions = node('div', 'bp-hero-actions');
    heroActions.append(install, uninstall);
    hero.append(heroText, heroActions);

    // Benchmark card: two big speed tiles.
    const tip = (element, text) => {
      element.classList.add('bp-has-tip');
      element.tabIndex = 0;
      element.setAttribute('aria-description', text);
      element.append(node('span', 'bp-tip', text));
    };
    const tile = (label, iconName, tone) => {
      const element = node('div', `bp-stat bp-${tone}`);
      const value = node('span', 'bp-stat-value', '—');
      const top = node('div', 'bp-stat-top');
      top.append(icon(iconName, 16), node('span', 'bp-stat-label', label), icon('info', 14));
      element.append(top, value, node('span', 'bp-stat-unit', 'tokens / second'));
      tip(element, TIPS[tone]);
      return [element, value];
    };
    const [ppTile, ppValue] = tile('Prompt processing', 'prompt', 'pp');
    const [tgTile, tgValue] = tile('Generation', 'gen', 'tg');
    const stats = node('div', 'bp-stats');
    stats.append(ppTile, tgTile);
    const benchBadge = node('span', 'bp-badge');
    const benchMeta = node('span', 'bp-meta');
    const benchStatus = node('p', 'bp-status');
    benchStatus.setAttribute('role', 'status');
    const start = button('Run benchmark', true, 'play');
    const benchFoot = node('div', 'bp-actions');
    benchFoot.append(benchBadge, benchMeta, benchStatus, start);
    const benchCard = card(skin, 'Speed test', [
      node('p', 'bp-note', '512 prompt + 128 generated tokens, two runs, with this model’s saved Parameters. Unload the model first; nothing is changed.'),
      stats, benchFoot]);

    // hipBLASLt card: phase stepper + one bar chart per measure.
    const stepper = node('ol', 'bp-steps');
    const stepItems = TUNE_STEPS.map(([id, label]) => {
      const item = node('li', 'bp-step');
      item.dataset.step = id;
      const mark = node('span', 'bp-step-mark');
      item.append(mark, node('span', 'bp-step-label', label));
      stepper.append(item);
      return [id, item, mark];
    });
    const charts = node('div', 'bp-charts');
    const tuneBadge = node('span', 'bp-badge');
    const tuneStatus = node('p', 'bp-status');
    tuneStatus.setAttribute('role', 'status');
    const tuneDelete = button('Delete tuning data', false);
    const tuneStart = button('Tune this model', true, 'tune');
    const tuneFoot = node('div', 'bp-actions');
    tuneFoot.append(tuneBadge, tuneStatus, tuneDelete, tuneStart);
    const tuneCard = card(skin, 'hipBLASLt tuning', [
      node('p', 'bp-note', 'Races rocBLAS automatic selection against Tensile and preferred hipBLASLt. Quantized ops outside rocBLAS are unaffected; the winner applies on the next model load.'),
      stepper, charts, tuneFoot]);

    const wrap = node('div', 'bp-form');
    wrap.append(hero, benchCard, tuneCard);
    panel.replaceChildren(wrap);

    // Horizontal bars, one chart per measure (prompt, generation), winner highlighted.
    const barChart = (title, rows, key, winner) => {
      const chart = node('figure', 'bp-chart');
      const caption = node('figcaption', '');
      caption.append(node('span', '', title), icon('info', 13));
      tip(caption, TIPS[key]);
      chart.append(caption);
      const max = Math.max(...rows.map(([, score]) => score[key]), 1e-9);
      for (const [name, score] of rows) {
        const row = node('div', `bp-bar-row${winner.includes(name) ? ' bp-winner' : ''}`);
        row.title = `${TUNE_LABELS[name] || name}: ${score[key].toFixed(2)} tokens/s`;
        const track = node('div', 'bp-bar-track');
        const bar = node('div', 'bp-bar');
        bar.style.width = `${Math.max(2, score[key] / max * 100)}%`;
        track.append(bar);
        const label = node('span', 'bp-bar-label', TUNE_LABELS[name] || name);
        if (winner.includes(name)) label.append(icon('check', 13));
        row.append(label, track, node('span', 'bp-bar-value', score[key].toFixed(1)));
        chart.append(row);
      }
      return chart;
    };

    const installUrl = `/llama-benchmark/install/status/${encodeURIComponent(modelId)}`;
    let installed = false;
    const pollInstall = async () => {
      if (!panel.isConnected) return;
      try {
        const data = await getJson(installUrl, 'Could not check llama-bench');
        installed = Boolean(data.installed);
        if (data.error) badge(installBadge, data.error, 'warn');
        else if (data.state === 'installing') badge(installBadge, 'Building llama-bench…', 'busy');
        else if (installed) badge(installBadge, 'llama-bench ready · Agent connected', 'ok');
        else badge(installBadge, 'llama-bench not installed', 'warn');
        buildChip.hidden = !data.build;
        buildChip.replaceChildren(icon('chip', 13), node('span', '', buildName(data.build)));
        buildChip.title = data.build || '';
        install.hidden = installed;
        uninstall.hidden = !installed;
        install.disabled = uninstall.disabled = data.state === 'installing';
        if (data.state === 'installing') setTimeout(pollInstall, 3000);
        else poll();
      } catch (error) { badge(installBadge, error.message, 'warn'); }
    };
    for (const [element, action] of [[install, 'install'], [uninstall, 'uninstall']]) {
      element.addEventListener('click', async () => {
        element.disabled = true;
        try { await post(`/llama-benchmark/${action}`, `Could not ${action} llama-bench`); }
        catch (error) { badge(installBadge, error.message, 'warn'); }
        pollInstall();
      });
    }

    let timer;
    const poll = async () => {
      clearTimeout(timer);
      if (!panel.isConnected) return;
      try {
        const data = await getJson(`/llama-benchmark/status/${encodeURIComponent(modelId)}`, 'Could not read benchmark status');
        const running = data.state === 'running';
        stats.classList.toggle('bp-running', running);
        stats.classList.toggle('bp-empty', !data.scores);
        ppValue.textContent = data.scores ? data.scores.pp.toFixed(1) : '—';
        tgValue.textContent = data.scores ? data.scores.tg.toFixed(1) : '—';
        if (running) badge(benchBadge, 'Running', 'busy');
        else if (data.state === 'failed') badge(benchBadge, 'Failed', 'warn');
        else if (data.scores) badge(benchBadge, 'Complete', 'ok');
        else badge(benchBadge, 'Not run yet');
        benchMeta.textContent = data.scores ? when(data.finished) : running ? `started ${when(data.started)}` : '';
        setError(benchStatus, data.state === 'failed' ? data.error : '');
        start.disabled = running || !installed;
        start.title = installed ? '' : 'Install llama-bench first';
        start.setText(data.scores ? 'Run again' : 'Run benchmark');
        if (running) timer = setTimeout(poll, 2500);
      } catch (error) { setError(benchStatus, error.message); }
    };
    start.addEventListener('click', async () => {
      start.disabled = true;
      setError(benchStatus, '');
      badge(benchBadge, 'Starting…', 'busy');
      try { await post('/llama-benchmark/start', 'Could not start benchmark'); }
      catch (error) { setError(benchStatus, error.message); start.disabled = false; return; }
      poll();
    });

    let tuneTimer;
    const pollTune = async () => {
      clearTimeout(tuneTimer);
      if (!panel.isConnected) return;
      try {
        const data = await getJson(`/llama-hipblaslt/status/${encodeURIComponent(modelId)}`, 'Could not read tuning status');
        const running = data.state === 'running';
        const scores = data.scores || {};
        if (running) badge(tuneBadge, `Tuning ${TUNE_LABELS[data.phase] || ''}`.trim() + '…', 'busy');
        else if (data.state === 'failed') badge(tuneBadge, 'Tuning failed', 'warn');
        else if (data.state === 'complete' && data.current) badge(tuneBadge, `Winner: ${TUNE_WINNERS[data.winner] || data.winner}${data.applied ? ' · applies on next load' : ''}`, 'ok');
        else if (data.state === 'complete') badge(tuneBadge, 'Stale: retune for this model, build or ROCm library', 'warn');
        else badge(tuneBadge, 'No tuning data');
        for (const [id, item, mark] of stepItems) {
          const state = scores[id] ? 'done' : running && data.phase === id ? 'active' : 'pending';
          item.dataset.state = state;
          if (state === 'done') mark.replaceChildren(icon('check', 13)); else mark.replaceChildren();
        }
        stepper.hidden = !running && !Object.keys(scores).length;
        const winner = data.state !== 'complete' ? [] : data.winner === 'auto' ? ['auto_first', 'auto_last'] : [data.winner];
        const rows = TUNE_STEPS.filter(([id]) => scores[id]).map(([id]) => [id, scores[id]]);
        charts.replaceChildren(...(rows.length ? [barChart('Prompt · tokens/s', rows, 'pp', winner), barChart('Generation · tokens/s', rows, 'tg', winner)] : []));
        charts.hidden = !rows.length;
        setError(tuneStatus, data.state === 'failed' ? data.error : '');
        tuneStart.disabled = running;
        tuneDelete.disabled = running || data.state === 'idle';
        if (running) tuneTimer = setTimeout(pollTune, 3000);
      } catch (error) { setError(tuneStatus, error.message); }
    };
    tuneStart.addEventListener('click', async () => {
      tuneStart.disabled = true;
      setError(tuneStatus, '');
      try { await post('/llama-hipblaslt/start', 'Could not start tuning'); }
      catch (error) { setError(tuneStatus, error.message); tuneStart.disabled = false; return; }
      pollTune();
    });
    tuneDelete.addEventListener('click', async () => {
      if (!confirm(`Delete saved hipBLASLt tuning for ${modelId}? The model files will stay installed.`)) return;
      tuneDelete.disabled = true;
      try { await post('/llama-hipblaslt/delete', 'Could not delete tuning data'); await pollTune(); }
      catch (error) { setError(tuneStatus, error.message); tuneDelete.disabled = false; }
    });

    pollInstall();
    pollTune();
  }

  async function mountStrata(panel) {
    panel.replaceChildren();
    const status = document.createElement('p');
    status.textContent = 'Checking Strata…';
    panel.append(status);
    try {
      const response = await fetch('/strata/status', {credentials: 'same-origin'});
      const data = await response.json();
      if (!data.running) {
        status.textContent = 'Strata is not running. Load this model through llama-swap, then reopen this tab.';
        return;
      }
      status.remove();
      const frame = document.createElement('iframe');
      frame.src = '/Strata/';
      frame.title = 'Strata UI';
      frame.style.cssText = 'width:100%;height:75vh;border:0;border-radius:10px;background:#111';
      panel.append(frame);
    } catch (error) { status.textContent = error.message; }
  }

  function addParamsTab() {
    const modelId = currentModel();
    const details = document.querySelector('[role=tab][data-value=details]');
    if (!modelId || !details) return;
    const root = details.closest('[data-slot=tabs]');
    if (!root) return;
    const existing = root.querySelector(CUSTOM_TAB);
    if (existing && existing.dataset.model === modelId) return;
    if (existing) closeCustom(root);
    root.querySelectorAll(`${CUSTOM_TAB}, ${CUSTOM_PANEL}`).forEach(node => node.remove());
    const nativePanel = [...root.children].find(node => node.getAttribute('role') === 'tabpanel');
    const add = (label, name, mount) => {
      const tab = details.cloneNode(false);
      tab.removeAttribute('id'); tab.removeAttribute('aria-controls');
      tab.dataset.value = `bunny-${name}`;
      tab.dataset.bunnyCustomTab = name;
      tab.dataset.model = modelId;
      tab.textContent = label;
      setState(tab, false);
      const panel = document.createElement('div');
      panel.setAttribute('role', 'tabpanel');
      panel.dataset.bunnyCustomPanel = name;
      panel.className = nativePanel?.className || '';
      panel.hidden = true;
      root.append(panel);
      tab.addEventListener('click', async () => {
        activateCustom(root, tab, panel);
        try { await mount(panel, modelId); }
        catch (error) { panel.textContent = error.message; }
      });
      return tab;
    };
    let after = details;
    // Strata models get their own Parameters list (Vision on/off) from the same editor.
    const params = add('Parameters', 'params', async (panel, id) => {
      await loadParamsAssets();
      await window.BunnyLlamaParams.mount(panel, id);
    });
    after.after(params); after = params;
    if (modelId === 'strata-qwen-flash-next') {
      const tab = add('Strata UI', 'strata', mountStrata);
      after.after(tab);
    } else {
      const bench = add('Benchmarking', 'benchmark', mountBenchmark);
      after.after(bench);
    }
    // Arriving from the Trainer's "Use in Llama": open Parameters straight away (llama-params.js preselects the LoRA).
    try {
      const pending = JSON.parse(localStorage.getItem('rocsy-pending-lora') || 'null');
      if (pending && pending.model === modelId) setTimeout(() => params.click(), 0);
    } catch (_) {}
    if (!root.dataset.bpListening) {
      root.dataset.bpListening = '1';
      const leave = event => {
        const tab = event.target.closest('[role=tab]');
        if (tab && !tab.matches(CUSTOM_TAB)) closeCustom(root);
      };
      for (const type of ['pointerdown', 'keydown', 'click']) root.addEventListener(type, leave, true);
    }
  }

  let scheduled = false;
  function refresh() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      addHomeLink();
      addApiTab();
      addParamsTab();
    });
  }
  new MutationObserver(refresh).observe(document.documentElement, {childList: true, subtree: true});
  window.addEventListener('hashchange', refresh);
  refresh();
})();
