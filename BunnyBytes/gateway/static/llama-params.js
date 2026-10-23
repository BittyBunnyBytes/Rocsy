// Model parameter editor for the "Parameters" tab llama-api-nav.js adds next
// to Details on llama-swap's model page. window.BunnyLlamaParams.mount(container,
// modelId) renders one model's form into `container`.
(() => {
  // llama-swap's own card markup (Details tab), so the form matches it.
  const NATIVE = {
    card: 'ring-foreground/10 bg-card text-card-foreground rounded-xl text-sm ring-1 group/card flex flex-col shrink-0 gap-0 overflow-hidden py-0',
    header: 'gap-1 rounded-t-xl grid auto-rows-min items-start border-b px-4 py-2',
    title: 'text-sm font-semibold',
    content: 'p-3',
  };
  let request;

  function load(force = false) {
    if (!request || force) {
      request = fetch('/llama-params/models', {credentials: 'same-origin'}).then(async response => {
        if (!response.ok) throw new Error(response.status === 401 ? 'Log in to BunnyBytes to edit parameters.' : 'Could not load model parameters.');
        return response.json();
      });
      request.catch(() => { request = null; });
    }
    return request;
  }

  // Short llama.cpp spellings typed into the Flags box map onto the fields' long flags.
  const ALIASES = {'-c': '--ctx-size', '-b': '--batch-size', '-ub': '--ubatch-size', '-t': '--threads',
    '-tb': '--threads-batch', '-ngl': '--gpu-layers', '--n-gpu-layers': '--gpu-layers', '-np': '--parallel',
    '-ctk': '--cache-type-k', '-ctv': '--cache-type-v', '-n': '--n-predict', '--top_p': '--top-p'};

  function checkValue(param, value) {
    const v = String(value).toLowerCase();
    if (param.kind === 'toggle') return ['on', 'off'].includes(v) ? '' : `${param.flag}: on or off`;
    if (param.choices) return param.choices.includes(v) ? '' : `${param.flag}: one of ${param.choices.join(', ')}`;
    if (param.kind === 'layers') return /^(all|auto|\d{1,3})$/.test(v) ? '' : `${param.flag}: all, auto or a number`;
    const number = Number(v);
    if (v === '' || Number.isNaN(number) || (param.kind === 'int' && !Number.isInteger(number))) return `${param.flag}: needs a ${param.kind === 'int' ? 'whole ' : ''}number`;
    const [low, high] = param.limits;
    return number < low || number > high ? `${param.flag}: between ${low} and ${high}` : '';
  }

  // "--flag value" text -> {flag: value}; unknown flags and missing values are reported.
  function parseFlags(text, params) {
    const known = new Map(params.map(p => [p.flag, p]));
    const words = text.split(/\s+/).filter(Boolean);
    const values = {}, errors = [];
    for (let i = 0; i < words.length; i++) {
      let [flag, inline] = words[i].includes('=') ? [words[i].slice(0, words[i].indexOf('=')), words[i].slice(words[i].indexOf('=') + 1)] : [words[i], undefined];
      if (!flag.startsWith('-') || /^-\d/.test(flag)) { errors.push(`"${words[i]}" isn't a flag`); continue; }
      flag = ALIASES[flag] || flag;
      const param = known.get(flag);
      let value = inline;
      if (value === undefined && i + 1 < words.length && (!words[i + 1].startsWith('-') || /^-\d/.test(words[i + 1]))) value = words[++i];
      if (!param) { errors.push(`${flag} can't be changed here`); continue; }
      if (param.unavailable) { errors.push(`${flag}: ${param.unavailable}`); if (value === undefined && i + 1 < words.length) i++; continue; }
      if (value === undefined) {
        if (param.kind === 'toggle') value = 'on';
        else { errors.push(`${flag}: missing a value`); continue; }
      }
      const problem = checkValue(param, value);
      if (problem) { errors.push(problem); continue; }
      values[flag] = param.kind === 'int' || param.kind === 'float' ? value : value.toLowerCase();
    }
    return {values, errors};
  }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function field(param, model) {
    const value = model.overrides[param.flag] ?? '';
    const fallback = model.defaults[param.flag] ?? 'llama.cpp default';
    let input;
    if (param.kind === 'toggle') {
      const wrap = node('label', `bp-param bp-toggle${value === 'on' ? ' bp-changed' : ''}`);
      input = node('input');
      input.type = 'checkbox';
      input.name = param.flag;
      input.id = `bp-${model.id}-${param.flag}`;
      input.checked = value === 'on';
      input.addEventListener('change', () => wrap.classList.toggle('bp-changed', input.checked));
      wrap.htmlFor = input.id;
      const head = node('span', 'bp-label');
      const title = node('span', 'bp-toggle-title');
      title.append(input, node('span', '', param.label));
      head.append(title, node('code', '', param.flag));
      wrap.append(head, node('small', '', `${param.help} Default: ${fallback}.`));
      return wrap;
    }
    if (param.choices) {
      const label = choice => param.choice_labels?.[choice] || choice;
      input = node('select');
      input.add(new Option(`Default (${label(fallback)})`, ''));
      // The "Default" option already stands for the model's default (e.g. Core Backend's choice).
      for (const choice of param.choices) if (!(param.flag === '--build' && choice === fallback)) input.add(new Option(label(choice), choice));
      input.value = value;
      if (param.unavailable) {
        input.value = '';
        input.disabled = true;
        input.title = param.unavailable;
      }
    } else {
      input = node('input');
      input.inputMode = param.kind === 'layers' ? 'text' : 'decimal';
      input.placeholder = fallback;
      input.value = value;
    }
    input.name = param.flag;
    input.id = `bp-${model.id}-${param.flag}`;
    const wrap = node('label', `bp-param${value !== '' ? ' bp-changed' : ''}`);
    wrap.htmlFor = input.id;
    input.addEventListener('input', () => wrap.classList.toggle('bp-changed', input.value !== ''));
    const head = node('span', 'bp-label');
    head.append(node('span', '', param.label), node('code', '', param.flag));
    wrap.append(head, input, node('small', param.unavailable ? 'bp-unavailable' : '', param.unavailable ? `Not available: ${param.unavailable}` : param.help));
    return wrap;
  }

  function card(skin, title, body) {
    const section = node('section', `bp-card ${skin.card}`.trim());
    section.dataset.slot = 'card';
    if (title) {
      const header = node('div', `bp-card-header ${skin.header}`.trim());
      header.append(node('div', skin.title, title));
      section.append(header);
    }
    const content = node('div', `bp-card-content ${skin.content}`.trim());
    content.append(...[].concat(body));
    section.append(content);
    return section;
  }

  async function mount(container, modelId) {
    const skin = NATIVE;
    container.classList.add('bp');
    container.replaceChildren(node('p', 'bp-note', 'Loading parameters…'));
    let data;
    try {
      data = await load();
    } catch (error) {
      container.replaceChildren(node('p', 'bp-status bp-error', error.message));
      return;
    }
    const model = data.models.find(m => m.id === modelId);
    if (!model) {
      container.replaceChildren(node('p', 'bp-note', `No editable parameters for ${modelId}.`));
      return;
    }

    // A model on its own engine (Strata) brings its own parameter list.
    const params = model.params || data.params;
    const strata = model.engine === 'strata';
    const backend = data.backend || {};
    const pinned = (backend.builds || []).find(b => b.id === model.overrides['--build']);
    const shown = pinned || {name: backend.build, boost: backend.boost, vulkan: backend.vulkan};
    const notes = [
      strata ? node('span', 'bp-pill', 'Strata engine') : node('span', 'bp-pill', shown.name ? `${shown.name} · ${shown.vulkan ? 'ROCm + Vulkan' : 'ROCm'}${pinned ? ' · set for this model' : ''}` : 'ROCm · RDNA-boost build'),
      ...(!strata && shown.boost === false ? [node('span', 'bp-pill bp-warn', 'No RDNA Boost')] : []),
      node('p', `bp-note${model.running ? ' bp-live' : ''}`, model.running
        ? '● Loaded now. Changes apply after it unloads (idle timeout) or the next model swap.'
        : 'Changes apply the next time this model loads.'),
    ];
    const summary = card(skin, null, notes);
    summary.classList.add('bp-summary');

    const form = node('form', 'bp-form');
    form.append(summary);

    // Flags box: the editable flags as text, kept in sync with the fields in both directions.
    const flagsText = node('textarea', 'bp-flags');
    flagsText.spellcheck = false;
    flagsText.setAttribute('aria-label', strata ? 'Strata options' : 'Flags sent to llama-server');
    const flagsError = node('p', 'bp-flags-error');
    flagsError.setAttribute('role', 'alert');
    const flagsHelp = node('small', 'bp-flags-help', strata ? 'One "--flag value" per line. Editing here updates the fields below and the other way round. Remove a line to go back to the default.' : 'One "--flag value" per line (short forms like -c, -b, -ub, -ngl work too). Editing here updates the fields below and the other way round. Remove a line to go back to the config default.');
    const fixedLine = node('p', 'bp-fixed');
    const fixedWords = (model.fixed || []).join(' ');
    const launcher = '--device …';
    fixedLine.append(node('span', '', 'Always sent, not editable here: '), node('code', '', `${fixedWords}${fixedWords ? ' ' : ''}${launcher}`));
    form.append(card(skin, 'Flags', strata ? [flagsText, flagsError, flagsHelp] : [flagsText, flagsError, flagsHelp, fixedLine]));
    for (const group of [...new Set(params.map(p => p.group))]) {
      const grid = node('div', 'bp-grid');
      for (const param of params.filter(p => p.group === group)) grid.append(field(param, model));
      form.append(card(skin, group, grid));
    }
    const status = node('p', 'bp-status');
    status.setAttribute('role', 'status');
    const reset = node('button', 'bp-button', 'Reset all to defaults');
    reset.type = 'button';
    const save = node('button', 'bp-button bp-primary', 'Save');
    save.type = 'submit';
    const actions = node('div', 'bp-actions');
    actions.append(status, reset, save);
    form.append(actions);
    container.replaceChildren(form);

    const fieldFor = flag => form.querySelector(`[name="${CSS.escape(flag)}"]`);
    const fieldValue = input => input.type === 'checkbox' ? (input.checked ? 'on' : '') : input.value.trim();
    const textFromFields = () => params.filter(param => !param.unavailable).map(param => {
      const input = fieldFor(param.flag);
      const value = (input && fieldValue(input)) || model.defaults[param.flag] || '';
      return value ? `${param.flag} ${value}` : '';
    }).filter(Boolean).join('\n');
    const syncText = () => {
      flagsText.value = textFromFields();
      showFlagErrors([]);
    };
    const showFlagErrors = errors => {
      flagsError.textContent = errors.length ? errors.join(' · ') : '';
      flagsText.classList.toggle('bp-invalid', errors.length > 0);
      save.disabled = errors.length > 0;
    };
    const fieldsFromText = () => {
      const {values, errors} = parseFlags(flagsText.value, params);
      for (const param of params) {
        const input = fieldFor(param.flag);
        if (!input) continue;
        let value = values[param.flag] ?? '';
        // A value equal to the config default is no override: the field stays on "Default".
        if (value !== '' && value === (model.defaults[param.flag] ?? null)) value = '';
        if (input.type === 'checkbox') input.checked = value === 'on';
        else input.value = value;
        input.closest('.bp-param')?.classList.toggle('bp-changed', input.type === 'checkbox' ? value === 'on' : value !== '');
      }
      showFlagErrors(errors);
    };
    flagsText.addEventListener('input', fieldsFromText);
    flagsText.addEventListener('blur', () => { if (!flagsError.textContent) syncText(); });
    form.addEventListener('input', event => { if (event.target !== flagsText) syncText(); });
    form.addEventListener('change', event => { if (event.target !== flagsText) syncText(); });
    syncText();
    // Sized once: resizing on every sync would move Save away mid-click (the box re-syncs on blur).
    flagsText.rows = Math.max(6, flagsText.value.split('\n').length + 2);

    const say = (message, error = false) => {
      status.textContent = message;
      status.classList.toggle('bp-error', error);
    };
    reset.addEventListener('click', () => {
      form.querySelectorAll('input, select').forEach(input => {
        if (input.type === 'checkbox') input.checked = false; else input.value = '';
      });
      form.querySelectorAll('.bp-param').forEach(p => p.classList.remove('bp-changed'));
      syncText();
      say('Cleared. Save to apply.');
    });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (flagsError.textContent) return say('Fix the Flags box first.', true);
      const params = {};
      form.querySelectorAll('input, select').forEach(input => {
        params[input.name] = input.type === 'checkbox' ? (input.checked ? 'on' : '') : input.value.trim();
      });
      save.disabled = true;
      try {
        const response = await fetch(`/llama-params/models/${encodeURIComponent(modelId)}`, {
          method: 'PUT',
          credentials: 'same-origin',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({params}),
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) return say(result.message || 'Could not save parameters.', true);
        model.overrides = result.overrides;
        await mount(container, modelId);
        const fresh = container.querySelector('.bp-status');
        if (fresh) fresh.textContent = model.running ? 'Saved. Applies when this model next loads.' : 'Saved.';
      } catch (error) {
        say(error.message, true);
      } finally {
        save.disabled = false;
      }
    });
  }

  window.BunnyLlamaParams = {mount, load, node, card, skin: NATIVE};
})();
